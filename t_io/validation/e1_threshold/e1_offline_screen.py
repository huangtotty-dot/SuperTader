# -*- coding: utf-8 -*-
"""
e1_offline_screen.py — E1 引擎买阈阶梯离线筛查(纯分析, 零引擎改动)
背景: §1 买分诊断(buy_score_dist)显示 68≈p99 从未生效; 引擎 buy_threshold=42 (signal_engine.py:524)。
     E1 路线=降低引擎买阈 (42→38/36/30 阶梯)。本脚本在 control 组(e2_variant_a, 仅键修复新基线)
     的 decision_trace 上离线复算各档行为, 选出 1-2 个决赛档进全管线 A/B。
候选规则(已拍板, 见交接笔记):
  candidate(T) = buy_score >= T + (trace_buy_threshold - 42)   # 保留 +8 早盘预警 / -5 接回放松语义
               AND buy_score > sell_score                       # 仲裁(照抄 signal_engine.py:620)
               AND 复算门控开(near_ma5_chop/above_ma5_trend)     # ma5_state 照抄 data_fetcher.py:169-176
                  OR trace decision=="BUY_LOW"                  # f5 强反转 bypass(signal_engine.py:585-586)不可离线复算, 仅保守放行实测已知者
               AND buy_block == []                              # 风控块(score 无关, trace 直取)
  记录层(照抄 harness_backtest.py:543-573):
               AND buy_score >= notify_buy (变体a: 个股43/40; 变体b: 对齐T)
               AND 同方向段去重(>=5min)
  引擎侧: 30min 冷却(cooldown_minutes=30, config.py:296), 冷却由 record_signal 触发(信号过记录层才启动)
闭环预估: control 实际信号 + 增量买入(qty=每股正qty中位, 缺省100, 近似) -> compute_closed_loop
产物: e1_threshold/e1_screen.json + 控制台表
"""
import json, math, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")
PRE = BASE / "t_io/validation/e2_daily_gate/minute_snapshots_pre"
SNAP = BASE / "t_io/minute_snapshots_ts"
PARTS = BASE / "t_io/validation/e2_variant_a/parts"
CONTROL_SIGNALS = BASE / "t_io/validation/e2_variant_a/control/signals.jsonl"
OUT = BASE / "t_io/validation/e1_threshold"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE))
import pandas as pd  # noqa: E402
from harness_backtest import settle_signal, classify_day_type, compute_closed_loop  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
NOTIFY_BUY_A = {"000988": 43.0, "600481": 43.0, "588170": 40.0, "600176": 40.0, "603667": 40.0}  # config.py:389-427
T_LADDER = [42, 38, 36, 30]
ENGINE_CD_MIN = 30   # config.py:296 cooldown_minutes
SEG_DEDUP_MIN = 5    # harness_backtest.py:569
CONTROL_BUY_N = 126  # e2_variant_a/control/summary.json 实测

# ---------- 1. 日线聚合(预热+样本, 无前视; 照抄 e2_daily_gate_analysis.py) ----------
def load_daily(code):
    daily = {}
    for root in (PRE, SNAP):
        for fp in root.glob(f"*/*/{code}_*.json"):
            d = json.load(open(fp, encoding="utf-8"))
            bars = d["bars"]
            if not bars:
                continue
            daily[d["date"]] = {"open": bars[0]["open"], "close": bars[-1]["close"],
                                "high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars),
                                "volume": sum(b["volume"] for b in bars)}
    return dict(sorted(daily.items()))

daily = {c: load_daily(c) for c in CODES}
print("daily bars:", {c: (len(v), min(v), max(v)) for c, v in daily.items()})

# 每股每样本日: 前一交易日的 ma5/slope(门控全部判别力, E2已验证)
prev_ma5 = {}   # code -> {sample_date: (ma5, slope) or None}
for c in CODES:
    dates = sorted(daily[c])
    closes = [daily[c][d]["close"] for d in dates]
    prev_ma5[c] = {}
    for i, d in enumerate(dates):
        j = i - 1  # 前一交易日
        if j >= 9:
            m5 = sum(closes[j - 4:j + 1]) / 5
            m5p = sum(closes[j - 9:j - 4]) / 5
            slope = (m5 - m5p) / m5p if m5p else 0.0
            prev_ma5[c][d] = (m5, slope)
        else:
            prev_ma5[c][d] = None

def gate_open(code, date, price):
    """ma5_state 三态(照抄 data_fetcher.py:169-176); 开闸=near/above"""
    pm = prev_ma5[code].get(date)
    if not pm:
        return None  # unknown
    ma5, slope = pm
    gap = abs(price - ma5) / ma5
    if gap <= 0.01:
        return True   # near_ma5_chop
    if price >= ma5 and slope >= 0:
        return True   # above_ma5_trend
    return False      # below_ma5_weak

# ---------- 2. 日型 ----------
snap_cache = {}
def day_bars(code, date):
    key = (code, date)
    if key not in snap_cache:
        fp = SNAP / date[:4] / date[5:7] / f"{code}_{date}.json"
        snap_cache[key] = json.load(open(fp, encoding="utf-8"))["bars"] if fp.exists() else []
    return snap_cache[key]

# ---------- 3. 加载 control trace ----------
ticks = defaultdict(list)   # code -> [tick dict] (按 ts 排序)
sample_dates = set()
for part in sorted(PARTS.iterdir()):
    if not part.name.startswith("control_"):
        continue
    for fp in sorted(part.glob("decision_trace_*.jsonl")):
        date = fp.stem.replace("decision_trace_", "")
        sample_dates.add(date)
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            bs = r.get("buy_score")
            if bs is None or (isinstance(bs, float) and math.isnan(bs)):
                continue
            ticks[r["code"]].append({
                "ts": r["scan_time"], "dt": datetime.strptime(r["scan_time"], "%Y-%m-%d %H:%M:%S"),
                "date": date, "hm": r["scan_time"][11:16], "price": float(r["price"]),
                "bs": float(bs), "ss": float(r.get("sell_score") or 0),
                "bth": float(r.get("buy_threshold") or 42.0),
                "block": tuple(r.get("buy_block") or []),
                "reason": r.get("decision_reason", ""),
            })
for c in CODES:
    ticks[c].sort(key=lambda t: t["dt"])
n_ticks = sum(len(v) for v in ticks.values())
sample_dates = sorted(sample_dates)
stock_days = sum(1 for c in CODES for d in sample_dates if day_bars(c, d))
print(f"ticks: {n_ticks}, sample_dates: {len(sample_dates)} ({sample_dates[0]}~{sample_dates[-1]}), stock_days: {stock_days}")

daytype = {}
for c in CODES:
    for d in sample_dates:
        bars = day_bars(c, d)
        if bars:
            df = pd.DataFrame(bars)
            df["time"] = pd.to_datetime(df["time"])
            daytype[(c, d)] = classify_day_type(df)
print("daytype census:", Counter(daytype.values()))

# ---------- 4. control 实测信号(基线) ----------
control_sigs = [json.loads(l) for l in open(CONTROL_SIGNALS, encoding="utf-8")]
control_buys = [s for s in control_sigs if s["action"] == "BUY_LOW"]
control_buy_key = {(s["code"], s["ts"][:16]) for s in control_buys}
pos_qty = defaultdict(list)
for s in control_buys:
    if s.get("qty", 0) > 0:
        pos_qty[s["code"]].append(s["qty"])
MEDIAN_QTY = {c: (int(statistics.median(pos_qty[c])) if pos_qty[c] else 100) for c in CODES}
print("median positive qty per code (近似用):", MEDIAN_QTY)
holdings = json.load(open(BASE / "holdings.json", encoding="utf-8"))
hmap = {k.split("_")[0]: v for k, v in holdings.items()}
cl_control = compute_closed_loop(control_sigs, hmap)
print("control closed loop:", cl_control["total_closed_pairs"], cl_control["total_net_pnl"])

# control 阴跌日买入 wr(基线)
_cb_bear = [s for s in control_buys if daytype.get((s["code"], s["ts"][:10])) == "bear_day"]
_bw = sum(1 for s in _cb_bear if s["settle_result"] == "WIN")
_bf = sum(1 for s in _cb_bear if s["settle_result"] == "FAIL")
control_bear = {"n": _bw + _bf, "wr": round(_bw / (_bw + _bf), 4) if (_bw + _bf) else None}
print("control bear_day buy wr:", control_bear)

# ---------- 5. 离线模拟 ----------
def simulate(T, variant):
    """返回记录层买入事件列表(含结算)。variant: 'a'=引擎-only(notify个股), 'b'=引擎+notify对齐T"""
    records = []
    for c in CODES:
        notify = NOTIFY_BUY_A[c] if variant == "a" else float(T)
        last_rec = None   # 引擎30min冷却(过记录层才启动)
        last_seg = None   # 记录层5min段去重
        for t in ticks[c]:
            adj = T + (t["bth"] - 42.0)
            if not (t["bs"] >= adj and t["bs"] > t["ss"]):
                continue
            go = gate_open(c, t["date"], t["price"])
            if go is not True and t["reason"] != "BUY_LOW":
                continue   # 门控关(below/unknown)且无实测bypass证据
            if t["block"]:
                continue
            if last_rec is not None and (t["dt"] - last_rec).total_seconds() < ENGINE_CD_MIN * 60:
                continue   # HOLD_BUY_COOLDOWN
            if t["bs"] < notify:
                continue   # 记录层通知阈
            if last_seg is not None and (t["dt"] - last_seg).total_seconds() < SEG_DEDUP_MIN * 60:
                continue   # 同方向段去重
            records.append({"code": c, "ts": t["ts"], "date": t["date"], "hm": t["hm"],
                            "price": t["price"], "bs": t["bs"], "ss": t["ss"]})
            last_rec = t["dt"]
            last_seg = t["dt"]
    # 结算(§1.1: 30根1分K, +0.5%/-0.4%)
    for r in records:
        bars = day_bars(r["code"], r["date"])
        t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
        i = t2i.get(r["hm"])
        if i is None:
            r["res"] = "VOID"
            continue
        fut = pd.DataFrame(bars[i + 1: i + 31])
        res, st = settle_signal("BUY_LOW", bars[i]["close"], fut)
        r["res"] = res
        r["settle_time"] = st
        r["dtype"] = daytype.get((r["code"], r["date"]), "unknown")
    return records

def wr_of(evts):
    w = sum(1 for e in evts if e["res"] == "WIN")
    f = sum(1 for e in evts if e["res"] == "FAIL")
    v = sum(1 for e in evts if e["res"] == "VOID")
    return {"n": len(evts), "wins": w, "fails": f, "voids": v,
            "wr": round(w / (w + f), 4) if (w + f) else None}

results = {}
for T in T_LADDER:
    for variant in ("a", "b"):
        key = f"T{T}{variant}"
        recs = simulate(T, variant)
        bear = [r for r in recs if r.get("dtype") == "bear_day"]
        incr = [r for r in recs if (r["code"], r["ts"][:16]) not in control_buy_key]
        overlap = len(recs) - len(incr)
        # 闭环预估: control 实际信号 + 增量买入(qty近似)
        incr_sigs = [{"ts": r["ts"], "code": r["code"], "action": "BUY_LOW",
                      "price": r["price"], "qty": MEDIAN_QTY[r["code"]]} for r in incr]
        cl = compute_closed_loop(control_sigs + incr_sigs, hmap)
        results[key] = {
            "buys": wr_of(recs),
            "density_per_stock_day": round(len(recs) / stock_days, 3),
            "by_code": {c: wr_of([r for r in recs if r["code"] == c]) for c in CODES},
            "by_dtype": {dt: wr_of([r for r in recs if r.get("dtype") == dt])
                         for dt in ("bull_day", "bear_day", "reversal_day", "chop_day", "unknown")},
            "bear_day_buy": wr_of(bear),
            "vs_control": {"overlap": overlap, "incremental": len(incr),
                           "incremental_settle": wr_of(incr)},
            "closed_loop_est": {"pairs": cl["total_closed_pairs"], "pnl": cl["total_net_pnl"],
                                "per_stock": cl["per_stock"]},
        }
        print(f"{key}: buys={results[key]['buys']} density={results[key]['density_per_stock_day']} "
              f"bear={results[key]['bear_day_buy']} incr={len(incr)} cl=({cl['total_closed_pairs']}, {cl['total_net_pnl']})")

# ---------- 6. 方法论自检: T=42a 应复现 control 实测 126±10% ----------
chk = results["T42a"]
dev = abs(chk["buys"]["n"] - CONTROL_BUY_N) / CONTROL_BUY_N
self_check = {"target": CONTROL_BUY_N, "offline": chk["buys"]["n"], "deviation": round(dev, 4),
              "pass": dev <= 0.10,
              "note": "偏差来源: 门控离线复算 vs 生产注入日上下文(E2已验证误差0.24pp); f5 bypass仅保守放行实测已知; "
                      "状态机二阶效应(接回/止盈轨迹随新买入改变)不可离线建模"}
print("SELF-CHECK:", self_check)

# ---------- 7. 输出 ----------
result = {
    "meta": {
        "purpose": "E1 引擎买阈阶梯离线筛查 — 选决赛档进全管线",
        "baseline": "control(e2_variant_a, 仅键修复新基线): 买126 wr0.5044 / 卖326 wr0.4667 / 闭环41对 +221.31 / 阴跌日买wr "
                    + json.dumps(control_bear),
        "candidate_rule": "bs>=T+(trace_bth-42) AND bs>ss AND 门控开(or实测BUY_LOW bypass) AND buy_block==[] "
                          "AND 记录层notify(a=个股43/40, b=T) AND 5min段去重; 引擎30min冷却",
        "two_layer_coupling": "引擎降阈后仍受记录层 notify_buy_threshold(000988/600481=43, 余=40)过滤: "
                              "变体a(引擎-only)对43阈股零增益区间[43,42)=空集; 变体b(对齐notify=T)才有密度增益",
        "unmodelable": "卖侧不变(非对称变体, 仲裁看分数不看阈值); 状态机二阶挤压(接回-5/止盈轨迹)离线不可建模, "
                       "E2变体A已验证阈值类变更离线→实测可迁移(误差0.24pp)",
        "qty_approx": "增量买入 qty=每股正qty中位(000988实测全为0→缺省100), 闭环PnL为近似",
        "stock_days": stock_days, "sample_dates": f"{sample_dates[0]}~{sample_dates[-1]}",
        "ticks": n_ticks,
    },
    "control_baseline": {"buy_n": CONTROL_BUY_N, "buy_wr": 0.5044, "sell_n": 326, "sell_wr": 0.4667,
                          "closed_pairs": cl_control["total_closed_pairs"], "closed_pnl": cl_control["total_net_pnl"],
                          "bear_day_buy": control_bear,
                          "density_per_stock_day": round(CONTROL_BUY_N / stock_days, 3)},
    "self_check": self_check,
    "variants": results,
}
with open(OUT / "e1_screen.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)
print("JSON written:", OUT / "e1_screen.json")

print("\n== 阶梯总表 ==")
print(f"{'档':<7}{'买n':>5}{'wr':>8}{'增量':>5}{'密度':>7}{'阴跌wr':>8}{'闭环对':>6}{'PnL':>9}")
print(f"{'control':<7}{126:>5}{0.5044:>8}{'-':>5}{round(126/stock_days,3):>7}{str(control_bear['wr']):>8}{cl_control['total_closed_pairs']:>6}{cl_control['total_net_pnl']:>9}")
for T in T_LADDER:
    for v in ("a", "b"):
        k = f"T{T}{v}"; r = results[k]
        print(f"{k:<7}{r['buys']['n']:>5}{str(r['buys']['wr']):>8}{r['vs_control']['incremental']:>5}"
              f"{r['density_per_stock_day']:>7}{str(r['bear_day_buy']['wr']):>8}"
              f"{r['closed_loop_est']['pairs']:>6}{r['closed_loop_est']['pnl']:>9}")
