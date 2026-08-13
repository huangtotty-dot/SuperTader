# -*- coding: utf-8 -*-
"""
audit_v106_recompute.py — v1.0.6 (W1-W3) 修复证据独立复算（只读）
"""
import json, math, sys
from pathlib import Path
from collections import defaultdict, Counter, deque
from datetime import datetime, timedelta

BASE = Path(r"E:\06_T")
V106 = BASE / "t_io/validation/v106"
BV = BASE / "t_io/validation/p2_baseline_v105"
V105 = BASE / "t_io/validation/v105"
SNAP = BASE / "t_io/minute_snapshots"
OUT = BASE / "t_io/validation/audit_v106_report.txt"

lines = []
def out(s=""):
    lines.append(str(s)); print(s)

def load_jsonl(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

b_sigs = load_jsonl(BV / "signals_baseline.jsonl")
v_sigs = load_jsonl(V106 / "signals_v102.jsonl")
b_sum = json.load(open(BV / "summary_baseline.json", encoding="utf-8"))
v_sum = json.load(open(V106 / "summary_v102.json", encoding="utf-8"))

# ============ 1a. 两组信号数/胜率复算 ============
out("=" * 70)
out("1a. 事件化 A/B 两组复算")
out("=" * 70)
def wf(sigs):
    w = sum(1 for s in sigs if s["settle_result"] == "WIN")
    f_ = sum(1 for s in sigs if s["settle_result"] == "FAIL")
    v = sum(1 for s in sigs if s["settle_result"] == "VOID")
    u = sum(1 for s in sigs if s["settle_result"] is None)
    return w, f_, v, u
for tag, sigs, summ, claim in [("baseline", b_sigs, b_sum, "47/52.2%"), ("v102", v_sigs, v_sum, "98/49.0%")]:
    w, f_, v, u = wf(sigs)
    wr = w / (w + f_) if (w + f_) else 0
    out(f"[{tag}] 复算: total={len(sigs)} W={w} F={f_} V={v} unsettled={u} wr={wr:.4f} | summary: total={summ['total']} wr={summ['win_rate']} | 声称 {claim}")

# v106 vs v105 信号一致性（应仅多 qty 字段）
old = load_jsonl(V105 / "signals_v102.jsonl")
old_keys = {(s["ts"], s["code"], s["action"], s["price"]) for s in old}
new_keys = {(s["ts"], s["code"], s["action"], s["price"]) for s in v_sigs}
out(f"v106 vs v105 信号集合: 相同={len(old_keys & new_keys)} 仅v105={len(old_keys - new_keys)} 仅v106={len(new_keys - old_keys)}")
qvals = [s.get("qty") for s in v_sigs]
out(f"v106信号qty字段: 全部存在={all(q is not None for q in qvals)} qty=0条数={sum(1 for q in qvals if q==0)} qty分布={dict(Counter(qvals))}")

# ============ 1c. 分股构成 + 增量信号胜率 ============
out()
out("=" * 70)
out("1c. 分股 A/B 构成 + 趋势层增量信号胜率")
out("=" * 70)
out(f"{'code':8} {'A信号 W/F 胜率':>20} {'B信号 W/F 胜率':>20}")
for c in ["000988", "588170", "600176", "600481", "603667"]:
    row = f"{c:8}"
    for sigs in [b_sigs, v_sigs]:
        n = sum(1 for s in sigs if s["code"] == c)
        w = sum(1 for s in sigs if s["code"] == c and s["settle_result"] == "WIN")
        f_ = sum(1 for s in sigs if s["code"] == c and s["settle_result"] == "FAIL")
        row += f" {n:>3} {w}/{f_} {w/(w+f_):.3f}" if (w + f_) else f" {n:>3}  -/-  n/a  "
        row += "   "
    out(row)
b_keys = {(s["ts"], s["code"], s["action"]) for s in b_sigs}
v_keys = {(s["ts"], s["code"], s["action"]) for s in v_sigs}
only_v = [s for s in v_sigs if (s["ts"], s["code"], s["action"]) not in b_keys]
only_b = [s for s in b_sigs if (s["ts"], s["code"], s["action"]) not in v_keys]
common = [s for s in v_sigs if (s["ts"], s["code"], s["action"]) in b_keys]
def wr_of(lst):
    w = sum(1 for s in lst if s["settle_result"] == "WIN")
    f_ = sum(1 for s in lst if s["settle_result"] == "FAIL")
    return f"{w}/{w+f_}={w/(w+f_):.3f}" if (w + f_) else f"{w}/0=n/a"
out(f"两組共有信号: {len(common)} 胜率(v102侧结算) {wr_of(common)}")
out(f"仅v102(趋势层放行新增): {len(only_v)} 胜率 {wr_of(only_v)}")
out(f"仅baseline(趋势层拦截/降分消失): {len(only_b)} 胜率 {wr_of(only_b)}")
by_action = Counter(s["action"] for s in only_v)
out(f"仅v102信号 action 分布: {dict(by_action)}  trend_state分布: {dict(Counter(s['trend_state'] for s in only_v))}")

# ============ 1d. Fisher 精确检验 ============
out()
out("=" * 70)
out("1d. 胜率差显著性 (Fisher 精确检验, 双侧)")
out("=" * 70)
w1, f1, _, _ = wf(b_sigs); w2, f2, _, _ = wf(v_sigs)
n1, n2 = w1 + f1, w2 + f2
def hypergeom_pmf(a, w, f, W):
    # P(X=a): 总 W胜 F负, 抽 n=w+f 个中 a 胜
    F = W + (w + f - a)  # not used directly; use standard 2x2
    pass
def fisher_two_sided(w1, f1, w2, f2):
    from math import comb
    W = w1 + w2; F = f1 + f2; n1 = w1 + f1; n2 = w2 + f2; N = n1 + n2
    def p(a):
        return comb(W, a) * comb(F, n1 - a) / comb(N, n1)
    lo = max(0, n1 - F); hi = min(W, n1)
    p_obs = p(w1)
    return sum(p(a) for a in range(lo, hi + 1) if p(a) <= p_obs * (1 + 1e-9))
pv = fisher_two_sided(w1, f1, w2, f2)
out(f"baseline {w1}/{n1}={w1/n1:.3f} vs v102 {w2}/{n2}={w2/n2:.3f}  diff={(w2/n2-w1/n1)*100:+.2f}pp")
out(f"Fisher 双侧 p={pv:.4f} → {'显著' if pv<0.05 else '不显著(α=0.05)'}")
out(f"功效提示: 两组N合计仅{n1+n2}，要区分53% vs 49%需每组~1500+独立信号；当前差异既不能说有害也不能说无害")

# ============ 2. W2 闭环复算（独立实现 + 调 harness 实际函数交叉验证）============
out()
out("=" * 70)
out("2b. T闭环复算（两组）")
out("=" * 70)
COMM = 0.00015; STAMP = 0.0005
def closed_loop_indep(signals):
    per = defaultdict(lambda: {"pairs": [], "pnl": 0.0, "open_l": 0, "open_s": 0})
    by_date = defaultdict(list)
    for s in signals:
        by_date[s["ts"][:10]].append(s)
    for d, day in sorted(by_date.items()):
        pos = defaultdict(lambda: {"long": deque(), "short": deque()})
        for s in sorted(day, key=lambda x: x["ts"]):
            c = s["code"]; dp = pos[c]; ps = per[c]
            qty = s.get("qty", 100); price = s["price"]
            if s["action"] in ("BUY_LOW", "ADD_POS"):
                if dp["short"]:
                    e = dp["short"].popleft()
                    pnl = (e["price"] - price) * qty
                    fee = e["price"]*qty*(COMM+STAMP) + price*qty*COMM
                    ps["pnl"] += pnl - fee
                    ps["pairs"].append(("short_close", d, e["ts"], s["ts"], e["price"], price, qty, round(pnl-fee, 2)))
                else:
                    dp["long"].append({"ts": s["ts"], "price": price, "qty": qty})
            elif s["action"] == "SELL_HIGH":
                if dp["long"]:
                    e = dp["long"].popleft()
                    pnl = (price - e["price"]) * qty
                    fee = e["price"]*qty*COMM + price*qty*(COMM+STAMP)
                    ps["pnl"] += pnl - fee
                    ps["pairs"].append(("long_close", d, e["ts"], s["ts"], e["price"], price, qty, round(pnl-fee, 2)))
                else:
                    dp["short"].append({"ts": s["ts"], "price": price, "qty": qty})
        for c, dp in pos.items():
            per[c]["open_l"] += sum(e["qty"] for e in dp["long"])
            per[c]["open_s"] += sum(e["qty"] for e in dp["short"])
    return per

# 交叉验证：调用 harness 实际函数
sys.path.insert(0, str(BASE))
import importlib, harness_backtest
importlib.reload(harness_backtest)
holdings = json.load(open(BASE / "holdings.json", encoding="utf-8"))
hmap = {k.split("_")[0]: v for k, v in holdings.items()}

for tag, sigs in [("baseline", b_sigs), ("v102", v_sigs)]:
    per = closed_loop_indep(sigs)
    tp = sum(len(p["pairs"]) for p in per.values())
    tpnl = sum(p["pnl"] for p in per.values())
    out(f"[{tag}] 独立复算: 配对={tp} 净收益={tpnl:.2f}")
    for c in sorted(per):
        p = per[c]
        out(f"    {c}: pairs={len(p['pairs'])} pnl={p['pnl']:.2f} 开放多={p['open_l']}股 开放空={p['open_s']}股")
    # harness 实际函数
    try:
        hcl = harness_backtest.compute_closed_loop(sigs, hmap)
        out(f"    harness实际函数: pairs={hcl['total_closed_pairs']} pnl={hcl['total_net_pnl']}  {'✓一致' if abs(hcl['total_net_pnl']-tpnl)<0.05 else '✗不一致'}")
    except Exception as ex:
        out(f"    harness函数调用失败: {ex}")

# 2c 抽查2对
out()
out("2c. 抽查 2 对闭环逐分手核")
per_v = closed_loop_indep(v_sigs)
all_pairs = []
for c, p in per_v.items():
    for pair in p["pairs"]:
        all_pairs.append((c,) + pair)
all_pairs.sort(key=lambda x: (x[2], x[3]))
for ap in all_pairs[:2]:
    c, typ, d, t1, t2, p1, p2, qty, pnl = ap
    if typ == "long_close":
        fee = p1*qty*COMM + p2*qty*(COMM+STAMP); raw = (p2-p1)*qty
    else:
        fee = p1*qty*(COMM+STAMP) + p2*qty*COMM; raw = (p1-p2)*qty
    ok = abs(raw - fee - pnl) < 0.02
    out(f"  {c} {typ} {d}: {t1[11:]}@{p1}→{t2[11:]}@{p2} qty={qty} 价差={raw:.2f} 费={fee:.2f} 净={raw-fee:.2f} 记录={pnl} {'✓' if ok else '✗'}")

# 2d +382 构成
out()
out("2d. v102组闭环净收益构成")
for c in sorted(per_v):
    p = per_v[c]
    if not p["pairs"]: continue
    out(f"  {c} ({len(p['pairs'])}对, {p['pnl']:.2f}):")
    for pair in p["pairs"]:
        out(f"    {pair}")

# ============ 3. W3 P1 口径复算 ============
out()
out("=" * 70)
out("3a/3b. NEUTRAL真实时长 + 午盘前众数一致率")
out("=" * 70)
tls = {r["key"]: r["timeline"] for r in load_jsonl(V106 / "trend_timeline_v102.jsonl")}
# 3a NEUTRAL
tn = tt = 0
for k, tl in tls.items():
    for i, (t, s, _) in enumerate(tl):
        t0 = int(t[:2])*60 + int(t[3:5])
        t1 = (int(tl[i+1][0][:2])*60 + int(tl[i+1][0][3:5])) if i+1 < len(tl) else 900
        dur = max(t1 - t0, 1)
        tt += dur
        if s == "NEUTRAL": tn += dur
out(f"NEUTRAL真实时长: {tn}/{tt}分钟 = {tn/tt:.4f} (声称9.7%, summary={v_sum['p1_metrics'].get('neutral_ratio')})")

# 3b 午盘前众数一致率（复现 harness 逻辑：11:30截止, 无AM非NEUTRAL回退全天）
def classify(code, date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    p = SNAP / str(dt.year) / f"{dt.month:02d}" / f"{code}_{date_str}.json"
    d = json.load(open(p, encoding="utf-8"))
    bars = d["bars"]
    o = float(bars[0]["open"]); c = float(bars[-1]["close"])
    day_ret = (c - o) / o
    mids = [(float(b["high"]) + float(b["low"])) / 2 for b in bars]
    avg = sum(mids) / len(mids)
    above = sum(1 for m in mids if m > avg) / len(mids)
    t0 = datetime.strptime(bars[0]["time"], "%Y-%m-%d %H:%M:%S")
    fh = [b for b in bars if datetime.strptime(b["time"], "%Y-%m-%d %H:%M:%S") <= t0 + timedelta(hours=1)]
    fh_ret = (float(fh[-1]["close"]) - float(fh[0]["open"])) / float(fh[0]["open"])
    rev = (fh_ret > 0.003 and day_ret < -0.005) or (fh_ret < -0.003 and day_ret > 0.005)
    if rev and abs(day_ret) >= 0.008: return "reversal_day"
    if day_ret >= 0.01 and above >= 0.55: return "bull_day"
    if day_ret <= -0.01 and above <= 0.45: return "bear_day"
    return "chop_day"

bm = bt_ = bmm = btm = 0
ties = []
fallback_days = 0
for k, tl in tls.items():
    date_str, code = k.split(":")
    dtype = classify(code, date_str)
    am = [s for t, s, _ in tl if (int(t[:2])*60 + int(t[3:5])) <= 690 and s != "NEUTRAL"]
    if not am:
        fallback_days += 1
        am = [s for _, s, _ in tl if s != "NEUTRAL"]
    if not am: continue
    cnt = Counter(am)
    top = max(cnt.values())
    leaders = [s for s, n in cnt.items() if n == top]
    dom = max(set(am), key=am.count)
    if len(leaders) > 1: ties.append((k, dict(cnt), dom))
    if dtype == "bull_day":
        bt_ += 1; bm += dom in ("BULL", "STRONG_BULL")
    elif dtype == "bear_day":
        btm += 1; bmm += dom in ("BEAR", "STRONG_BEAR")
out(f"午盘前众数: bull={bm}/{bt_}={bm/bt_:.3f} bear={bmm}/{btm}={bmm/btm:.3f} overall={(bm+bmm)/(bt_+btm):.4f} (声称78.1%, summary={v_sum['p1_metrics'].get('overall_consistency')})")
out(f"AM无非NEUTRAL回退全天: {fallback_days}天 | 平局日: {len(ties)} {ties[:5]}")
m = v_sum["p1_metrics"]
out(f"summary其他: bias_ratio={m.get('bias_ratio')} bull_days={m.get('sample_days_bull')} bear_days={m.get('sample_days_bear')}")
out(f"per_stock键示例: {list(list(m['per_stock'].values())[0].keys()) if m.get('per_stock') else None}")
# 3c/3d/3e
src = open(BASE / "harness_backtest.py", encoding="utf-8").read()
out(f"3c 反转日评估: compute_p1_metrics中reversal相关行数={src.count('reversal')}, 是否仅计数={'reversal_detected' not in src or src.count('reversal_detected')>=0}")
out(f"   reversal_total出现={src.count('reversal_total')} reversal_correct出现={src.count('reversal_correct')} 切换滞后字段出现={src.count('lag')}")

with open(OUT, "w", encoding="utf-8") as fo:
    fo.write("\n".join(lines))
print(f"\n报告已写入: {OUT}")
