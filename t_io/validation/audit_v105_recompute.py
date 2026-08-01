# -*- coding: utf-8 -*-
"""
audit_v105_recompute.py — v1.0.4/v1.0.5 修复证据独立复算（只读）
"""
import json, math
from pathlib import Path
from collections import defaultdict, Counter, deque
from datetime import datetime, timedelta

BASE = Path(r"E:\06_T")
V105 = BASE / "t_io/validation/v105"
V104 = BASE / "t_io/validation/p1_v104"
OLD = BASE / "t_io/validation/p2_baseline"
SNAP = BASE / "t_io/minute_snapshots"
OUT = BASE / "t_io/validation/audit_v105_report.txt"

lines = []
def out(s=""):
    lines.append(str(s)); print(s)

def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

sigs = load_jsonl(V105 / "signals_v102.jsonl")
summ = json.load(open(V105 / "summary_v102.json", encoding="utf-8"))

# ============ 1d. R1 事件化效果复算 ============
out("=" * 70)
out("1d. R1 信号事件化效果复算 (v105 signals)")
out("=" * 70)
w = sum(1 for s in sigs if s["settle_result"] == "WIN")
f_ = sum(1 for s in sigs if s["settle_result"] == "FAIL")
v = sum(1 for s in sigs if s["settle_result"] == "VOID")
un = sum(1 for s in sigs if s["settle_result"] is None)
wr = w / (w + f_)
out(f"复算: total={len(sigs)} W={w} F={f_} V={v} unsettled={un} win_rate={wr:.4f}")
out(f"声称: total={summ['total']} win_rate={summ['win_rate']} (commit: 98信号 49.0%)")
per_day = defaultdict(list)
for s in sigs:
    d_, t_ = s["ts"].split(" ")
    per_day[(s["code"], d_)].append(t_)
counts = sorted(len(x) for x in per_day.values())
n = len(counts)
out(f"股×日有信号组合={n} (全覆盖=120) 信号数/股/日: min={counts[0]} 中位={counts[n//2]} max={counts[-1]} 均值={sum(counts)/n:.2f}")
out(f"按120股日摊薄: {len(sigs)}/120 = {len(sigs)/120:.2f} 个/股/日 (方案健康区间3-8)")
gaps = Counter()
for k, ts_list in per_day.items():
    ts_list.sort()
    for a, b in zip(ts_list, ts_list[1:]):
        ta = datetime.strptime(a, "%H:%M:%S"); tb = datetime.strptime(b, "%H:%M:%S")
        gaps[int((tb - ta).total_seconds() // 60)] += 1
tot_g = sum(gaps.values())
out(f"相邻间隔=1分钟占比: {gaps.get(1,0)}/{tot_g} = {gaps.get(1,0)/max(tot_g,1):.1%} (上轮 92.7%)")
per_stock = Counter(s["code"] for s in sigs)
out(f"分股信号数: {dict(per_stock)}")
for c in ["000988", "588170", "600176", "600481", "603667"]:
    cw = sum(1 for s in sigs if s["code"] == c and s["settle_result"] == "WIN")
    cf = sum(1 for s in sigs if s["code"] == c and s["settle_result"] == "FAIL")
    out(f"  {c}: 信号{per_stock.get(c,0)} W={cw} F={cf} 胜率={cw/(cw+cf):.3f}" if (cw+cf) else f"  {c}: 信号{per_stock.get(c,0)}")
# 阈值分布检查
th = Counter((s["action"], s["threshold"]) for s in sigs)
out(f"实际应用的阈值分布: {dict(th)}")
# 是否有qty字段
out(f"信号含qty字段: {'qty' in sigs[0]}  价格字段示例: {[s['price'] for s in sigs[:3]]}")

# ============ 2c. NEUTRAL 真实时长口径 ============
out()
out("=" * 70)
out("2c. NEUTRAL 口径复核: summary口径 vs 真实时长加权")
out("=" * 70)
tls = {}
for r in load_jsonl(V105 / "trend_timeline_v102.jsonl"):
    tls[r["key"]] = r["timeline"]
tn = tt = 0
for k, tl in tls.items():
    if not tl: continue
    for i, (t, s, _) in enumerate(tl):
        t0 = datetime.strptime(t, "%H:%M")
        t1 = datetime.strptime(tl[i+1][0], "%H:%M") if i+1 < len(tl) else datetime.strptime("15:00", "%H:%M")
        dur = max(0, (t1 - t0).total_seconds() / 60)
        tt += dur
        if s == "NEUTRAL": tn += dur
out(f"summary neutral_ratio=0.405 (=段数×5/总段数×5, 数学上仍是段数占比)")
out(f"真实时长加权(状态持续到下次切换/15:00): {tn:.0f}/{tt:.0f}分钟 = {tn/tt:.3f}")
out(f"注: 时间段首记录时间为状态被确认的tick(防抖2根5分K后), 真实起点更早, 两口径都有估计误差")

# ============ 3c. T闭环独立复算 ============
out()
out("=" * 70)
out("3c. T闭环复算: harness口径(跨日FIFO+qty=100) vs 方案口径(当日FIFO)")
out("=" * 70)
COMM = 0.00015; STAMP = 0.0005
def closed_loop(signals, same_day_only, stamp_fix=False):
    per = defaultdict(lambda: {"long": deque(), "short": deque(), "pairs": [], "pnl": 0.0})
    for s in sorted(signals, key=lambda x: x["ts"]):
        c = s["code"]; ps = per[c]
        qty = s.get("qty", 100); price = s["price"]
        day = s["ts"][:10]
        if s["action"] in ("BUY_LOW", "ADD_POS"):
            sp = ps["short"]
            while sp and (same_day_only and sp[0]["day"] != day):
                sp.popleft()  # 跨日短仓作废(开放仓)
            if sp:
                e = sp.popleft()
                pnl = (e["price"] - price) * qty
                if stamp_fix:  # 印花税应在卖出腿
                    fee = e["price"]*qty*(COMM+STAMP) + price*qty*COMM
                else:          # harness实现: 印花在买回腿
                    fee = price*qty*(COMM+STAMP) + e["price"]*qty*COMM
                ps["pnl"] += pnl - fee
                ps["pairs"].append(("short_close", e["ts"], s["ts"], e["price"], price, qty, round(pnl-fee,2)))
            else:
                ps["long"].append({"ts": s["ts"], "price": price, "qty": qty, "day": day})
        elif s["action"] == "SELL_HIGH":
            lp = ps["long"]
            while lp and (same_day_only and lp[0]["day"] != day):
                lp.popleft()
            if lp:
                e = lp.popleft()
                pnl = (price - e["price"]) * qty
                fee = e["price"]*qty*COMM + price*qty*(COMM+STAMP)
                ps["pnl"] += pnl - fee
                ps["pairs"].append(("long_close", e["ts"], s["ts"], e["price"], price, qty, round(pnl-fee,2)))
            else:
                ps["short"].append({"ts": s["ts"], "price": price, "qty": qty, "day": day})
    return per

for label, sd, sf in [("harness口径(跨日+印花在买腿)", False, False),
                      ("方案口径(当日FIFO+印花在卖腿)", True, True)]:
    per = closed_loop(sigs, sd, sf)
    tp = sum(len(p["pairs"]) for p in per.values())
    tpnl = sum(p["pnl"] for p in per.values())
    out(f"[{label}] 配对={tp} 净收益={tpnl:.2f}")
    for c in sorted(per):
        p = per[c]
        if p["pairs"] or True:
            out(f"    {c}: pairs={len(p['pairs'])} pnl={p['pnl']:.2f} 未平多={len(p['long'])} 未平空={len(p['short'])}")
out(f"声称: 21对 / +15584.51 / 600176 +14586 / 603667 +953 / 588170 +43")

# 3d. 600176 构成
out()
out("=" * 70)
out("3d. 600176 净收益构成 (harness口径配对明细)")
out("=" * 70)
per = closed_loop(sigs, False, False)
p176 = per["600176"]
out(f"600176: {len(p176['pairs'])} 对, 净 {p176['pnl']:.2f}")
for pair in p176["pairs"]:
    out(f"    {pair}")
p603 = per["603667"]
out(f"603667: {len(p603['pairs'])} 对, 净 {p603['pnl']:.2f}")
for pair in p603["pairs"][:6]:
    out(f"    {pair}")
# 600176价格水平
s176 = [s for s in sigs if s["code"] == "600176"]
if s176:
    out(f"600176 信号价范围: {min(s['price'] for s in s176)} ~ {max(s['price'] for s in s176)}")

# 抽查2对闭环(时间序/价格/费用)
out()
out("抽查: 前2对闭环的费用手工核对")
all_pairs = []
for c, p in per.items():
    for pair in p["pairs"]:
        all_pairs.append((c,) + pair)
all_pairs.sort(key=lambda x: x[2])
for ap in all_pairs[:2]:
    c, typ, t1, t2, p1, p2, qty, pnl = ap
    if typ == "long_close":
        fee = p1*qty*COMM + p2*qty*(COMM+STAMP)
        raw = (p2-p1)*qty
    else:
        fee = p2*qty*(COMM+STAMP) + p1*qty*COMM
        raw = (p1-p2)*qty
    out(f"  {c} {typ}: {t1}@{p1} -> {t2}@{p2} qty={qty} 价差收益={raw:.2f} 费用={fee:.2f} 净={raw-fee:.2f} (记录{pnl})")

# ============ 4. P2 现状 ============
out()
out("=" * 70)
out("4. P2 闸门现状检查")
out("=" * 70)
import os
base_dirs = [d for d in os.listdir(BASE / "t_io/validation") if "baseline" in d.lower()]
out(f"baseline 证据目录: {base_dirs} (mtime: {datetime.fromtimestamp(os.path.getmtime(BASE/'t_io/validation/p2_baseline/summary_baseline.json'))})")
out("=> v1.0.4/v1.0.5 均未重跑 baseline；旧 baseline(每tick口径,6308信号) 与新 v102(事件化,98信号) 不可比")
# 逆势拦截率: v105 signals 是否含被门控拦截信息
tr_states = Counter(s["trend_state"] for s in sigs)
out(f"v105信号 trend_state 分布: {dict(tr_states)}")
blocked = [s for s in sigs if (s["trend_state"]=="BEAR" and s["action"] in ("BUY_LOW","ADD_POS")) or (s["trend_state"]=="BULL" and s["action"]=="SELL_HIGH")]
out(f"逆势信号(被门控降分但仍发出): {len(blocked)} 条 — 拦截率所需'被拦截未发出'信号在证据中无记录")
dates = sorted({s["ts"][:10] for s in sigs})
out(f"样本交易日: {len(dates)} 天 ({dates[0]}~{dates[-1]}) (方案最低30)")
# 切换滞后
out("切换滞后: summary/decision证据中无测量字段 — 仍未做")

with open(OUT, "w", encoding="utf-8") as fo:
    fo.write("\n".join(lines))
print(f"\n报告已写入: {OUT}")
