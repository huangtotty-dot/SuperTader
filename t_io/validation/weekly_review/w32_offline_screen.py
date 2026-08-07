# -*- coding: utf-8 -*-
"""
w32_offline_screen.py — 2026-W32 周复盘 §3 离线筛查（纯分析，零引擎改动）
三候选并行：
  C1 接回解耦：awaiting_buyback 接回信号绕过 {daily_overheated, index_uni_down_clearance} 门控
  C2 卖侧 bull 日：T+5 / T+10 卖阈上浮（a=引擎-only / b=引擎+notify 对齐）+ G 强上涨硬阻断
  C6 B 类语义：纯上涨窗 RSI=100 填充（1m 盲 tick 解盲 + 卖分 +4.0/+14.0 上限）及与 C2 交互
方法论复用 E1（t_io/validation/e1_threshold/e1_offline_screen.py）：
  control = e1_final/T36b 全管线产物（当前生产口径，90 交易日 / 450 股日 / 5 码）
  离线复算变体行为 → 自检 → 决赛档建议；全管线决赛（§4）待闸门锁定后另跑。
产物：w32_screen.json + 控制台表
"""
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")
PRE = BASE / "t_io/validation/e2_daily_gate/minute_snapshots_pre"
SNAP = BASE / "t_io/minute_snapshots_ts"
PARTS = BASE / "t_io/validation/e1_final/parts"
CONTROL_SIGNALS = BASE / "t_io/validation/e1_final/T36b/signals.jsonl"
OUT = BASE / "t_io/validation/weekly_review"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from harness_backtest import settle_signal, classify_day_type, compute_closed_loop  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
NOTIFY_BUY = 36.0                       # E1 采纳：个股 notify_buy 全对齐 36
NOTIFY_SELL = {"600176": 51.0}          # 600176=51，其余 55（config.py:393-431）
NOTIFY_SELL_DEFAULT = 55.0
NOTIFY_SELL_EARLY = 65.0                # 早盘 <10:00 档（config.py:330）
ENGINE_SELL_TH = 42.0                   # signal_engine.py:534 固定
ENGINE_CD_MIN = 30
SEG_DEDUP_MIN = 5
BYPASS_BLOCKS = {"daily_overheated", "index_uni_down_clearance"}  # C1 绕过集
# control 基线（父代理给定，E1 T36b 口径；SC1 逐项核对）
CONTROL = {"buy_n": 144, "buy_wr": 0.5038, "sell_n": 330, "sell_wr": 0.4638,
           "total_n": 474, "total_wr": 0.4759, "cl_pairs": 47, "cl_pnl": 252.98, "buy_density": 0.320}

# ---------- 0. 公共数据装载 ----------
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
prev_close = {}   # (code,date) -> 前一交易日收盘
prev_ma5 = {}
for c in CODES:
    dates = sorted(daily[c])
    closes = [daily[c][d]["close"] for d in dates]
    for i, d in enumerate(dates):
        if i >= 1:
            prev_close[(c, d)] = closes[i - 1]
        if i >= 9:
            m5 = sum(closes[i - 5:i]) / 5
            m5p = sum(closes[i - 10:i - 5]) / 5
            prev_ma5.setdefault(c, {})[d] = (m5, (m5 - m5p) / m5p if m5p else 0.0)
        else:
            prev_ma5.setdefault(c, {})[d] = None

def gate_open(code, date, price):
    """ma5_state 三态（照抄 data_fetcher.py:169-176 / E1 复刻）；开闸=near/above"""
    pm = prev_ma5.get(code, {}).get(date)
    if not pm:
        return None
    ma5, slope = pm
    gap = abs(price - ma5) / ma5
    if gap <= 0.01:
        return True
    if price >= ma5 and slope >= 0:
        return True
    return False

snap_cache = {}
def day_bars(code, date):
    key = (code, date)
    if key not in snap_cache:
        fp = SNAP / date[:4] / date[5:7] / f"{code}_{date}.json"
        snap_cache[key] = json.load(open(fp, encoding="utf-8"))["bars"] if fp.exists() else []
    return snap_cache[key]

# ---------- 1. 装载 T36b control trace ----------
ticks = defaultdict(list)   # code -> [tick]
sample_dates = set()
for part in sorted(PARTS.iterdir()):
    if not part.name.startswith("T36b_"):
        continue
    for fp in sorted(part.glob("decision_trace_*.jsonl")):
        date = fp.stem.replace("decision_trace_", "")
        sample_dates.add(date)
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            bs, ss = r.get("buy_score"), r.get("sell_score")
            blind = (bs is None or (isinstance(bs, float) and math.isnan(bs))
                     or ss is None or (isinstance(ss, float) and math.isnan(ss)))
            ticks[r["code"]].append({
                "ts": r["scan_time"], "dt": datetime.strptime(r["scan_time"], "%Y-%m-%d %H:%M:%S"),
                "date": date, "hm": r["scan_time"][11:16], "price": float(r["price"]),
                "bs": float("nan") if blind else float(bs), "ss": float("nan") if blind else float(ss),
                "bth": float(r.get("buy_threshold") or 42.0),
                "block": tuple(r.get("buy_block") or []),
                "reason": r.get("decision_reason", ""),
                "bf": r.get("buy_factors") or {},
                "rsi_trace": r.get("rsi"),
                "blind": blind,
            })
for c in CODES:
    ticks[c].sort(key=lambda t: t["dt"])
n_ticks = sum(len(v) for v in ticks.values())
sample_dates = sorted(sample_dates)
stock_days = sum(1 for c in CODES for d in sample_dates if day_bars(c, d))
print(f"ticks: {n_ticks}, dates: {len(sample_dates)} ({sample_dates[0]}~{sample_dates[-1]}), stock_days: {stock_days}")

daytype = {}
for c in CODES:
    for d in sample_dates:
        bars = day_bars(c, d)
        if bars:
            df = pd.DataFrame(bars)
            df["time"] = pd.to_datetime(df["time"])
            daytype[(c, d)] = classify_day_type(df)
print("daytype census:", Counter(daytype.values()))

control_sigs = [json.loads(l) for l in open(CONTROL_SIGNALS, encoding="utf-8")]
control_buys = [s for s in control_sigs if s["action"] == "BUY_LOW"]
control_sells = [s for s in control_sigs if s["action"] == "SELL_HIGH"]
pos_qty = defaultdict(list)
for s in control_buys:
    if s.get("qty", 0) > 0:
        pos_qty[s["code"]].append(s["qty"])
MEDIAN_QTY = {c: (int(statistics.median(pos_qty[c])) if pos_qty[c] else 100) for c in CODES}
holdings = json.load(open(BASE / "holdings.json", encoding="utf-8"))
hmap = {k.split("_")[0]: v for k, v in holdings.items()}
cl_control = compute_closed_loop(control_sigs, hmap)

def wr_of(evts, key="res"):
    w = sum(1 for e in evts if e.get(key) == "WIN")
    f = sum(1 for e in evts if e.get(key) == "FAIL")
    return {"n": len(evts), "wins": w, "fails": f,
            "wr": round(w / (w + f), 4) if (w + f) else None}

def settle_one(action, code, date, hm, price):
    bars = day_bars(code, date)
    t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
    i = t2i.get(hm)
    if i is None:
        return "VOID", None
    fut = pd.DataFrame(bars[i + 1: i + 31])
    return settle_signal(action, price, fut)

# ========== SC1: control 复现自检 ==========
sc1 = {
    "buy_n": len(control_buys), "sell_n": len(control_sells),
    "buy_wr": wr_of([{"res": s["settle_result"]} for s in control_buys])["wr"],
    "sell_wr": wr_of([{"res": s["settle_result"]} for s in control_sells])["wr"],
    "cl_pairs": cl_control["total_closed_pairs"], "cl_pnl": cl_control["total_net_pnl"],
}
sc1["pass"] = (sc1["buy_n"] == CONTROL["buy_n"] and sc1["sell_n"] == CONTROL["sell_n"]
               and abs(sc1["buy_wr"] - CONTROL["buy_wr"]) < 0.001
               and abs(sc1["sell_wr"] - CONTROL["sell_wr"]) < 0.001
               and sc1["cl_pairs"] == CONTROL["cl_pairs"] and abs(sc1["cl_pnl"] - CONTROL["cl_pnl"]) < 0.5)
print("SC1 control 复现:", sc1)

# ========== C1: 接回解耦 ==========
print("\n== C1 接回解耦 ==")
cand = []
reason_census = Counter()
for c in CODES:
    for t in ticks[c]:
        if t["blind"]:
            continue
        if not any(k.startswith("接回追踪") for k in t["bf"]):
            continue
        if not t["reason"].startswith("HOLD_BUY_BLOCKED"):
            continue
        reason_census[t["reason"] if len(t["reason"]) < 60 else t["reason"][:60]] += 1
        if not (set(t["block"]) & BYPASS_BLOCKS):
            continue  # 被其他原因拦（daily_gate/早盘预警等），非 C1 域
        cand.append({**t, "code": c})
other_block = [t for t in cand if not (set(t["block"]) <= BYPASS_BLOCKS)]
clean = [t for t in cand if set(t["block"]) <= BYPASS_BLOCKS]
gate_excl = [t for t in clean if gate_open(t["code"], t["date"], t["price"]) is not True]
pool = [t for t in clean if gate_open(t["code"], t["date"], t["price"]) is True]
print(f"接回激活且被拦 tick: {len(cand)}（含其他 block {len(other_block)}）→ 纯绕过集 {len(clean)}"
      f" → ma5门控保守剔除 {len(gate_excl)} → 候选池 {len(pool)}")

control_buy_dts = defaultdict(list)
for s in control_buys:
    control_buy_dts[s["code"]].append(datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%S"))
for c in CODES:
    control_buy_dts[c].sort()

incr = []
below_notify = 0
for c in CODES:
    last_rec = last_seg = None
    events = ([(d, "ctl", None) for d in control_buy_dts[c]]
              + [(t["dt"], "cand", t) for t in pool if t["code"] == c])
    events.sort(key=lambda e: e[0])
    for dt, kind, t in events:
        if kind == "ctl":
            last_rec = dt; last_seg = dt
            continue
        if last_rec is not None and (dt - last_rec).total_seconds() < ENGINE_CD_MIN * 60:
            continue
        if t["bs"] < NOTIFY_BUY:
            below_notify += 1
            continue
        if last_seg is not None and (dt - last_seg).total_seconds() < SEG_DEDUP_MIN * 60:
            continue
        incr.append(t)
        last_rec = dt; last_seg = dt
for t in incr:
    res, st = settle_one("BUY_LOW", t["code"], t["date"], t["hm"], t["price"])
    t["res"] = res
    t["dtype"] = daytype.get((t["code"], t["date"]), "unknown")
    # 接回完成代理：同 code-day 此前有 control 卖
    t["has_prior_sell"] = any(s["code"] == t["code"] and s["ts"][:10] == t["date"] and s["ts"] < t["ts"]
                              for s in control_sells)
overlap = sum(1 for t in incr
              if any(s["code"] == t["code"] and s["ts"][:16] == t["ts"][:16] for s in control_buys))
incr_sigs = [{"ts": t["ts"], "code": t["code"], "action": "BUY_LOW",
              "price": t["price"], "qty": MEDIAN_QTY[t["code"]]} for t in incr]
cl_c1 = compute_closed_loop(control_sigs + incr_sigs, hmap)
per_day = Counter((t["code"], t["date"]) for t in incr)
c1 = {
    "activated_blocked_ticks": len(cand),
    "with_other_blocks": len(other_block),
    "clean_bypass_set": len(clean),
    "ma5_gate_excluded": len(gate_excl),
    "pool": len(pool),
    "below_notify_36": below_notify,
    "recorded": len(incr),
    "settle": wr_of(incr),
    "by_dtype": {dt: wr_of([t for t in incr if t["dtype"] == dt])
                 for dt in ("bull_day", "bear_day", "reversal_day", "chop_day", "unknown")},
    "overlap_control_buy": overlap,
    "buyback_completion_proxy": sum(1 for t in incr if t["has_prior_sell"]),
    "density_total_buy": round((len(control_buys) + len(incr)) / stock_days, 3),
    "max_per_code_day": max(per_day.values()) if per_day else 0,
    "closed_loop": {"pairs": cl_c1["total_closed_pairs"], "pnl": cl_c1["total_net_pnl"],
                    "d_pairs": cl_c1["total_closed_pairs"] - cl_control["total_closed_pairs"],
                    "d_pnl": round(cl_c1["total_net_pnl"] - cl_control["total_net_pnl"], 2)},
    "reason_census_top": reason_census.most_common(5),
}
print(json.dumps({k: v for k, v in c1.items() if k != "reason_census_top"}, ensure_ascii=False, indent=1))

# ========== C2: 卖侧 bull 日 ==========
print("\n== C2 卖侧 bull 日 ==")
sells = []
for s in control_sells:
    d = s["ts"][:10]
    sells.append({**s, "date": d, "hm": s["ts"][11:16],
                  "dt": datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%S"),
                  "dtype": daytype.get((s["code"], d), "unknown"),
                  "early": int(s["ts"][11:13]) < 10})
bull_sells = [s for s in sells if s["dtype"] == "bull_day"]
nonbull_sells = [s for s in sells if s["dtype"] != "bull_day"]
c2_base = {
    "all": wr_of([{ "res": s["settle_result"]} for s in sells]),
    "bull": wr_of([{ "res": s["settle_result"]} for s in bull_sells]),
    "nonbull": wr_of([{ "res": s["settle_result"]} for s in nonbull_sells]),
    "bull_share": round(len(bull_sells) / len(sells), 4),
}
print("control 卖基线:", c2_base)

def strong_up_proxy(code, date, hm):
    """G 变体：tick 时点强上涨代理（无前视；镜像 signal_engine.py:781-787，vwap 用累计典型价代理，扩展至 ETF）"""
    bars = day_bars(code, date)
    t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
    i = t2i.get(hm)
    if i is None or i < 19:
        return None
    sub = bars[:i + 1]
    price = float(sub[-1]["close"])
    closes = [float(b["close"]) for b in sub]
    c5 = sum(closes[-5:]) / 5
    c10 = sum(closes[-10:]) / 10
    c20 = sum(closes[-20:]) / 20
    ma_ok = c5 >= c10 * 0.995 and c10 >= c20 * 0.995
    day_low = min(float(b["low"]) for b in sub)
    atr = sum(float(b["high"]) - float(b["low"]) for b in sub[-14:]) / min(14, len(sub)) / price if price else 0.02
    atr = max(atr, 0.002)
    rebound = (price - day_low) / day_low if day_low > 0 else 0
    num = sum(((float(b["high"]) + float(b["low"]) + float(b["close"])) / 3) * float(b.get("volume", 0)) for b in sub)
    den = sum(float(b.get("volume", 0)) for b in sub)
    vwap = num / den if den > 0 else closes[-1]
    return bool(ma_ok and rebound > 3 * atr and price > vwap * 1.005)

def c2_variant(name, bull_uplift=0, g_block=False):
    kept, dropped = [], []
    for s in sells:
        if s["dtype"] == "bull_day":
            if g_block:
                if s["sell_score"] <= 80 and strong_up_proxy(s["code"], s["date"], s["hm"]):
                    dropped.append(s); continue
            elif bull_uplift > 0:
                req = float(s.get("threshold") or NOTIFY_SELL_DEFAULT) + bull_uplift
                if float(s["sell_score"]) < req:
                    dropped.append(s); continue
        kept.append(s)
    kept_sigs = [s for s in control_sigs if not (s["action"] == "SELL_HIGH" and any(
        d is s for d in dropped))]
    cl = compute_closed_loop(kept_sigs, hmap)
    kb = [s for s in kept if s["dtype"] == "bull_day"]
    return {
        "variant": name,
        "dropped": len(dropped),
        "dropped_wr": wr_of([{"res": s["settle_result"]} for s in dropped]),
        "kept_all": wr_of([{"res": s["settle_result"]} for s in kept]),
        "kept_bull": wr_of([{"res": s["settle_result"]} for s in kb]),
        "kept_nonbull": wr_of([{"res": s["settle_result"]} for s in kept if s["dtype"] != "bull_day"]),
        "sell_density": round(len(kept) / stock_days, 3),
        "closed_loop": {"pairs": cl["total_closed_pairs"], "pnl": cl["total_net_pnl"],
                        "d_pairs": cl["total_closed_pairs"] - cl_control["total_closed_pairs"],
                        "d_pnl": round(cl["total_net_pnl"] - cl_control["total_net_pnl"], 2)},
    }

c2 = {"baseline": c2_base, "variants": {}}
# a 档（引擎-only）：42→47/52 仍低于 notify 51/55 → 信号零变化（两层耦合，断言）
c2["variants"]["T+5a_engine_only"] = {"note": "引擎 42→47 < notify 51/55，记录层不变 → 信号零变化",
                                       "dropped": 0}
c2["variants"]["T+10a_engine_only"] = {"note": "引擎 42→52 < notify 51/55，记录层不变 → 信号零变化",
                                        "dropped": 0}
c2["variants"]["T+5b"] = c2_variant("T+5b", bull_uplift=5)
c2["variants"]["T+10b"] = c2_variant("T+10b", bull_uplift=10)
c2["variants"]["G"] = c2_variant("G", g_block=True)
for k, v in c2["variants"].items():
    print(k, json.dumps(v, ensure_ascii=False))
# SC2: T+0 恒等
sc2 = {"pass": c2_variant("T+0", bull_uplift=0)["dropped"] == 0}

# ========== C6: B 类语义 ==========
print("\n== C6 B 类语义 ==")
blind_ticks = [(c, t) for c in CODES for t in ticks[c] if t["blind"]]
blind_days = sorted({(c, t["date"]) for c, t in blind_ticks})
print(f"盲 tick: {len(blind_ticks)}，涉及 {len(blind_days)} 股日")

def rsi_class_series(closes, period):
    """照抄 indicators.py RSI（Cutler 滚动均值, min_periods=1）→ 逐 bar 分类 pure_up/zero_zero/normal + B语义值"""
    c = pd.Series(closes, dtype=float)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(period, min_periods=1).mean()
    cls, bval = [], []
    for g, l in zip(gain, loss):
        if pd.isna(g) or pd.isna(l):
            cls.append("warmup"); bval.append(float("nan"))
        elif l == 0 and g > 0:
            cls.append("pure_up"); bval.append(100.0)
        elif l == 0 and g == 0:
            cls.append("zero_zero"); bval.append(50.0)   # V1.1.2 已修（非 B 类域）
        else:
            rs = g / l
            cls.append("normal"); bval.append(round(100 - 100 / (1 + rs), 4))
    return cls, bval

c6_stats = Counter()
rsi_xcheck = []
pu_events = []   # 纯上涨盲 tick 明细
for c, d in blind_days:
    bars = day_bars(c, d)
    if not bars:
        continue
    closes = [float(b["close"]) for b in bars]
    cls1m, _ = rsi_class_series(closes, 6)
    t2i = {b["time"][11:16]: i for i, b in enumerate(bars)}
    # 5m 已完工 bar 序列
    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"])
    df5 = df.set_index("time").resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    cls5m, _ = rsi_class_series(df5["close"].tolist(), 14)
    ends5 = list(df5.index)
    day_ticks = sorted([t for cc, t in blind_ticks if cc == c and t["date"] == d], key=lambda t: t["dt"])
    nonblind = [t for t in ticks[c] if t["date"] == d and not t["blind"]]
    for t in day_ticks:
        i = t2i.get(t["hm"])
        k1 = cls1m[i] if i is not None else "no_bar"
        c6_stats[k1] += 1
        if k1 != "pure_up":
            continue
        # 5m：取 end<=tick 的最后完工 5m bar
        tdt = t["dt"]
        j = None
        for k in range(len(ends5) - 1, -1, -1):
            if ends5[k] <= tdt:
                j = k; break
        pu5 = (j is not None and cls5m[j] == "pure_up")
        # 代理基底：同股日前/后最近非盲 tick
        before = [x for x in nonblind if x["dt"] < tdt]
        after = [x for x in nonblind if x["dt"] > tdt]
        pu_events.append({
            "code": c, "date": d, "hm": t["hm"], "dt": tdt, "price": t["price"],
            "pu5": pu5,
            "base_b": before[-1] if before else None,
            "base_a": after[0] if after else None,
        })
# SC3: RSI 公式复算交叉校验（非盲 tick 抽样）
checked = 0
for c in CODES:
    for t in ticks[c]:
        if t["blind"] or t["rsi_trace"] is None or checked >= 400:
            continue
        bars = day_bars(c, t["date"])
        t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
        i = t2i.get(t["hm"])
        if i is None:
            continue
        _, bv = rsi_class_series([float(b["close"]) for b in bars[:i + 1]], 6)
        if not math.isnan(bv[-1]):
            rsi_xcheck.append(abs(bv[-1] - float(t["rsi_trace"])))
            checked += 1
sc3 = {"n": len(rsi_xcheck),
       "median_abs_diff": round(statistics.median(rsi_xcheck), 3) if rsi_xcheck else None,
       "p95_abs_diff": round(float(np.percentile(rsi_xcheck, 95)), 3) if rsi_xcheck else None,
       "note": "trace 为 V1.1.2 前快照（0/0 窗 NaN 未填50），差异主要来自该已修类；中位数应≈0"}

def c6_flip(base_key, delta_mode):
    """delta_mode: '1m'=+4.0 / 'both'=+4.0(+10.0 若5m纯上涨)。返回 (sell_flips, buy_flips)。
    买侧代理：bs' = base_bs − base买因子['RSI超卖']（盲 tick RSI=100 → 超卖贡献归零，精确扣除）；
    仲裁对侧 ss' 取 +delta 上限（对买翻转是保守下界）。"""
    recs, buys = [], []
    for e in pu_events:
        base = e[base_key]
        if base is None:
            continue
        delta = 4.0 + (10.0 if (delta_mode == "both" and e["pu5"]) else 0.0)
        ssp = base["ss"] + delta
        bsp = base["bs"] - float(base["bf"].get("RSI超卖", 0) or 0)
        # 卖侧翻转
        if ssp >= ENGINE_SELL_TH and ssp > base["bs"]:
            req = (NOTIFY_SELL_EARLY if int(e["hm"][:2]) < 10
                   else NOTIFY_SELL.get(e["code"], NOTIFY_SELL_DEFAULT))
            if ssp >= req:
                recs.append({**e, "ssp": round(ssp, 1), "bsp": base["bs"], "delta": delta})
        # 买侧翻转（引擎阈用基底 tick 的 bth；盲 tick 自身 bth 不可知，近似）
        if bsp >= base["bth"] and bsp > ssp and bsp >= NOTIFY_BUY:
            buys.append({**e, "bsp": round(bsp, 1)})
    # 5min 段去重（每股每方向）
    def dedup(rs):
        kept, last = [], {}
        for r in sorted(rs, key=lambda r: r["dt"]):
            lt = last.get(r["code"])
            if lt is not None and (r["dt"] - lt).total_seconds() < SEG_DEDUP_MIN * 60:
                continue
            kept.append(r)
            last[r["code"]] = r["dt"]
        return kept
    kept, kept_buys = dedup(recs), dedup(buys)
    for r in kept:
        res, _ = settle_one("SELL_HIGH", r["code"], r["date"], r["hm"], r["price"])
        r["res"] = res
        r["dtype"] = daytype.get((r["code"], r["date"]), "unknown")
    for r in kept_buys:
        res, _ = settle_one("BUY_LOW", r["code"], r["date"], r["hm"], r["price"])
        r["res"] = res
        r["dtype"] = daytype.get((r["code"], r["date"]), "unknown")
    return kept, kept_buys

c6 = {"blind_ticks": len(blind_ticks), "blind_stock_days": len(blind_days),
      "blind_class": dict(c6_stats), "pure_up_events": len(pu_events),
      "pu5_share": round(sum(1 for e in pu_events if e["pu5"]) / len(pu_events), 4) if pu_events else None,
      "scenarios": {}}
for bk in ("base_b", "base_a"):
    for dm in ("1m", "both"):
        kept, kept_buys = c6_flip(bk, dm)
        bull_kept = [r for r in kept if r["dtype"] == "bull_day"]
        c6["scenarios"][f"{bk}_{dm}"] = {
            "flips_recorded": len(kept),
            "settle": wr_of(kept),
            "bull_day": wr_of(bull_kept),
            "buy_flips_recorded": len(kept_buys),
            "buy_settle": wr_of(kept_buys),
            "density_add": round(len(kept) / stock_days, 3),
            "x_c2": {  # 与 C2 交互：bull 日翻转信号被 T+5b/T+10b 再压制的数量
                "suppressed_by_T+5b": sum(1 for r in bull_kept if r["ssp"] < 60),
                "suppressed_by_T+10b": sum(1 for r in bull_kept if r["ssp"] < 65),
            },
        }
        print(f"C6 {bk}_{dm}:", json.dumps(c6["scenarios"][f"{bk}_{dm}"], ensure_ascii=False))

# ========== 汇总输出 ==========
result = {
    "meta": {
        "purpose": "W32 §3 离线筛查：C1 接回解耦 / C2 卖侧 bull 日 / C6 B 类语义",
        "control": "e1_final/T36b（当前生产口径）",
        "baseline": CONTROL,
        "stock_days": stock_days, "dates": f"{sample_dates[0]}~{sample_dates[-1]}", "ticks": n_ticks,
        "codes": CODES,
        "two_layer_coupling": {
            "C1": "接回绕过引擎门控后仍须过记录层 notify_buy=36（bth 已含接回 −5 放松，[31,36) 带被 notify 吸收）",
            "C2": "卖侧耦合方向与买侧相反：引擎 42+10=52 < notify 51/55 → a 档（引擎-only）信号零变化；"
                  "一切效果在 notify 层（t55→60/65，早盘 65→70/75）。harness 已有 T_NOTIFY_SELL 注入"
                  "（harness_backtest.py:556），但 bull 日条件上浮需新增开关（如 T_NOTIFY_SELL_BULL_UPLIFT）",
            "C6": "指标层语义变更（indicators.py 纯上涨窗 NaN→100），两层无耦合；"
                  "1m RSI NaN 致整分失明（signal_engine.py:699 or-50 对 NaN 无效），"
                  "5m RSI NaN 经比较运算优雅降级为 0 不致盲",
        },
        "unmodelable": "状态机二阶效应（新增接回/少卖引发的轨迹变化）离线不可建模；"
                       "C6 盲 tick 基底分用邻近非盲 tick 代理（前后双场景给区间）；"
                       "E2/E1 已验证阈值类变更离线→实测可迁移（误差 0.24pp）",
        "qty_approx": f"增量买入 qty=每股正qty中位 {MEDIAN_QTY}，闭环 PnL 为量级参考",
    },
    "self_check": {"SC1_control_reproduction": sc1, "SC2_c2_t0_identity": sc2, "SC3_rsi_xcheck": sc3},
    "C1_buyback_decouple": c1,
    "C2_bull_day_sell": c2,
    "C6_bclass_rsi": c6,
}
with open(OUT / "w32_screen.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)
print("\nJSON written:", OUT / "w32_screen.json")
