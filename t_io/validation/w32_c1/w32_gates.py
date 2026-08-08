# -*- coding: utf-8 -*-
"""
w32_gates.py — W32 §4 六闸门逐项判定（基线=ctl 新鲜对照；T36b 历史口径为参照）
① 总买 wr ≥ 0.4788          ② 闭环 ≥47 对 且 PnL ≥ +252.98     ③ 卖侧退化 ≤1pp(vs ctl)
④ 阴跌日买 wr ≥ 0.30         ⑤ 买密度 ∈[0.30,0.60] 且单股 ≤7/日  ⑥ 白名单校验 + bear 日接回 wr ≥0.35
另报: V1.1.2 漂移量（ctl vs e1_final/T36b 历史合并口径）
产物: w32_c1/gates_verdict.json + 控制台表
"""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/w32_c1"
SNAP = BASE / "t_io/minute_snapshots_ts"
T36B_SUMMARY = BASE / "t_io/validation/e1_final/T36b/summary.json"
OUT = ROOT / "gates_verdict.json"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "t_io/validation"))
import pandas as pd  # noqa: E402
from harness_backtest import classify_day_type, compute_closed_loop  # noqa: E402
from run_ab_expanded import SEGS  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
WHITELIST = {"daily_overheated", "index_uni_down_clearance"}
STOCK_DAYS = 450

def load_signals(group):
    return [json.loads(l) for l in open(ROOT / group / "signals.jsonl", encoding="utf-8") if l.strip()]

ctl_sigs = load_signals("ctl")
c1_sigs = load_signals("C1")
holdings = json.load(open(ROOT / "holdings_snapshot_3d80810.json", encoding="utf-8"))
hmap = {k.split("_")[0]: v for k, v in holdings.items()}

# ---- 日型（与离线筛查同口径）----
dates = sorted({s["ts"][:10] for s in ctl_sigs + c1_sigs})
daytype = {}
for c in CODES:
    for d in dates:
        fp = SNAP / d[:4] / d[5:7] / f"{c}_{d}.json"
        if not fp.exists():
            continue
        bars = json.load(open(fp, encoding="utf-8"))["bars"]
        if bars:
            df = pd.DataFrame(bars)
            df["time"] = pd.to_datetime(df["time"])
            daytype[(c, d)] = classify_day_type(df)

def wr(sub):
    w = sum(1 for s in sub if s["settle_result"] == "WIN")
    f = sum(1 for s in sub if s["settle_result"] == "FAIL")
    return {"n": len(sub), "wins": w, "fails": f, "wr": round(w / (w + f), 4) if (w + f) else None}

buys_ctl = [s for s in ctl_sigs if s["action"] == "BUY_LOW"]
sells_ctl = [s for s in ctl_sigs if s["action"] == "SELL_HIGH"]
buys_c1 = [s for s in c1_sigs if s["action"] == "BUY_LOW"]
sells_c1 = [s for s in c1_sigs if s["action"] == "SELL_HIGH"]
cl_ctl = compute_closed_loop(ctl_sigs, hmap)
cl_c1 = compute_closed_loop(c1_sigs, hmap)

# ---- 增量买（C1 vs ctl，按 code+ts16）----
ctl_buy_keys = {(s["code"], s["ts"][:16]) for s in buys_ctl}
incr_buys = [s for s in buys_c1 if (s["code"], s["ts"][:16]) not in ctl_buy_keys]

# ---- trace 对比：白名单校验 + 增量买接回属性 ----
# (code, ts16) -> {"dec": ..., "block": frozenset, "bf": keys}
def load_trace_map(group):
    m = {}
    for code in CODES:
        for s, e in SEGS:
            d = ROOT / "parts" / f"{group}_{code}_{s.replace('-', '')}"
            for fp in sorted(d.glob("decision_trace_*.jsonl")):
                for line in open(fp, encoding="utf-8"):
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    bs = r.get("buy_score")
                    if bs is None or (isinstance(bs, float) and math.isnan(bs)):
                        continue
                    m[(r["code"], r["scan_time"][:16])] = {
                        "dec": r.get("decision", ""),
                        "block": frozenset(r.get("buy_block") or []),
                        "bf": frozenset((r.get("buy_factors") or {}).keys()),
                    }
    return m

print("loading traces ...")
ctl_tm = load_trace_map("ctl")
c1_tm = load_trace_map("C1")

whitelist_violations = []
changed_to_buy = 0
for key, c1r in c1_tm.items():
    cr = ctl_tm.get(key)
    if cr is None:
        continue
    if cr["dec"] != "BUY_LOW" and c1r["dec"] == "BUY_LOW":
        changed_to_buy += 1
        if not (cr["block"] <= WHITELIST):
            whitelist_violations.append({"key": key, "ctl_block": sorted(cr["block"])})
# 接回属性：增量买对应 C1 trace 的 buy_factors 含接回追踪
buyback_incr, nonbuyback_incr = [], []
for s in incr_buys:
    r = c1_tm.get((s["code"], s["ts"][:16]))
    if r and any(k.startswith("接回追踪") for k in r["bf"]):
        buyback_incr.append(s)
    else:
        nonbuyback_incr.append(s)
bear_incr = [s for s in incr_buys if daytype.get((s["code"], s["ts"][:10])) == "bear_day"]
bear_bb = [s for s in buyback_incr if daytype.get((s["code"], s["ts"][:10])) == "bear_day"]
bear_c1_buys = [s for s in buys_c1 if daytype.get((s["code"], s["ts"][:10])) == "bear_day"]

per_code_day = Counter((s["code"], s["ts"][:10]) for s in buys_c1)
max_per_code_day = max(per_code_day.values()) if per_code_day else 0

# ---- V1.1.2 漂移：ctl vs T36b 历史口径 ----
t36b = json.load(open(T36B_SUMMARY, encoding="utf-8"))
drift = {
    "buy_n": {"ctl": len(buys_ctl), "t36b": t36b["buy"]["n"]},
    "buy_wr": {"ctl": wr(buys_ctl)["wr"], "t36b": t36b["buy"]["win_rate"]},
    "sell_n": {"ctl": len(sells_ctl), "t36b": t36b["sell"]["n"]},
    "sell_wr": {"ctl": wr(sells_ctl)["wr"], "t36b": t36b["sell"]["win_rate"]},
    "cl_pairs": {"ctl": cl_ctl["total_closed_pairs"], "t36b": t36b["closed_loop"]["total_closed_pairs"]},
    "cl_pnl": {"ctl": cl_ctl["total_net_pnl"], "t36b": t36b["closed_loop"]["total_net_pnl"]},
}

gates = {}
g1 = wr(buys_c1)
gates["①总买wr>=0.4788"] = {"value": g1, "pass": bool(g1["wr"] is not None and g1["wr"] >= 0.4788)}
gates["②闭环>=47对且PnL>=+252.98"] = {
    "value": {"pairs": cl_c1["total_closed_pairs"], "pnl": cl_c1["total_net_pnl"]},
    "pass": cl_c1["total_closed_pairs"] >= 47 and cl_c1["total_net_pnl"] >= 252.98}
sell_ctl_wr, sell_c1_wr = wr(sells_ctl)["wr"], wr(sells_c1)["wr"]
d_sell = round(sell_c1_wr - sell_ctl_wr, 4) if (sell_ctl_wr is not None and sell_c1_wr is not None) else None
gates["③卖侧退化<=1pp"] = {"value": {"ctl": sell_ctl_wr, "C1": sell_c1_wr, "delta": d_sell},
                            "pass": bool(d_sell is not None and d_sell >= -0.01)}
g4 = wr(bear_c1_buys)
gates["④阴跌日买wr>=0.30"] = {"value": g4, "pass": bool(g4["wr"] is not None and g4["wr"] >= 0.30)}
density = round(len(buys_c1) / STOCK_DAYS, 3)
gates["⑤买密度∈[0.30,0.60]且单股<=7/日"] = {
    "value": {"density": density, "max_per_code_day": max_per_code_day},
    "pass": 0.30 <= density <= 0.60 and max_per_code_day <= 7}
g6b = wr(bear_bb)
gates["⑥白名单+bear日接回wr>=0.35"] = {
    "value": {"changed_to_buy_ticks": changed_to_buy, "violations": whitelist_violations[:10],
              "n_violations": len(whitelist_violations),
              "incr_buys": len(incr_buys), "buyback_attr": len(buyback_incr),
              "nonbuyback_incr": len(nonbuyback_incr), "bear_buyback": g6b},
    "pass": len(whitelist_violations) == 0 and bool(g6b["wr"] is None or g6b["wr"] >= 0.35)}

verdict = {
    "baseline_ctl": {"buy": wr(buys_ctl), "sell": sell_ctl_wr,
                     "cl": {"pairs": cl_ctl["total_closed_pairs"], "pnl": cl_ctl["total_net_pnl"]},
                     "buy_density": round(len(buys_ctl) / STOCK_DAYS, 3)},
    "variant_C1": {"buy": g1, "sell": sell_c1_wr,
                   "cl": {"pairs": cl_c1["total_closed_pairs"], "pnl": cl_c1["total_net_pnl"]},
                   "buy_density": density},
    "v112_drift_ctl_vs_t36b": drift,
    "incremental": {"incr_buys": len(incr_buys), "settle": wr(incr_buys),
                    "buyback_attr": len(buyback_incr), "bear_incr": wr(bear_incr)},
    "gates": gates,
    "all_pass": all(g["pass"] for g in gates.values()),
}
json.dump(verdict, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
print(json.dumps(verdict, ensure_ascii=False, indent=2, default=str))
print("\nVERDICT:", "ALL_GATES_PASS" if verdict["all_pass"] else "GATE_BREACH")
