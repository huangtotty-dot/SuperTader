# -*- coding: utf-8 -*-
"""
attr_000988.py — 000988 双向压缩归因专项(分析-only, 不动生产, 不跑全管线)
数据: e1_final/{T36b,T30b} + e2_variant_a/control 合并信号 + parts decision_trace + 统一分钟库
任务: ①信号结构分解(日型/因子/时段/贴线) ②质量分解(闭环盈亏结构/小盈大亏/卖侧对称)
     ③波动率结构(5分振幅/日振幅/ATR) ⑤处置方案离线预估(复用现有信号+闭环重算)
产物: t_io/validation/attr_000988/attr_000988.json + 控制台表
"""
import json, math, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
SNAP = BASE / "t_io/minute_snapshots_ts"
OUT = BASE / "t_io/validation/attr_000988"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE))
import pandas as pd  # noqa: E402
from harness_backtest import classify_day_type  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
POOL = [c for c in CODES if c != "000988"]
GROUPS = {
    "control": (BASE / "t_io/validation/e2_variant_a/control/signals.jsonl",
                BASE / "t_io/validation/e2_variant_a/parts", "control"),
    "T36b": (BASE / "t_io/validation/e1_final/T36b/signals.jsonl",
             BASE / "t_io/validation/e1_final/parts", "T36b"),
    "T30b": (BASE / "t_io/validation/e1_final/T30b/signals.jsonl",
             BASE / "t_io/validation/e1_final/parts", "T30b"),
}

# ---------- 0. 加载信号 + 日型 + trace 索引 ----------
sigs = {g: [json.loads(l) for l in open(p, encoding="utf-8")] for g, (p, _, _) in GROUPS.items()}

dt = {}
for c in CODES:
    for fp in SNAP.glob(f"*/*/{c}_*.json"):
        d = json.load(open(fp, encoding="utf-8"))
        if d["date"] < "2026-03-16":
            continue
        bars = d["bars"]
        if not bars:
            continue
        df = pd.DataFrame(bars)
        df["time"] = pd.to_datetime(df["time"])
        dt[(c, d["date"])] = classify_day_type(df)
print("daytype:", Counter(dt.values()))

# trace 索引: (code, ts16) -> {bth, sth, buy_factors, sell_factors}
trace_idx = {}
for g, (_, parts, prefix) in GROUPS.items():
    if g == "T30b":
        continue  # T30b 仅处置方案对照, 不join trace(结构同T36b)
    for part in sorted(parts.iterdir()):
        if not part.name.startswith(prefix + "_"):
            continue
        for fp in part.glob("decision_trace_*.jsonl"):
            for line in open(fp, encoding="utf-8"):
                r = json.loads(line)
                trace_idx[(g, r["code"], r["scan_time"][:16])] = {
                    "bth": r.get("buy_threshold"), "sth": r.get("sell_threshold"),
                    "bf": r.get("buy_factors") or {}, "sf": r.get("sell_factors") or {}}
print("trace_idx:", len(trace_idx))

def session(ts):
    hhmm = int(ts[11:13]) * 100 + int(ts[14:16])
    if hhmm < 1000:
        return "早盘(<10:00)"
    if hhmm >= 1430:
        return "尾盘(>=14:30)"
    return "盘中"

def wr(evts, key="settle_result"):
    w = sum(1 for e in evts if e[key] == "WIN")
    f = sum(1 for e in evts if e[key] == "FAIL")
    return {"n": len(evts), "settled": w + f, "wr": round(w / (w + f), 4) if (w + f) else None}

# ---------- 1. 信号结构分解 ----------
struct = {}
for g in ("control", "T36b"):
    buys = [s for s in sigs[g] if s["action"] == "BUY_LOW"]
    sells = [s for s in sigs[g] if s["action"] == "SELL_HIGH"]
    for label, codes in (("000988", ["000988"]), ("POOL", POOL)):
        b = [s for s in buys if s["code"] in codes]
        se = [s for s in sells if s["code"] in codes]
        # 贴线: 记录层(notify) 与 引擎层(trace bth)
        near_ntf = sum(1 for s in b if s["buy_score"] - s["threshold"] <= 2) / max(1, len(b))
        eng_diffs, near_eng = [], 0
        for s in b:
            t = trace_idx.get((g, s["code"], s["ts"][:16]))
            if t and t["bth"] is not None:
                eng_diffs.append(s["buy_score"] - t["bth"])
                if s["buy_score"] - t["bth"] <= 2:
                    near_eng += 1
        near_eng_r = near_eng / max(1, len(eng_diffs))
        # 因子均贡献(buys)
        fac = defaultdict(list)
        for s in b:
            t = trace_idx.get((g, s["code"], s["ts"][:16]))
            if t:
                for k, v in t["bf"].items():
                    fac[k].append(v)
        fac_top = sorted(((k, round(statistics.mean(v), 2), len(v)) for k, v in fac.items()),
                         key=lambda x: -x[1])[:8]
        # 卖侧贴线
        sell_near = sum(1 for s in se if s["sell_score"] - s["threshold"] <= 2) / max(1, len(se))
        struct[f"{g}:{label}"] = {
            "buy_n": len(b),
            "buy_by_dtype": {t: sum(1 for s in b if dt.get((s["code"], s["ts"][:10])) == t)
                             for t in ("bull_day", "bear_day", "reversal_day", "chop_day")},
            "buy_by_session": {sess: sum(1 for s in b if session(s["ts"]) == sess)
                               for sess in ("早盘(<10:00)", "盘中", "尾盘(>=14:30)")},
            "buy_near_notify_±2": round(near_ntf, 4),
            "buy_near_engine_±2": round(near_eng_r, 4),
            "buy_engine_diff_mean": round(statistics.mean(eng_diffs), 2) if eng_diffs else None,
            "buy_score_mean": round(statistics.mean([s["buy_score"] for s in b]), 2) if b else None,
            "buy_factor_top": fac_top,
            "sell_n": len(se),
            "sell_near_notify_±2": round(sell_near, 4),
            "sell_score_mean": round(statistics.mean([s["sell_score"] for s in se]), 2) if se else None,
            "buy_wr": wr(b), "sell_wr": wr(se),
        }
        print(f"{g}:{label} buys={len(b)} near_eng={near_eng_r:.2f} score_mean={struct[f'{g}:{label}']['buy_score_mean']}")

# ---------- 2. 质量分解(闭环 pair 级, 复制 compute_closed_loop FIFO 并保留 pair 明细) ----------
def closed_pairs(signals):
    from collections import deque
    commission, stamp = 0.00015, 0.0005
    by_date = defaultdict(list)
    for s in signals:
        by_date[s["ts"][:10]].append(s)
    pairs = []
    for date_str, day_sigs in sorted(by_date.items()):
        day_pos = defaultdict(lambda: {"long": deque(), "short": deque()})
        for s in sorted(day_sigs, key=lambda x: x["ts"]):
            code, action, price = s["code"], s["action"], s["price"]
            qty = s.get("qty", 100)
            dp = day_pos[code]
            if action in ("BUY_LOW", "ADD_POS"):
                if dp["short"]:
                    e0 = dp["short"].popleft()
                    pnl = (e0["price"] - price) * qty
                    fee = e0["price"] * qty * (commission + stamp) + price * qty * commission
                    pairs.append({"code": code, "date": date_str, "type": "short_close", "pnl": round(pnl - fee, 2)})
                else:
                    dp["long"].append({"price": price})
            elif action == "SELL_HIGH":
                if dp["long"]:
                    e0 = dp["long"].popleft()
                    pnl = (price - e0["price"]) * qty
                    fee = e0["price"] * qty * commission + price * qty * (commission + stamp)
                    pairs.append({"code": code, "date": date_str, "type": "long_close", "pnl": round(pnl - fee, 2)})
                else:
                    dp["short"].append({"price": price})
    return pairs

def pair_stats(pairs, codes):
    sub = [p for p in pairs if p["code"] in codes]
    w = [p["pnl"] for p in sub if p["pnl"] > 0]
    l = [p["pnl"] for p in sub if p["pnl"] <= 0]
    gw, gl = sum(w), -sum(l)
    return {"pairs": len(sub), "pair_wr": round(len(w) / len(sub), 4) if sub else None,
            "avg_win": round(statistics.mean(w), 2) if w else None,
            "avg_loss": round(statistics.mean(l), 2) if l else None,
            "net_pnl": round(sum(p["pnl"] for p in sub), 2),
            "profit_factor": round(gw / gl, 3) if gl > 0 else None,
            "盈亏比": round(statistics.mean(w) / abs(statistics.mean(l)), 3) if w and l else None}

quality = {}
for g in ("control", "T36b", "T30b"):
    prs = closed_pairs(sigs[g])
    quality[g] = {"000988": pair_stats(prs, ["000988"]), "POOL": pair_stats(prs, POOL),
                  "ALL": pair_stats(prs, CODES)}
    print(f"{g} 000988: {quality[g]['000988']} | POOL: {quality[g]['POOL']}")

# ---------- 3. 波动率结构 ----------
vol = {}
for c in CODES:
    daily = {}
    for fp in SNAP.glob(f"*/*/{c}_*.json"):
        d = json.load(open(fp, encoding="utf-8"))
        bars = d["bars"]
        if bars:
            daily[d["date"]] = bars
    dates = sorted(d for d in daily if d >= "2026-03-16")
    amp_day, amp_5m, tr_list, closes = [], [], [], []
    prev_close = None
    for d in dates:
        bars = daily[d]
        H = max(b["high"] for b in bars); L = min(b["low"] for b in bars)
        C = bars[-1]["close"]
        if prev_close:
            amp_day.append((H - L) / prev_close)
            tr_list.append(max(H - L, abs(H - prev_close), abs(L - prev_close)) / prev_close)
        # 5分钟振幅
        for i in range(0, len(bars) - 4, 5):
            seg = bars[i:i + 5]
            h5 = max(b["high"] for b in seg); l5 = min(b["low"] for b in seg)
            c5 = seg[-1]["close"]
            if c5 > 0:
                amp_5m.append((h5 - l5) / c5)
        closes.append(C)
        prev_close = C
    vol[c] = {"price_mean": round(statistics.mean(closes), 2),
              "日振幅%": round(statistics.mean(amp_day) * 100, 3),
              "日TR%(≈ATR口径)": round(statistics.mean(tr_list) * 100, 3),
              "5分振幅%": round(statistics.mean(amp_5m) * 100, 3),
              "5分振幅中位%": round(statistics.median(amp_5m) * 100, 3),
              "days": len(dates)}
print("vol:", json.dumps(vol, ensure_ascii=False))

# ---------- 5. 处置方案离线预估(基于 T36b 信号集, 重算闸门指标) ----------
def gate_metrics(signals, label):
    buys = [s for s in signals if s["action"] == "BUY_LOW"]
    sells = [s for s in signals if s["action"] == "SELL_HIGH"]
    bear = [s for s in buys if dt.get((s["code"], s["ts"][:10])) == "bear_day"]
    prs = closed_pairs(signals)
    pnl = round(sum(p["pnl"] for p in prs), 2)
    bwr = wr(buys); swr = wr(sells); bearwr = wr(bear)
    return {"label": label, "buy_n": len(buys), "buy_wr": bwr["wr"],
            "sell_n": len(sells), "sell_wr": swr["wr"],
            "pairs": len(prs), "pnl": pnl,
            "bear_n": bearwr["settled"], "bear_wr": bearwr["wr"],
            "density": round(len(buys) / 450, 3),
            "gate": {"①买wr>=0.49": (bwr["wr"] or 0) >= 0.49,
                     "②对>=41&PnL>=221.31": len(prs) >= 41 and pnl >= 221.31,
                     "③卖侧退化<=1pp": (swr["wr"] or 0) >= 0.4667 - 0.01,
                     "④阴跌>=0.30": (bearwr["wr"] or 0) >= 0.30,
                     "⑤密度∈[0.30,0.60]": 0.30 <= len(buys) / 450 <= 0.60}}

t36 = sigs["T36b"]
options = [gate_metrics(t36, "T36b现状(引擎36+notify36全池)")]
# A40/A42: 000988 个股级回调(引擎侧用 trace bth 近似: 生存条件 buy_score >= X+(bth-36); notify侧 buy_score >= X 或 43)
for X, ntf in ((40, 40), (42, 43)):
    kept = []
    for s in t36:
        if s["action"] == "BUY_LOW" and s["code"] == "000988":
            t = trace_idx.get(("T36b", s["code"], s["ts"][:16]))
            bth = t["bth"] if t and t["bth"] is not None else 36.0
            if not (s["buy_score"] >= X + (bth - 36.0) and s["buy_score"] >= ntf):
                continue
        kept.append(s)
    options.append(gate_metrics(kept, f"A: 000988回调引擎{X}+notify{ntf}"))
# B: 阴跌日买入黑名单(仅000988)
kept = [s for s in t36 if not (s["action"] == "BUY_LOW" and s["code"] == "000988"
        and dt.get((s["code"], s["ts"][:10])) == "bear_day")]
options.append(gate_metrics(kept, "B: 000988阴跌日买入黑名单"))
# B2: 阴跌日黑名单(全池)
kept = [s for s in t36 if not (s["action"] == "BUY_LOW" and dt.get((s["code"], s["ts"][:10])) == "bear_day")]
options.append(gate_metrics(kept, "B2: 全池阴跌日买入黑名单"))
# C: 000988 买侧全禁(只做卖)
kept = [s for s in t36 if not (s["action"] == "BUY_LOW" and s["code"] == "000988")]
options.append(gate_metrics(kept, "C: 000988只做卖(买侧全禁)"))

for o in options:
    print(o["label"], {k: o[k] for k in ("buy_n", "buy_wr", "pairs", "pnl", "bear_wr", "density")}, o["gate"])

# ---------- 输出 ----------
result = {"meta": {"purpose": "000988双向压缩归因(分析-only)", "data": "e1_final/{T36b,T30b}+e2_variant_a/control 合并信号+trace+统一分钟库",
                    "stock_days": 450},
          "struct": struct, "quality": quality, "volatility": vol, "options": options}
with open(OUT / "attr_000988.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)
print("JSON written:", OUT / "attr_000988.json")
