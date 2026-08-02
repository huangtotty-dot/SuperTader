# -*- coding: utf-8 -*-
"""
e2_daily_gate_analysis.py — E2 daily_gate 离线量化(纯分析, 零引擎改动)
背景: ①harness 硬编码 daily_ctx 缺字段(harness_backtest.py:467) → 回测门控恒锁死;
      ②生产 data_fetcher._build_daily_context_from_df 首行 PARAMS["daily_context_min_rows"] 等 7 键
      未定义 → KeyError 被吞 → daily_status="error" → 生产门控同样恒锁死(P0-D 类新实例)。
本脚本复算"键修复后世界"的门控行为:
  - daily bars = 预热期(e2_daily_gate/minute_snapshots_pre) + 样本期(minute_snapshots_ts) 分钟聚合,
    截至前一交易日(无前视); ma5_state 判定逐 tick 用 trace price(语义同生产 current_price)
  - ma5_state 规则照抄 data_fetcher.py:169-176: gap<=0.01→near_ma5_chop; price>=ma5且slope>=0→above_ma5_trend; 否则 below_ma5_weak
  - 日型 = harness_backtest.classify_day_type(日内分钟df)
产物: e2_daily_gate/e2_analysis.json + 控制台表
"""
import json, math, sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
PRE = BASE / "t_io/validation/e2_daily_gate/minute_snapshots_pre"
SNAP = BASE / "t_io/minute_snapshots_ts"
PARTS = BASE / "t_io/validation/v110_degraded/parts"
OUT = BASE / "t_io/validation/e2_daily_gate"
sys.path.insert(0, str(BASE))
import pandas as pd  # noqa: E402
from harness_backtest import settle_signal, classify_day_type  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
COOLDOWN_MIN = 30

# ---------- 1. 日线聚合(预热+样本, 无前视) ----------
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

# 每股: 日期序列 -> ma5 序列
ma5_map = {}     # code -> {date: ma5(截至该日)}
slope_map = {}   # code -> {date: ma5_slope(对6日前)}
prev_close = {}  # code -> {date: 前一日收盘}
for c in CODES:
    dates = sorted(daily[c])
    closes = [daily[c][d]["close"] for d in dates]
    ma5_map[c], slope_map[c], prev_close[c] = {}, {}, {}
    for i, d in enumerate(dates):
        if i >= 4:
            ma5_map[c][d] = sum(closes[i - 4:i + 1]) / 5
        if i >= 9:
            m5 = sum(closes[i - 4:i + 1]) / 5
            m5p = sum(closes[i - 9:i - 4]) / 5
            slope_map[c][d] = (m5 - m5p) / m5p if m5p else 0.0
        if i >= 1:
            prev_close[c][d] = closes[i - 1]

def ma5_state(code, date, price):
    """截至前一日的 ma5/slope + 当前价 -> 三态(照抄 data_fetcher.py:169-176)"""
    dates = sorted(daily[code])
    # 前一交易日
    prev = [d for d in dates if d < date]
    if len(prev) < 10:
        return "unknown", 0.0, 0.0
    pd_ = prev[-1]
    ma5 = ma5_map[code].get(pd_)
    slope = slope_map[code].get(pd_, 0.0)
    if not ma5:
        return "unknown", 0.0, 0.0
    gap = abs(price - ma5) / ma5
    if gap <= 0.01:
        return "near_ma5_chop", ma5, slope
    if price >= ma5 and slope >= 0:
        return "above_ma5_trend", ma5, slope
    return "below_ma5_weak", ma5, slope

# ---------- 2. 日型 ----------
snap_cache = {}
def day_bars(code, date):
    key = (code, date)
    if key not in snap_cache:
        fp = SNAP / date[:4] / date[5:7] / f"{code}_{date}.json"
        snap_cache[key] = json.load(open(fp, encoding="utf-8"))["bars"] if fp.exists() else []
    return snap_cache[key]

daytype = {}
for c in CODES:
    for d in sorted(set(k for k in daily[c] if k >= "2026-03-16")):
        bars = day_bars(c, d)
        if bars:
            df = pd.DataFrame(bars)
            df["time"] = pd.to_datetime(df["time"])
            daytype[(c, d)] = classify_day_type(df)
print("daytype census:", Counter(daytype.values()))

# ---------- 3. 逐 tick 复算门控 ----------
all_ticks = []          # (code, date, hm, price, buy_score, sell_score, reason)
for part in sorted(PARTS.iterdir()):
    for fp in sorted(part.glob("decision_trace_*.jsonl")):
        date = fp.stem.replace("decision_trace_", "")
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            bs = r.get("buy_score")
            if bs is None or (isinstance(bs, float) and math.isnan(bs)):
                continue
            all_ticks.append((r["code"], date, r["scan_time"][11:16], float(r["price"]),
                              float(bs), float(r.get("sell_score") or 0), r.get("decision_reason", "")))
print("ticks:", len(all_ticks))

gate_census = defaultdict(Counter)     # code -> ma5_state -> n
open_by_dtype = defaultdict(lambda: [0, 0])  # daytype -> [open, total]
for code, date, hm, price, bs, ss, reason in all_ticks:
    st, ma5, slope = ma5_state(code, date, price)
    gate_census[code][st] += 1
    opened = st in ("near_ma5_chop", "above_ma5_trend")
    dt = daytype.get((code, date), "unknown")
    open_by_dtype[dt][0] += opened
    open_by_dtype[dt][1] += 1

# ---------- 4. 被拦 buy-worthy 事件(daily_gate 23500) 质量 ----------
blocked = [t for t in all_ticks if t[6] == "HOLD_BUY_BLOCKED:daily_gate"]
print("blocked buy-worthy ticks:", len(blocked))

def dedup_settle(events, release_pred):
    """30min冷却去重 + §1.1结算; release_pred(tick_extra)->bool 决定放行"""
    out = defaultdict(lambda: {"WIN": 0, "FAIL": 0, "VOID": 0})
    byday = defaultdict(list)
    for e in events:
        byday[(e[0], e[1])].append(e)
    for (code, date), evs in byday.items():
        evs.sort(key=lambda x: x[2])
        bars = day_bars(code, date)
        t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
        last_i = -10**9
        for e in evs:
            _, _, hm, price, bs, ss, _ = e
            st, ma5, slope = ma5_state(code, date, price)
            if not release_pred(st, slope, e):
                continue
            i = t2i.get(hm)
            if i is None or i - last_i < COOLDOWN_MIN:
                continue
            last_i = i
            fut = pd.DataFrame(bars[i + 1: i + 31])
            res, _ = settle_signal("BUY_LOW", bars[i]["close"], fut)
            dt = daytype.get((code, date), "unknown")
            out[("ALL", "ALL")][res] += 1
            out[("state", st)][res] += 1
            out[("dtype", dt)][res] += 1
            out[("code", code)][res] += 1
    return out

def wr_of(table, key):
    v = table.get(key, {"WIN": 0, "FAIL": 0, "VOID": 0})
    n = v["WIN"] + v["FAIL"]
    return {"n": n, "void": v.get("VOID", 0), "wr": round(v["WIN"] / n, 4) if n else None}

# 变体放行谓词（作用于被拦事件; 修复门控本身放行 near/above 不属"被拦"分析集）
variants = {
    "repaired_gate(现状修复)": lambda st, slope, e: st in ("near_ma5_chop", "above_ma5_trend"),
    "A_弱中回升放行": lambda st, slope, e: st in ("near_ma5_chop", "above_ma5_trend") or (st == "below_ma5_weak" and slope >= 0),
    "B_深跌bypass(-2%&分>=55)": lambda st, slope, e: st in ("near_ma5_chop", "above_ma5_trend") or
        (prev_close[e[0]].get(e[1]) and (e[3] / prev_close[e[0]][e[1]] - 1) <= -0.02 and e[4] >= 55),
    "C_完全移除": lambda st, slope, e: True,
}
var_tables = {name: dedup_settle(blocked, pred) for name, pred in variants.items()}

def summarize(table):
    return {"ALL": wr_of(table, ("ALL", "ALL")),
            "by_state": {st: wr_of(table, ("state", st)) for st in ("near_ma5_chop", "above_ma5_trend", "below_ma5_weak", "unknown")},
            "by_dtype": {dt: wr_of(table, ("dtype", dt)) for dt in ("bull_day", "bear_day", "reversal_day", "chop_day", "unknown")},
            "by_code": {c: wr_of(table, ("code", c)) for c in CODES}}

# ---------- 5. 风险量化: 阴跌段最大连败 ----------
def max_consec_fails(table_events):
    return None  # 在下方逐变体重算(需事件序列)

def released_events(pred):
    seq = []
    byday = defaultdict(list)
    for e in blocked:
        byday[(e[0], e[1])].append(e)
    for (code, date), evs in sorted(byday.items()):
        evs.sort(key=lambda x: x[2])
        bars = day_bars(code, date)
        t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
        last_i = -10**9
        for e in evs:
            st, ma5, slope = ma5_state(code, e[1], e[3])
            if not pred(st, slope, e):
                continue
            i = t2i.get(e[2])
            if i is None or i - last_i < COOLDOWN_MIN:
                continue
            last_i = i
            fut = pd.DataFrame(bars[i + 1: i + 31])
            res, _ = settle_signal("BUY_LOW", bars[i]["close"], fut)
            seq.append({"code": code, "date": date, "hm": e[2], "state": st,
                        "daytype": daytype.get((code, date), "unknown"), "res": res,
                        "today_ret": round(e[3] / prev_close[code].get(date, e[3]) - 1, 4)})
    return seq

risk = {}
for name, pred in variants.items():
    seq = released_events(pred)
    bear = [s for s in seq if s["daytype"] == "bear_day"]
    deep988 = [s for s in seq if s["code"] == "000988" and s["today_ret"] <= -0.02]
    def maxcf(evts):
        m = cur = 0
        for s in evts:
            cur = cur + 1 if s["res"] == "FAIL" else 0
            m = max(m, cur)
        return m
    def wr(evts):
        n = sum(1 for s in evts if s["res"] in ("WIN", "FAIL"))
        w = sum(1 for s in evts if s["res"] == "WIN")
        return {"n": n, "wr": round(w / n, 4) if n else None, "max_consec_fail": maxcf(evts)}
    risk[name] = {"bear_day": wr(bear), "000988_深跌日(≤-2%)": wr(deep988)}

result = {
    "meta": {"blocked_ticks": len(blocked), "cooldown_min": COOLDOWN_MIN,
             "key_missing_finding": "daily_context_min_rows等7键未定义→生产get_daily_context必抛KeyError被吞→daily_status=error→生产门控恒锁死; harness硬编码缺字段同效",
             "daily_bars": "预热期分钟聚合(2025-11-10起)+样本期, 截至前一交易日无前视",
             "subcondition_truth": "修复后 daily_status==ok 与 daily_ma5>0 恒真(数据充足), 门控全部判别力=ma5_state"},
    "gate_open_rate": {
        "by_code": {c: {st: n for st, n in gate_census[c].items()} for c in CODES},
        "open_rate_by_code": {c: round((gate_census[c]["near_ma5_chop"] + gate_census[c]["above_ma5_trend"]) / sum(gate_census[c].values()), 4) for c in CODES},
        "open_rate_by_daytype": {dt: round(o / t, 4) for dt, (o, t) in open_by_dtype.items()}},
    "variant_summary": {name: summarize(t) for name, t in var_tables.items()},
    "risk": risk,
}
with open(OUT / "e2_analysis.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)
print("JSON written:", OUT / "e2_analysis.json")

print("\n== 修复后门控开闸率(全tick) ==")
print(" by_code:", result["gate_open_rate"]["open_rate_by_code"])
print(" by_daytype:", result["gate_open_rate"]["open_rate_by_daytype"])
print("\n== 变体对比(被拦23500事件, 30min去重) ==")
for name, s in result["variant_summary"].items():
    print(f" {name}: ALL={s['ALL']}")
    print(f"   state={s['by_state']}")
    print(f"   dtype={s['by_dtype']}")
print("\n== 风险 ==")
for name, r in risk.items():
    print(f" {name}: bear={r['bear_day']} 988deep={r['000988_深跌日(≤-2%)']}")
