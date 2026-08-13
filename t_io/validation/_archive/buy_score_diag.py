# -*- coding: utf-8 -*-
"""
buy_score_diag.py — 优化指引 §1 第一步: 买分分布诊断(观测通道, 零引擎改动)
数据源: t_io/validation/v110_degraded/parts/*/decision_trace_*.jsonl (v1.1.0 生产代码逐tick观测)
结算口径: harness_backtest.settle_signal §1.1 (BUY_LOW, 30根1分钟K, +0.5%/-0.4%), 当日future bars
关键设计:
  - NaN 显式剔除并计率(评分NaN行为已知技术债, 不混入分位数)
  - 虚拟买入候选 = buy_score>=th 且 buy_score>sell_score (对齐 can_buy 仲裁, signal_engine.py:620)
  - 分层: 全量候选 / daily_gate通过子集 (decision_reason!=HOLD_BUY_BLOCKED:daily_gate 近似)
  - 冷却去重30min模拟信号间隔, 标注为观测性预估
产物: t_io/validation/buy_score_dist/buy_score_dist.json + 控制台表
"""
import json, math, sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
PARTS = BASE / "t_io/validation/v110_degraded/parts"
SNAP = BASE / "t_io/minute_snapshots_ts"
OUT = BASE / "t_io/validation/buy_score_dist"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE))
import pandas as pd  # noqa: E402
from harness_backtest import settle_signal  # noqa: E402

THRESHOLDS = [68, 60, 52, 48, 45, 42]
SLOTS = [("早盘", "09:30", "10:00"), ("午前", "10:00", "11:30"),
         ("午后", "13:00", "14:00"), ("尾盘", "14:00", "15:00")]
COOLDOWN_MIN = 30

def pct(v, q):
    if not v:
        return None
    return round(v[min(len(v) - 1, int(q / 100 * len(v)))], 2)

def qtable(vals):
    v = sorted(vals)
    return {"n": len(v), "p50": pct(v, 50), "p75": pct(v, 75), "p90": pct(v, 90),
            "p95": pct(v, 95), "p99": pct(v, 99), "max": round(v[-1], 2) if v else None}

# ---------- 1. 采集(NaN剔除+计率) ----------
ticks = defaultdict(list)           # code -> [(date, hm, buy_score, sell_score, daily_gated)]
factors = defaultdict(lambda: defaultdict(list))
residuals = defaultdict(list)
nan_ct, tot_ct = Counter(), Counter()
reasons = Counter()
th_census = Counter()
for part in sorted(PARTS.iterdir()):
    for fp in sorted(part.glob("decision_trace_*.jsonl")):
        date = fp.stem.replace("decision_trace_", "")
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            code = r["code"]
            tot_ct[code] += 1
            reasons[r.get("decision_reason", "?")] += 1
            th_census[r.get("buy_threshold")] += 1
            bs = r.get("buy_score")
            if bs is None or (isinstance(bs, float) and math.isnan(bs)):
                nan_ct[code] += 1
                continue
            ss = r.get("sell_score")
            ss = float(ss) if ss is not None and not (isinstance(ss, float) and math.isnan(ss)) else 0.0
            gated = r.get("decision_reason") == "HOLD_BUY_BLOCKED:daily_gate"
            ticks[code].append((date, r["scan_time"][11:16], float(bs), ss, gated))
            bf = r.get("buy_factors") or {}
            for k, v in bf.items():
                factors[code][k].append(float(v))
            residuals[code].append(float(bs) - sum(float(v) for v in bf.values()))
n_ok = sum(len(v) for v in ticks.values())
print(f"ticks ok={n_ok} nan={sum(nan_ct.values())} "
      f"nan_rate={ {c: f'{nan_ct[c] / tot_ct[c]:.1%}' for c in sorted(tot_ct)} }")
print(f"trace内buy_threshold取值={dict(th_census)}  (生产全局=68, 个股43/43/40/40; trace值为harness口径)")
print("decision_reason top:", reasons.most_common(8))

# ---------- 2. 分布 ----------
dist = {"overall": qtable([t[2] for c in ticks for t in ticks[c]]),
        "per_code": {c: qtable([t[2] for t in v]) for c, v in sorted(ticks.items())},
        "per_slot": {}}
for name, s, e in SLOTS:
    dist["per_slot"][name] = qtable([t[2] for c in ticks for t in ticks[c] if s <= t[1] < e])

# ---------- 3. 过档 + 冷却去重虚拟信号 + 结算 ----------
snap_cache = {}
def day_bars(code, date):
    key = (code, date)
    if key not in snap_cache:
        fp = SNAP / date[:4] / date[5:7] / f"{code}_{date}.json"
        snap_cache[key] = json.load(open(fp, encoding="utf-8"))["bars"] if fp.exists() else []
    return snap_cache[key]

th_stats = {}
for th in THRESHOLDS:
    dens = defaultdict(lambda: defaultdict(int))
    settle_all = defaultdict(lambda: {"WIN": 0, "FAIL": 0, "VOID": 0})
    settle_dgok = defaultdict(lambda: {"WIN": 0, "FAIL": 0, "VOID": 0})
    gated_share = defaultdict(lambda: [0, 0])  # code -> [gated, total]
    for code, arr in sorted(ticks.items()):
        byday = defaultdict(list)
        for t in arr:
            byday[t[0]].append(t)
        for date, dt in byday.items():
            dt.sort(key=lambda x: x[1])
            bars = day_bars(code, date)
            t2i = {b["time"][11:16]: i for i, b in enumerate(bars)} if bars else {}
            last_i = -10**9
            for _, hm, b, s_, gated in dt:
                if b < th or b <= s_:      # 对齐仲裁: 买分须严格>卖分
                    continue
                i = t2i.get(hm)
                if i is None or i - last_i < COOLDOWN_MIN:
                    continue
                last_i = i
                dens[code][date] += 1
                gated_share[code][1] += 1
                fut = pd.DataFrame(bars[i + 1: i + 31])
                res, _ = settle_signal("BUY_LOW", bars[i]["close"], fut)
                settle_all[code][res] += 1
                if gated:
                    gated_share[code][0] += 1
                else:
                    settle_dgok[code][res] += 1
    def wr(d):
        return {c: (round(v["WIN"] / (v["WIN"] + v["FAIL"]), 4) if v["WIN"] + v["FAIL"] else None)
                for c, v in sorted(d.items())}
    th_stats[str(th)] = {
        "dedup_signals_per_day": {c: qtable(list(d.values())) for c, d in sorted(dens.items())},
        "dedup_total": {c: sum(d.values()) for c, d in sorted(dens.items())},
        "est_wr_all": wr(settle_all), "est_wr_daily_ok": wr(settle_dgok),
        "daily_gated_share": {c: f"{g}/{t}={g / t:.0%}" if t else "0" for c, (g, t) in sorted(gated_share.items())},
        "settle_all": {c: dict(v) for c, v in sorted(settle_all.items())}}

# ---------- 4. 因子分解 ----------
all_fn = sorted({fn for c in factors for fn in factors[c]})
factor_stats = {}
for code in sorted(ticks):
    factor_stats[code] = {
        "factors": {fn: {"mean": round(sum(factors[code][fn]) / len(factors[code][fn]), 2),
                         "p90": pct(sorted(factors[code][fn]), 90),
                         "max": round(max(factors[code][fn]), 2)}
                    for fn in all_fn if factors[code].get(fn)},
        "residual(买分-因子和)": (lambda r: {"mean": round(sum(r) / len(r), 2), "p50": pct(sorted(r), 50),
                                            "p90": pct(sorted(r), 90), "max": round(max(r), 2)})(residuals[code])}

result = {"meta": {"source": "v110_degraded/parts decision_trace (v1.1.0生产代码, 统一口径)",
                   "ticks_ok": n_ok, "nan_rate": {c: round(nan_ct[c] / tot_ct[c], 4) for c in sorted(tot_ct)},
                   "trace_buy_threshold_census": dict(th_census),
                   "decision_reason_top": reasons.most_common(10),
                   "candidate_rule": "buy_score>=th 且 buy_score>sell_score (can_buy仲裁)",
                   "cooldown_min": COOLDOWN_MIN,
                   "settle": "§1.1 BUY_LOW 30bars +0.5%/-0.4%, 观测性预估(无份数联动/组合门控)"},
          "distribution": dist, "threshold_stats": th_stats, "factor_decomposition": factor_stats}
with open(OUT / "buy_score_dist.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)
print("JSON written:", OUT / "buy_score_dist.json")

print("\n== 分位数(NaN已剔) ==")
print(" overall:", dist["overall"])
for c, q in dist["per_code"].items():
    print(f"  {c}: {q}")
print(" per_slot:", {k: (v["p50"], v["p90"], v["p99"], v["max"]) for k, v in dist["per_slot"].items()})
print("\n== 各档: 密度med/p90/max | 预估胜率(全量/daily_ok) | daily_gate拦截占比 ==")
for th in THRESHOLDS:
    d = th_stats[str(th)]
    for c in sorted(ticks):
        q = d["dedup_signals_per_day"].get(c, {})
        print(f"  th={th} {c}: dens=({q.get('p50')}/{q.get('p90')}/{q.get('max')}) "
              f"wr={d['est_wr_all'].get(c)}/{d['est_wr_daily_ok'].get(c)} gated={d['daily_gated_share'].get(c)} "
              f"tot={d['dedup_total'].get(c)}")
