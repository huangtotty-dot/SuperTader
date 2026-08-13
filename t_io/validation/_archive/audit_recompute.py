# -*- coding: utf-8 -*-
"""
audit_recompute.py — P1/P2 验证证据独立复算（只读，不改任何项目文件）
审核员脚本：从 signals/trend_timeline/minute_snapshots 原始数据重算关键指标。
"""
import json, math
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, timedelta

BASE = Path(r"E:\06_T")
P1 = BASE / "t_io/validation/p1"
P2 = BASE / "t_io/validation/p2_baseline"
SNAP = BASE / "t_io/minute_snapshots"
OUT = BASE / "t_io/validation/audit_recompute_report.txt"

lines = []
def out(s=""):
    lines.append(str(s)); print(s)

def load_jsonl(p):
    recs = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs

def load_bars(code, date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    p = SNAP / str(dt.year) / f"{dt.month:02d}" / f"{code}_{date_str}.json"
    if not p.exists():
        p = SNAP / f"{code}_{date_str}.json"
        if not p.exists():
            return []
    d = json.load(open(p, encoding="utf-8"))
    snaps = d if isinstance(d, list) else d.get("bars") or d.get("snapshots") or []
    rows = []
    for s in snaps:
        t = s.get("time", "")
        if len(str(t)) <= 5:
            t = f"{date_str} {t}"
        rows.append({"time": pd_ts(t), "open": float(s.get("open") or 0),
                     "high": float(s.get("high") or 0), "low": float(s.get("low") or 0),
                     "close": float(s.get("close") or 0)})
    rows.sort(key=lambda r: r["time"])
    return rows

def pd_ts(s):
    return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")

# ============ 2a. 总胜率复算 ============
out("=" * 70)
out("2a. 总胜率/无效率复算 vs summary 报告值")
out("=" * 70)
summ = {}
for tag, d, name in [("v102", P1, "summary_v102.json"), ("baseline", P2, "summary_baseline.json")]:
    sigs = load_jsonl(d / f"signals_{tag}.jsonl")
    summ[tag] = sigs
    w = sum(1 for s in sigs if s["settle_result"] == "WIN")
    f_ = sum(1 for s in sigs if s["settle_result"] == "FAIL")
    v = sum(1 for s in sigs if s["settle_result"] == "VOID")
    none_ = sum(1 for s in sigs if s["settle_result"] is None)
    wr = w / (w + f_) if (w + f_) else 0
    rep = json.load(open(d / name, encoding="utf-8"))
    out(f"[{tag}] 复算: total={len(sigs)} WIN={w} FAIL={f_} VOID={v} settle=None:{none_}")
    out(f"       复算 win_rate={wr:.4f} void_rate={v/len(sigs):.4f}")
    out(f"       报告 total={rep['total']} WIN={rep['wins']} FAIL={rep['fails']} VOID={rep['voids']}"
        f" win_rate={rep['win_rate']} void_rate={rep['void_rate']}")
    match = (abs(wr - rep['win_rate']) < 5e-4 and w == rep['wins'] and f_ == rep['fails'])
    out(f"       核对: {'一致' if match else '不一致'} (注: total 含 settle=None 未结算信号)")

# ============ 2b. 信号密度 + 相邻间隔 ============
out()
out("=" * 70)
out("2b. 每股每日信号数分布 + 相邻信号时间间隔")
out("=" * 70)
for tag in ["v102", "baseline"]:
    sigs = summ[tag]
    per_day = defaultdict(list)
    for s in sigs:
        d_, t_ = s["ts"].split(" ")
        per_day[(s["code"], d_)].append(t_)
    counts = sorted(len(v) for v in per_day.values())
    n = len(counts)
    def q(p):
        return counts[min(n - 1, int(p * n))]
    out(f"[{tag}] 股×日 组合数={n} 信号数/股/日: min={counts[0]} p25={q(0.25)} "
        f"中位={q(0.5)} p75={q(0.75)} max={counts[-1]} 均值={sum(counts)/n:.1f}")
    # 相邻间隔
    gaps = Counter()
    for k, ts_list in per_day.items():
        ts_list.sort()
        for a, b in zip(ts_list, ts_list[1:]):
            ta = datetime.strptime(a, "%H:%M:%S"); tb = datetime.strptime(b, "%H:%M:%S")
            gaps[int((tb - ta).total_seconds() // 60)] += 1
    tot = sum(gaps.values())
    top = sorted(gaps.items())[:6]
    out(f"       相邻间隔(分钟)分布 top: {top}  间隔=1分钟占比: {gaps.get(1,0)/tot:.1%} (n={tot})")
    # 同股同向连续信号（每tick重复记录的直接证据）
    streak = 0; maxstreak = 0
    per_day_full = defaultdict(list)
    for s in summ[tag]:
        d_ = s["ts"].split(" ")[0]
        per_day_full[(s["code"], d_)].append((s["ts"], s["action"]))
    streaks = []
    for k, lst in per_day_full.items():
        lst.sort()
        cur = 1
        for a, b in zip(lst, lst[1:]):
            if a[1] == b[1]:
                cur += 1
            else:
                streaks.append(cur); cur = 1
        streaks.append(cur)
    streaks.sort()
    out(f"       同方向连续信号段: 中位长度={streaks[len(streaks)//2]} p90={streaks[int(0.9*len(streaks))]} max={streaks[-1]}")

# ============ 2c. 分股 A/B 胜率 ============
out()
out("=" * 70)
out("2c. 分股 A/B 胜率对比 (baseline vs v102)")
out("=" * 70)
out(f"{'code':8} {'A:W/F/胜率':>22} {'B:W/F/胜率':>22} {'差(pp)':>8} 判定")
per_stock = {}
for code in ["000988", "588170", "600176", "600481", "603667"]:
    res = {}
    for tag in ["baseline", "v102"]:
        w = sum(1 for s in summ[tag] if s["code"] == code and s["settle_result"] == "WIN")
        f_ = sum(1 for s in summ[tag] if s["code"] == code and s["settle_result"] == "FAIL")
        res[tag] = (w, f_, w / (w + f_) if (w + f_) else float("nan"))
    a, b = res["baseline"], res["v102"]
    diff = (b[2] - a[2]) * 100
    flag = "⚠️退化>10pp" if diff < -10 else ""
    per_stock[code] = (a, b, diff)
    out(f"{code:8} {f'{a[0]}/{a[1]}/{a[2]:.3f}':>22} {f'{b[0]}/{b[1]}/{b[2]:.3f}':>22} {diff:>+7.2f} {flag}")

# ============ 2d. 显著性 ============
out()
out("=" * 70)
out("2d. v102 vs baseline 胜率差异显著性 (两比例 z 检验)")
out("=" * 70)
def wf(tag):
    w = sum(1 for s in summ[tag] if s["settle_result"] == "WIN")
    f_ = sum(1 for s in summ[tag] if s["settle_result"] == "FAIL")
    return w, f_
w1, f1 = wf("v102"); w2, f2 = wf("baseline")
n1, n2 = w1 + f1, w2 + f2
p1_, p2_ = w1 / n1, w2 / n2
pp = (w1 + w2) / (n1 + n2)
se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2))
z = (p1_ - p2_) / se
out(f"v102: {w1}/{n1}={p1_:.4f}  baseline: {w2}/{n2}={p2_:.4f}  diff={(p1_-p2_)*100:+.2f}pp")
out(f"z={z:.3f} (|z|>1.96 才在 5% 水平显著) → {'显著' if abs(z)>1.96 else '不显著'}")
out("注: 相邻信号间隔大量=1分钟，30分钟结算窗高度重叠(同一价格路径被重复计数~30次)，")
out("    有效样本量远小于名义N≈5000/6000，上述 z 值还是最乐观(独立假设)下的结果。")

# 有效样本量粗估：同股同日同向信号段数
for tag in ["v102", "baseline"]:
    segs = 0
    per_day_full = defaultdict(list)
    for s in summ[tag]:
        per_day_full[(s["code"], s["ts"].split(" ")[0])].append((s["ts"], s["action"], s["settle_result"]))
    seg_results = Counter()
    for k, lst in per_day_full.items():
        lst.sort()
        prev = None
        for ts_, act, res in lst:
            cur = (act,)
            if cur != prev:
                segs += 1
                seg_results[res] += 1
                prev = cur
    w = seg_results["WIN"]; f_ = seg_results["FAIL"]
    out(f"[{tag}] 去重后独立信号段数={segs} (WIN段={w} FAIL段={f_} VOID段={seg_results['VOID']} None={seg_results[None]})"
        f" 段级胜率={w/(w+f_):.4f}" if (w+f_) else "")

# ============ 2e. 抽查3个信号的settle ============
out()
out("=" * 70)
out("2e. 抽查 3 个信号的 settle 正确性（独立重算）")
out("=" * 70)
def settle_indep(action, price, bars_after, win_pct):
    for b in bars_after[:30]:
        if action in ("BUY_LOW", "BUY", "ADD_POS"):
            if b["low"] <= price * 0.996: return ("FAIL", b["time"])
            if b["high"] >= price * (1 + win_pct): return ("WIN", b["time"])
        else:
            if b["high"] >= price * 1.004: return ("FAIL", b["time"])
            if b["low"] <= price * (1 - win_pct): return ("WIN", b["time"])
    return ("VOID", None)

ETF = {"588170"}
picks = {}
for want in ["WIN", "FAIL", "VOID"]:
    for s in summ["v102"]:
        if s["settle_result"] == want and s["settle_time"] is not None or (want=="VOID" and s["settle_result"]=="VOID"):
            picks[want] = s
            break
for want, s in picks.items():
    d_, t_ = s["ts"].split(" ")
    bars = load_bars(s["code"], d_)
    sig_ts = pd_ts(s["ts"])
    after = [b for b in bars if b["time"] > sig_ts]
    wp = 0.003 if s["code"] in ETF else 0.005
    res, st = settle_indep(s["action"], s["price"], after, wp)
    # 手工描述
    out(f"[{want}] {s['ts']} {s['code']} {s['action']} @ {s['price']} "
        f"阈值: {'+'+format(wp*100,'.1f')+'%='+format(s['price']*(1+wp),'.3f') if s['action'] in ('BUY_LOW','ADD_POS') else '-'+format(wp*100,'.1f')+'%='+format(s['price']*(1-wp),'.3f')}"
        f" / 止损线 {'-0.4%='+format(s['price']*0.996,'.3f') if s['action'] in ('BUY_LOW','ADD_POS') else '+0.4%='+format(s['price']*1.004,'.3f')}")
    out(f"      记录: settle={s['settle_result']} @ {s['settle_time']}  复算: settle={res} @ {st}  "
        f"{'✓一致' if res == s['settle_result'] else '✗不一致'}")
    trace = []
    for b in after[:8]:
        trace.append(f"{b['time'].strftime('%H:%M')}={b['close']}")
    out(f"      后续价格路径(前8分钟): {' '.join(trace)}")

# ============ 2f. 字段schema核对 ============
out()
out("=" * 70)
out("2f. signals jsonl 字段 vs 方案§1.4 schema")
out("=" * 70)
need = ["ts","code","name","action","price","buy_score","sell_score","threshold",
        "trend_state","trend_confidence","rsi_5m","dif_5m","dea_5m",
        "rsi5_buy_trigger","rsi5_sell_trigger","t_mode","priority_path","vwap_dev",
        "today_ret","settle_result","settle_time"]
have = set(summ["v102"][0].keys())
missing = [k for k in need if k not in have]
out(f"实际字段: {sorted(have)}")
out(f"§1.4要求但缺失: {missing}")

# ============ 附加1: 日型分类复核（含07-08）+ reversal 判定 ============
out()
out("=" * 70)
out("附1. 日型分类复核（按 harness classify_day_type 逻辑重算）")
out("=" * 70)
def classify(bars):
    if len(bars) < 30: return "unknown"
    o = bars[0]["open"]; c = bars[-1]["close"]
    day_ret = (c - o) / o
    hi = max(b["high"] for b in bars); lo = min(b["low"] for b in bars)
    mids = [(b["high"] + b["low"]) / 2 for b in bars]
    avg = sum(mids) / len(mids)
    above = sum(1 for m in mids if m > avg) / len(mids)
    t0 = bars[0]["time"]
    fh = [b for b in bars if b["time"] <= t0 + timedelta(hours=1)]
    fh_ret = (fh[-1]["close"] - fh[0]["open"]) / fh[0]["open"]
    rev = (fh_ret > 0.003 and day_ret < -0.005) or (fh_ret < -0.003 and day_ret > 0.005)
    if day_ret >= 0.01 and above >= 0.55: return "bull_day", day_ret, above, fh_ret
    if day_ret <= -0.01 and above <= 0.45: return "bear_day", day_ret, above, fh_ret
    if rev and abs(day_ret) >= 0.008: return "reversal_day", day_ret, above, fh_ret
    return "chop_day", day_ret, above, fh_ret

codes = ["000988", "588170", "600176", "600481", "603667"]
dates = sorted({s["ts"].split(" ")[0] for s in summ["v102"]})
dtype_cnt = Counter()
rev_missed = []
day_type_map = {}
for d_ in dates:
    for c in codes:
        bars = load_bars(c, d_)
        if not bars: continue
        r = classify(bars)
        dtype_cnt[r[0]] += 1
        day_type_map[(d_, c)] = r
        # 满足 reversal 条件但被 bull/bear 抢先归类的
        day_ret, above, fh_ret = r[1], r[2], r[3]
        rev = (fh_ret > 0.003 and day_ret < -0.005) or (fh_ret < -0.003 and day_ret > 0.005)
        if rev and abs(day_ret) >= 0.008 and r[0] != "reversal_day":
            rev_missed.append((d_, c, r[0], round(day_ret,4), round(fh_ret,4)))
out(f"日型分布(复算): {dict(dtype_cnt)}  (summary称 19 bull / 23 bear / 0 reversal)")
out(f"满足反转条件但被 bull/bear 规则抢先吞掉的日: {rev_missed if rev_missed else '无'}")
r8 = day_type_map.get(("2026-07-08", "000988"))
out(f"000988@07-08: 日型={r8[0]} 日涨幅(vs开盘)={r8[1]:.2%} 均价上方占比={r8[2]:.2f} 首小时涨幅={r8[3]:.2%}")

# ============ 附加2: P1指标按方案口径重算 ============
out()
out("=" * 70)
out("附2. P1 指标按方案口径重算 vs summary 口径")
out("=" * 70)
tls = {}
for rec in load_jsonl(P1 / "trend_timeline_v102.jsonl"):
    d_, c = rec["key"].split(":")
    tls[(d_, c)] = rec["timeline"]

# summary口径: dominant=众数(除NEUTRAL), 分母=全部bull/bear日
# 方案口径: 分母=系统非NEUTRAL且客观单边日; NEUTRAL占比=时长加权; 偏差=系统判定BULL日数/BEAR日数
res_sum = Counter(); res_spec = Counter(); spec_denom = 0
time_neutral = 0; time_total = 0
sys_bull_days = 0; sys_bear_days = 0
TRADE_END = "15:00"
for (d_, c), tl in tls.items():
    if not tl: continue
    states = [s for _, s, _ in tl if s != "NEUTRAL"]
    dominant = max(set(states), key=states.count) if states else "NEUTRAL"
    r = day_type_map.get((d_, c))
    dtype = r[0] if r else "unknown"
    # 时长加权 NEUTRAL 占比
    for i, (t, s, _) in enumerate(tl):
        t_start = datetime.strptime(t, "%H:%M")
        t_end = datetime.strptime(tl[i+1][0], "%H:%M") if i+1 < len(tl) else datetime.strptime(TRADE_END, "%H:%M")
        dur = max(0, (t_end - t_start).total_seconds() / 60)
        time_total += dur
        if s == "NEUTRAL":
            time_neutral += dur
    # 系统判定日方向
    if dominant in ("BULL", "STRONG_BULL"): sys_bull_days += 1
    elif dominant in ("BEAR", "STRONG_BEAR"): sys_bear_days += 1
    # summary口径
    if dtype == "bull_day":
        res_sum["bull_total"] += 1
        if dominant in ("BULL", "STRONG_BULL"): res_sum["bull_match"] += 1
    if dtype == "bear_day":
        res_sum["bear_total"] += 1
        if dominant in ("BEAR", "STRONG_BEAR"): res_sum["bear_match"] += 1
    # 方案口径: 分母=系统非NEUTRAL 且 客观单边
    if dtype in ("bull_day", "bear_day") and dominant != "NEUTRAL":
        spec_denom += 1
        want = ("BULL", "STRONG_BULL") if dtype == "bull_day" else ("BEAR", "STRONG_BEAR")
        if dominant in want:
            res_spec["match"] += 1
        else:
            res_spec["mismatch_wrong_dir"] += 1  # 判反方向
    elif dtype in ("bull_day", "bear_day") and dominant == "NEUTRAL":
        res_spec["excluded_neutral"] += 1

sm = res_sum["bull_match"] + res_sum["bear_match"]; st = res_sum["bull_total"] + res_sum["bear_total"]
out(f"[summary口径] 一致率={sm}/{st}={sm/st:.3f}  (报告值 0.881)")
out(f"[方案口径] 分母排除系统NEUTRAL日({res_spec['excluded_neutral']}天): "
    f"一致率={res_spec['match']}/{spec_denom}={res_spec['match']/spec_denom:.3f}"
    f" 其中判反方向={res_spec['mismatch_wrong_dir']}天")
out(f"[NEUTRAL占比] 报告值 0.233 = 状态段数占比(字段名叫neutral_minutes实为段数)")
out(f"            时长加权重算 = {time_neutral:.0f}/{time_total:.0f} 分钟 = {time_neutral/time_total:.3f}")
out(f"[系统性偏差] 报告值 0.826 = 客观bull日/客观bear日 = 19/23 (样本构成，非系统判定偏差)")
out(f"            方案口径(系统判定BULL日/BEAR日) = {sys_bull_days}/{sys_bear_days} = {sys_bull_days/max(sys_bear_days,1):.3f}")

# ============ 附加3: STRONG档30分钟准确率独立补算 ============
out()
out("=" * 70)
out("附3. STRONG 档 30 分钟准确率独立补算（summary 中全为0的指标）")
out("=" * 70)
sb_tot = sb_ok = 0; ss_tot = ss_ok = 0
for (d_, c), tl in tls.items():
    bars = load_bars(c, d_)
    if not bars: continue
    for t, s, _ in tl:
        if s not in ("STRONG_BULL", "STRONG_BEAR"): continue
        t0 = datetime.strptime(f"{d_} {t}", "%Y-%m-%d %H:%M")
        before = [b for b in bars if b["time"] <= t0]
        after = [b for b in bars if t0 < b["time"] <= t0 + timedelta(minutes=30)]
        if not before or len(after) < 5: continue
        p0 = before[-1]["close"]
        p_end = after[-1]["close"]
        if s == "STRONG_BULL":
            sb_tot += 1
            if p_end > p0: sb_ok += 1
        else:
            ss_tot += 1
            if p_end < p0: ss_ok += 1
out(f"STRONG_BULL 后30分钟上涨: {sb_ok}/{sb_tot} = {sb_ok/max(sb_tot,1):.1%} (报告值 0/67)")
out(f"STRONG_BEAR 后30分钟下跌: {ss_ok}/{ss_tot} = {ss_ok/max(ss_tot,1):.1%} (报告值 0/107)")
out("结论: summary 全0系 compute_p1_metrics 只累计 total 从未给 correct 赋值(功能未实现)，非真实0%")

# ============ 附加4: 信号结算窗口重叠度 ============
out()
out("=" * 70)
out("附4. 结算窗口重叠度（N虚增量化）")
out("=" * 70)
for tag in ["v102", "baseline"]:
    per_cd = defaultdict(list)
    for s in summ[tag]:
        per_cd[(s["code"], s["ts"].split(" ")[0])].append(s["ts"])
    overlap = 0; tot = 0
    for k, lst in per_cd.items():
        lst.sort()
        prev_settle_end = None
        for ts_ in lst:
            t0 = datetime.strptime(ts_, "%Y-%m-%d %H:%M:%S")
            tot += 1
            if prev_settle_end and t0 < prev_settle_end:
                overlap += 1
            prev_settle_end = max(prev_settle_end or t0, t0 + timedelta(minutes=30))
    out(f"[{tag}] 结算窗与前一信号重叠的信号占比: {overlap}/{tot} = {overlap/tot:.1%}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\n报告已写入: {OUT}")
