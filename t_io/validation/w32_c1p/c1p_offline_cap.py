# -*- coding: utf-8 -*-
"""
c1p_offline_cap.py — C1' 离线 cap 模拟（W33 第一候选·第一步，成本≈0）
基于 W32 C1 全管线产物（t_io/validation/w32_c1/C1/signals.jsonl + parts traces）
① 破闸日解剖：max_per_code_day=8 的 (code,date) 当天买信号构成（ctl原有/接回增量/二阶增量）
② 两种 cap 口径六闸重算：
   口径A = 仅接回信号 cap 7/(code,date)（§4.5 原文语义）
   口径B = 全部买信号 cap 7/(code,date)（直接对齐闸门⑤统计口径）
产物：t_io/validation/w32_c1p/c1p_offline_cap.json
"""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/w32_c1"
SNAP = BASE / "t_io/minute_snapshots_ts"
OUTDIR = BASE / "t_io/validation/w32_c1p"
OUTDIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "t_io/validation"))
import pandas as pd  # noqa: E402
from harness_backtest import classify_day_type, compute_closed_loop  # noqa: E402
from run_ab_expanded import SEGS  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
STOCK_DAYS = 450
CTL_SELL_WR = 0.4638  # gates_verdict.json 基线

ctl_sigs = [json.loads(l) for l in open(ROOT / "ctl/signals.jsonl", encoding="utf-8") if l.strip()]
c1_sigs = [json.loads(l) for l in open(ROOT / "C1/signals.jsonl", encoding="utf-8") if l.strip()]
holdings = json.load(open(ROOT / "holdings_snapshot_3d80810.json", encoding="utf-8"))
hmap = {k.split("_")[0]: v for k, v in holdings.items()}

# ---- 日型（与 w32_gates 同口径）----
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

# ---- C1 trace map（接回属性识别，与 w32_gates 同口径）----
def load_trace_map(group):
    m = {}
    for code in CODES:
        for s, e in SEGS:
            d = ROOT / "parts" / f"{group}_{code}_{s.replace('-', '')}"
            if not d.exists():
                continue
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
                        "bf": frozenset((r.get("buy_factors") or {}).keys()),
                    }
    return m

print("loading C1 traces ...")
c1_tm = load_trace_map("C1")

buys_ctl = [s for s in ctl_sigs if s["action"] == "BUY_LOW"]
buys_c1 = [s for s in c1_sigs if s["action"] == "BUY_LOW"]
ctl_buy_keys = {(s["code"], s["ts"][:16]) for s in buys_ctl}

def is_buyback(s):
    r = c1_tm.get((s["code"], s["ts"][:16]))
    return bool(r and any(k.startswith("接回追踪") for k in r["bf"]))

def is_incr(s):
    return (s["code"], s["ts"][:16]) not in ctl_buy_keys

# ---- ① 破闸日解剖 ----
per_cd = Counter((s["code"], s["ts"][:10]) for s in buys_c1)
breach = sorted([(k, v) for k, v in per_cd.items() if v > 7], key=lambda x: -x[1])
anatomy = []
for (code, date), n in breach:
    day_buys = sorted([s for s in buys_c1 if s["code"] == code and s["ts"][:10] == date],
                      key=lambda s: s["ts"])
    rows = [{
        "ts": s["ts"], "settle": s["settle_result"], "price": s["price"],
        "buy_score": s["buy_score"],
        "kind": ("buyback" if is_buyback(s) else ("incr_2nd" if is_incr(s) else "ctl_orig")),
    } for s in day_buys]
    anatomy.append({"code": code, "date": date, "n": n,
                    "n_buyback": sum(1 for r in rows if r["kind"] == "buyback"),
                    "n_incr_2nd": sum(1 for r in rows if r["kind"] == "incr_2nd"),
                    "n_ctl_orig": sum(1 for r in rows if r["kind"] == "ctl_orig"),
                    "signals": rows})

per_cd_ctl = Counter((s["code"], s["ts"][:10]) for s in buys_ctl)
max_ctl = max(per_cd_ctl.values()) if per_cd_ctl else 0
ctl_over7 = {f"{k[0]}_{k[1]}": v for k, v in per_cd_ctl.items() if v > 7}

# ---- ② cap 模拟 ----
def wr(sub):
    w = sum(1 for s in sub if s["settle_result"] == "WIN")
    f = sum(1 for s in sub if s["settle_result"] == "FAIL")
    return {"n": len(sub), "wins": w, "fails": f,
            "wr": round(w / (w + f), 4) if (w + f) else None}

def apply_cap(sigs, mode):
    """mode A: 仅接回信号 cap 7/(code,date)；mode B: 全部买 cap 7。按时间序保留前 7。"""
    buys = sorted([s for s in sigs if s["action"] == "BUY_LOW"],
                  key=lambda s: (s["code"], s["ts"][:10], s["ts"]))
    others = [s for s in sigs if s["action"] != "BUY_LOW"]
    cnt = defaultdict(int)
    kept, dropped = [], []
    for s in buys:
        key = (s["code"], s["ts"][:10])
        if mode == "A":
            if is_buyback(s):
                cnt[key] += 1
                if cnt[key] > 7:
                    dropped.append(s)
                    continue
        else:
            cnt[key] += 1
            if cnt[key] > 7:
                dropped.append(s)
                continue
        kept.append(s)
    return kept + others, dropped

def six_gates(sigs, label):
    buys = [s for s in sigs if s["action"] == "BUY_LOW"]
    sells = [s for s in sigs if s["action"] == "SELL_HIGH"]
    cl = compute_closed_loop(sigs, hmap)
    bear_buys = [s for s in buys if daytype.get((s["code"], s["ts"][:10])) == "bear_day"]
    bb_buys = [s for s in buys if is_buyback(s)]
    bear_bb = [s for s in bb_buys if daytype.get((s["code"], s["ts"][:10])) == "bear_day"]
    pcd = Counter((s["code"], s["ts"][:10]) for s in buys)
    density = round(len(buys) / STOCK_DAYS, 3)
    mx = max(pcd.values()) if pcd else 0
    g1 = wr(buys)
    g4 = wr(bear_buys)
    g6 = wr(bear_bb)
    sell_wr = wr(sells)["wr"]
    gates = {
        "①总买wr>=0.4788": bool(g1["wr"] is not None and g1["wr"] >= 0.4788),
        "②闭环>=47对且PnL>=+252.98": bool(cl["total_closed_pairs"] >= 47 and cl["total_net_pnl"] >= 252.98),
        "③卖侧退化<=1pp": bool(sell_wr is not None and (sell_wr - CTL_SELL_WR) >= -0.01),
        "④阴跌日买wr>=0.30": bool(g4["wr"] is not None and g4["wr"] >= 0.30),
        "⑤买密度∈[0.30,0.60]且单股<=7/日": bool(0.30 <= density <= 0.60 and mx <= 7),
        "⑥bear日接回wr>=0.35": bool(g6["wr"] is None or g6["wr"] >= 0.35),
    }
    return {
        "label": label,
        "buy": g1, "sell_wr": sell_wr,
        "cl": {"pairs": cl["total_closed_pairs"], "pnl": cl["total_net_pnl"]},
        "buy_density": density, "max_per_code_day": mx,
        "bear_buy": g4, "buyback_n": len(bb_buys), "bear_buyback": g6,
        "gates": gates, "all_pass": all(gates.values()),
    }

sigsA, droppedA = apply_cap(c1_sigs, "A")
sigsB, droppedB = apply_cap(c1_sigs, "B")

res = {
    "breach_anatomy": anatomy,
    "ctl_baseline_max_per_code_day": max_ctl,
    "ctl_days_over7": ctl_over7,
    "C1_no_cap_recheck": six_gates(c1_sigs, "C1 原始（破闸复现校验）"),
    "C1p_modeA_buyback_cap7": {
        **six_gates(sigsA, "C1' 口径A：仅接回cap7"),
        "dropped": [{"ts": s["ts"], "code": s["code"], "settle": s["settle_result"],
                     "price": s["price"], "buy_score": s["buy_score"]} for s in droppedA]},
    "C1p_modeB_allbuy_cap7": {
        **six_gates(sigsB, "C1' 口径B：全部买cap7"),
        "dropped": [{"ts": s["ts"], "code": s["code"], "settle": s["settle_result"],
                     "price": s["price"], "buy_score": s["buy_score"]} for s in droppedB]},
}
json.dump(res, open(OUTDIR / "c1p_offline_cap.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2, default=str)
print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
