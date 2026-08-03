# -*- coding: utf-8 -*-
"""
daily_review.py — 三层复盘体系·日复盘系统自动数据项(§1 第1步 + §5 观察项)
分析-only, 冻结参数。用法: python daily_review.py [--date 2026-08-03]
数据源: t_io/traces/{decision_trace,shadow_signals,preopen_trace,data_quality}_DATE.jsonl
        t_io/logs/t_trader_sys_DATE.log, t_io/logs/closure_audit.jsonl
日型口径: harness_backtest.classify_day_type 的 close-only 近似(生产无分钟OHLC落盘, trace仅tick价)
产物: t_io/validation/daily_review/daily_review_DATE.json + 控制台摘要
"""
import argparse, json, math, re, sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
CODES = ["000988", "588170", "600176", "600481", "603667"]
NAMES = {"000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
         "600481": "双良节能", "603667": "五洲新春"}

p = argparse.ArgumentParser()
p.add_argument("--date", default="2026-08-03")
DATE = p.parse_args().date
OUT = BASE / "t_io/validation/daily_review"
OUT.mkdir(parents=True, exist_ok=True)

def fnum(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))

# ---------- 1. decision_trace: 信号/极值/振幅/日型/NaN ----------
trace_fp = BASE / f"t_io/traces/decision_trace_{DATE}.jsonl"
ticks = defaultdict(list)
decisions = defaultdict(list)   # code -> [(ts, action, score)]
nan_ticks = Counter()
for line in open(trace_fp, encoding="utf-8"):
    r = json.loads(line)
    c = r["code"]
    ticks[c].append(r)
    bs, ss = r.get("buy_score"), r.get("sell_score")
    if not fnum(bs) and not fnum(ss):
        nan_ticks[c] += 1
    d = r.get("decision")
    if d in ("BUY_LOW", "SELL_HIGH"):
        decisions[c].append((r["scan_time"], d, bs if d == "BUY_LOW" else ss,
                             r.get("price"), r.get("buy_block") or [], r.get("sell_block") or []))

def day_profile(rs):
    """close-only 近似 classify_day_type(口径: harness_backtest.py:170-196)"""
    prices = [float(r["price"]) for r in rs if fnum(r.get("price"))]
    if len(prices) < 30:
        return {"day_type": "unknown"}
    o, cl = prices[0], prices[-1]
    H, L = max(prices), min(prices)
    day_ret = (cl - o) / o
    avg = sum(prices) / len(prices)
    above = sum(1 for x in prices if x > avg) / len(prices)
    fh = prices[: max(1, len(prices) // 4)]  # 近似首小时(生产tick约30s一根≈全天1/8, 取1/4保守)
    fh_ret = (fh[-1] - fh[0]) / fh[0]
    reversed_dir = (fh_ret > 0.003 and day_ret < -0.005) or (fh_ret < -0.003 and day_ret > 0.005)
    if reversed_dir and abs(day_ret) >= 0.008:
        dtype = "reversal_day"
    elif day_ret >= 0.01 and above >= 0.55:
        dtype = "bull_day"
    elif day_ret <= -0.01 and above <= 0.45:
        dtype = "bear_day"
    else:
        dtype = "chop_day"
    return {"open": round(o, 3), "close": round(cl, 3), "high": round(H, 3), "low": round(L, 3),
            "day_ret%": round(day_ret * 100, 2), "振幅%": round((H - L) / o * 100, 2),
            "day_type": dtype, "above_avg_ratio": round(above, 3)}

prof = {c: day_profile(ticks[c]) for c in CODES}

def valid_max(rs, key):
    vals = [r[key] for r in rs if fnum(r.get(key))]
    return round(max(vals), 1) if vals else None

sig_stat = {}
for c in CODES:
    dec = decisions[c]
    buys = [d for d in dec if d[1] == "BUY_LOW"]
    sells = [d for d in dec if d[1] == "SELL_HIGH"]
    sig_stat[c] = {"ticks": len(ticks[c]), "nan_ticks": nan_ticks[c],
                   "buy_signals": len(buys), "sell_signals": len(sells),
                   "buy_first_last": (buys[0][0][11:16], buys[-1][0][11:16]) if buys else None,
                   "sell_first_last": (sells[0][0][11:16], sells[-1][0][11:16]) if sells else None,
                   "max_buy_score": valid_max(ticks[c], "buy_score"),
                   "max_sell_score": valid_max(ticks[c], "sell_score"),
                   **prof[c]}

# ---------- 2. shadow_signals: ±3 分近阈漏单 ----------
shadow_fp = BASE / f"t_io/traces/shadow_signals_{DATE}.jsonl"
shadow_near = defaultdict(list)
shadow_total = 0
if shadow_fp.exists():
    for line in open(trace_fp if False else shadow_fp, encoding="utf-8"):
        r = json.loads(line)
        shadow_total += 1
        db, ds = r.get("distance_to_buy_threshold"), r.get("distance_to_sell_threshold")
        near_buy = fnum(db) and abs(db) <= 3
        near_sell = fnum(ds) and abs(ds) <= 3
        if near_buy or near_sell:
            shadow_near[r["code"]].append({
                "ts": r["scan_time"][11:16], "action": r.get("action"),
                "buy_score": r.get("buy_score"), "sell_score": r.get("sell_score"),
                "dist_buy": db, "dist_sell": ds, "miss_reason": r.get("miss_reason")})

def shadow_compact(evts):
    """按 (action, miss_reason) 聚合: 时段范围+条数+最近距离"""
    out = []
    for (act, mr), grp in defaultdict(list, {}).items():
        pass
    groups = defaultdict(list)
    for e in evts:
        groups[(e["action"], e["miss_reason"])].append(e)
    for (act, mr), g in groups.items():
        dists = [abs(e["dist_buy"]) for e in g if fnum(e["dist_buy"]) and abs(e["dist_buy"]) <= 3] + \
                [abs(e["dist_sell"]) for e in g if fnum(e["dist_sell"]) and abs(e["dist_sell"]) <= 3]
        out.append({"action": act, "miss_reason": mr, "n": len(g),
                    "span": f"{g[0]['ts']}~{g[-1]['ts']}", "min_dist": round(min(dists), 1) if dists else None})
    return sorted(out, key=lambda x: (x["min_dist"] if x["min_dist"] is not None else 99))

shadow_report = {c: shadow_compact(shadow_near[c]) for c in CODES if shadow_near[c]}

# ---------- 3. 日志: 仓控拦截/静默/实际推送/收盘同步 ----------
log_fp = BASE / f"t_io/logs/t_trader_sys_{DATE}.log"
suppress, silent_sell, pushes, eod = defaultdict(list), defaultdict(list), [], []
re_sup = re.compile(r"^(\d{2}:\d{2}:\d{2}).*🛑 (\d{6}) (BUY_LOW|SELL_HIGH)信号达标\((\d+)分\)但仓控可交易量为0")
re_sil = re.compile(r"^(\d{2}:\d{2}:\d{2}).*📉 (\d{6}) 卖出信号得分(\d+)分，低于.*阈值(\d+)分，静默")
re_push = re.compile(r"^(\d{2}:\d{2}:\d{2}).*飞书消息已成功送达: .+?\((\d{6})\) (BUY_LOW|SELL_HIGH)")
re_sync = re.compile(r"收盘同步 (.+?\((\d{6})\)): qty (\d+)→(\d+), t_qty (\d+)→(\d+)")
for line in open(log_fp, encoding="utf-8", errors="replace"):
    m = re_sup.search(line)
    if m:
        suppress[m.group(2)].append({"ts": m.group(1), "action": m.group(3), "score": int(m.group(4))})
        continue
    m = re_sil.search(line)
    if m:
        silent_sell[m.group(2)].append({"ts": m.group(1), "score": int(m.group(3)), "th": int(m.group(4))})
        continue
    m = re_push.search(line)
    if m:
        pushes.append({"ts": m.group(1), "code": m.group(2), "action": m.group(3)})
        continue
    m = re_sync.search(line)
    if m:
        eod.append({"code": m.group(2), "qty_from": int(m.group(3)), "qty_to": int(m.group(4))})
suppress, silent_sell = dict(suppress), dict(silent_sell)

# ---------- 4. 闭环(closure_audit 当日) ----------
audit_today = None
for line in open(BASE / "t_io/logs/closure_audit.jsonl", encoding="utf-8"):
    r = json.loads(line)
    if r.get("date") == DATE:
        audit_today = r
closed = {}
if audit_today:
    for d in audit_today["details"]:
        closed[d["code"]] = {k: d[k] for k in ("sold", "bought", "unrebuilt", "est_pnl", "qty_diff")}

# ---------- 5. 当日信号结算(close-only近似: +0.5%/-0.4%, 30 tick窗口) ----------
def settle(code, ts, action, price):
    rs = ticks[code]
    idx = next((i for i, r in enumerate(rs) if r["scan_time"] == ts), None)
    if idx is None or price in (None, 0):
        return "VOID"
    for r in rs[idx + 1: idx + 31]:
        p = float(r["price"])
        if action == "BUY_LOW":
            if p <= price * 0.996:
                return "FAIL"
            if p >= price * 1.005:
                return "WIN"
        else:
            if p >= price * 1.004:
                return "FAIL"
            if p <= price * 0.995:
                return "WIN"
    return "VOID"

settle_rows = []
for c in CODES:
    for ts, act, score, price, bb, sb in decisions[c]:
        res = settle(c, ts, act, price)
        settle_rows.append({"code": c, "ts": ts[11:16], "action": act, "score": score,
                            "price": price, "res": res, "day_type": prof[c]["day_type"]})
settle_by_code = {}
for c in CODES:
    rows = [r for r in settle_rows if r["code"] == c]
    for act in ("BUY_LOW", "SELL_HIGH"):
        sub = [r for r in rows if r["action"] == act]
        w = sum(1 for r in sub if r["res"] == "WIN")
        f = sum(1 for r in sub if r["res"] == "FAIL")
        settle_by_code.setdefault(c, {})[act] = {"n": len(sub), "wins": w, "fails": f,
                                                 "wr": round(w / (w + f), 4) if (w + f) else None}

# ---------- 6. 观察项 ----------
total_sigs = sum(v["buy_signals"] + v["sell_signals"] for v in sig_stat.values())
s988 = sig_stat["000988"]["buy_signals"] + sig_stat["000988"]["sell_signals"]
watch = {
    "#1_000988_qty冻结": {"suppressed_qty0": len(suppress["000988"]), "pushed": [p for p in pushes if p["code"] == "000988"],
                        "audit": closed.get("000988"),
                        "note": "实盘当日其买信号被仓控0拦截次数; audit sold/bought=0 表示无成交"},
    "#2_000988_bull日买入": {"day_type": prof["000988"]["day_type"], "buy_signals": sig_stat["000988"]["buy_signals"],
                            "note": "D+E未上线, bull日买入应仍触发"},
    "#3_588170_买wr": settle_by_code.get("588170", {}).get("BUY_LOW"),
    "#4_阴跌日股": {c: {"day_type": prof[c]["day_type"], "buy_settle": settle_by_code.get(c, {}).get("BUY_LOW")}
                   for c in CODES if prof[c]["day_type"] == "bear_day"},
    "#5_000988信号占比": {"n": s988, "total": total_sigs, "ratio": round(s988 / total_sigs, 4) if total_sigs else None,
                        "alert": (s988 / total_sigs) > 0.55 if total_sigs else None},
}

# ---------- 输出 ----------
result = {"date": DATE, "sig_stat": sig_stat, "shadow_total": shadow_total,
          "shadow_near_±3": shadow_report,
          "qty_freeze": {"suppressed": {c: suppress[c] for c in CODES if suppress[c]},
                          "silent_sell": {c: {"n": len(silent_sell[c]), "max_score": max((e["score"] for e in silent_sell[c]), default=None)}
                                          for c in CODES if c in silent_sell and silent_sell[c]},
                          "pushes": pushes, "eod_sync": eod},
          "closed_loop": closed, "audit_problems": audit_today["problems"] if audit_today else None,
          "settle": {"rows": settle_rows, "by_code": settle_by_code},
          "watch": watch}
with open(OUT / f"daily_review_{DATE}.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"== {DATE} 日复盘数据摘要 ==")
for c in CODES:
    s = sig_stat[c]
    print(f"{c} {NAMES[c]}: 买{s['buy_signals']}/卖{s['sell_signals']} 买max{s['max_buy_score']} 卖max{s['max_sell_score']} "
          f"振幅{s.get('振幅%')}% 日ret{s.get('day_ret%')}% {s['day_type']} nan={s['nan_ticks']}")
print("shadow_near:", {c: len(v) for c, v in shadow_near.items()})
print("suppressed:", {c: len(v) for c, v in suppress.items()}, "pushes:", pushes)
print("silent_sell:", {c: (len(v), max((e['score'] for e in v), default=None)) for c, v in silent_sell.items() if v})
print("closed:", closed)
print("settle_by_code:", json.dumps(settle_by_code, ensure_ascii=False))
print("watch:", json.dumps(watch, ensure_ascii=False, default=str)[:600])
print("JSON:", OUT / f"daily_review_{DATE}.json")
