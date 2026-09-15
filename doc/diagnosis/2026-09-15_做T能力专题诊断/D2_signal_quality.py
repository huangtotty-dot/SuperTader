# -*- coding: utf-8 -*-
"""D2 信号质量诊断（信号端弱在哪）— 2026-09-15
只读 t_io/ 数据；统计窗口 2026-08-27 ~ 2026-09-14（renko_t 13 个完整交易日）。
前向收益数据源：t_io/minute_snapshots 1min 快照（起点标签口径），CSV 缓存仅到 08-26 故不用。
"""
import sys, os, json, glob, re, collections
sys.stdout.reconfigure(encoding='utf-8')

ROOT = r'E:/superTrader'
TRACES = os.path.join(ROOT, 't_io', 'traces')
LOGS = os.path.join(ROOT, 't_io', 'logs')
SNAP = os.path.join(ROOT, 't_io', 'minute_snapshots')
EXCLUDE_DAY = '2026-09-15'

RENKO_DAYS = ['2026-08-27','2026-08-28','2026-08-31','2026-09-01','2026-09-02',
              '2026-09-03','2026-09-04','2026-09-07','2026-09-08','2026-09-09',
              '2026-09-10','2026-09-11','2026-09-14']  # 13 个完整交易日

def iter_jsonl(path):
    n_bad = 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                n_bad += 1
    if n_bad:
        print(f'  [脏行] {os.path.basename(path)}: {n_bad}')

def dayof(path, prefix):
    return os.path.basename(path)[len(prefix):len(prefix)+10]

# ---------- 1. 加载 renko_t ----------
entries, exits = [], []
for f in sorted(glob.glob(os.path.join(TRACES, 'renko_t_2026-*.jsonl'))):
    day = dayof(f, 'renko_t_')
    if day == EXCLUDE_DAY:
        continue
    for d in iter_jsonl(f):
        d['_day'] = day
        if d.get('action') == 'BUY_LOW':
            entries.append(d)
        elif d.get('action') == 'SELL_HIGH':
            exits.append(d)
print(f'[renko_t] 完整日 {len(set(d["_day"] for d in entries+exits))} 天, '
      f'BUY_LOW {len(entries)} 条, SELL_HIGH {len(exits)} 条')

# ---------- 2. 加载快照 1min ----------
SNAP_RE = re.compile(r'^(\d{6})_(\d{4}-\d{2}-\d{2})\.json$')
snap = collections.defaultdict(dict)  # code -> day -> bars
for f in glob.glob(os.path.join(SNAP, '20*', '*', '*.json')):
    m = SNAP_RE.match(os.path.basename(f))
    if not m:
        continue
    code, dt = m.group(1), m.group(2)
    if dt not in RENKO_DAYS:
        continue
    try:
        d = json.load(open(f, encoding='utf-8'))
    except Exception:
        continue
    bars = [{'t': b['time'][11:16], 'o': float(b['open']), 'h': float(b['high']),
             'l': float(b['low']), 'c': float(b['close'])} for b in d['bars']]
    bars.sort(key=lambda x: x['t'])
    snap[code][dt] = bars

def fwd_metrics(code, day, ts, price):
    """前向指标: ret15/ret30/ret_close(%), MFE30/60(30/60分钟内最大涨幅%, 相对信号价)"""
    bars = snap.get(code, {}).get(day)
    if not bars or len(bars) < 60:
        return None
    hhmm = ts[11:16]
    hi = int(ts[11:13]); mi = int(ts[14:16])
    def add_min(mins):
        t = hi*60 + mi + mins
        return '%02d:%02d' % (t//60, t%60)
    # 信号之后的 bar（起点标签 > 信号分钟）
    after = [b for b in bars if b['t'] > hhmm]
    if not after:
        return None
    def price_at(mins):
        tgt = add_min(mins)
        cand = [b for b in bars if b['t'] >= tgt]
        return cand[0]['c'] if cand else None
    p15, p30 = price_at(15), price_at(30)
    pclose = bars[-1]['c']
    w30 = [b for b in after if b['t'] <= add_min(30)]
    w60 = [b for b in after if b['t'] <= add_min(60)]
    mfe30 = max((b['h'] for b in w30), default=None)
    mfe60 = max((b['h'] for b in w60), default=None)
    return {
        'ret15': (p15/price-1)*100 if p15 else None,
        'ret30': (p30/price-1)*100 if p30 else None,
        'ret_close': (pclose/price-1)*100 if pclose else None,
        'mfe30': (mfe30/price-1)*100 if mfe30 else None,
        'mfe60': (mfe60/price-1)*100 if mfe60 else None,
    }

def bucket(ts):
    m = int(ts[11:13])*60 + int(ts[14:16])
    if m < 600: return '早盘09:30-10:00'
    if m < 690: return '午前10:00-11:30'
    if m < 840: return '午后13:00-14:00'
    return '尾盘14:00-15:00'

# ---------- 3. 为 BUY_LOW 计算前向 ----------
n_cover = 0
for e in entries:
    fm = fwd_metrics(e['code'], e['_day'], e['ts'], e['price'])
    e['_fm'] = fm
    if fm and fm['ret30'] is not None:
        n_cover += 1
print(f'[前向覆盖] {n_cover}/{len(entries)} 条 BUY_LOW 有分钟数据可算前向 '
      f'({n_cover/len(entries)*100:.1f}%)')

valid = [e for e in entries if e['_fm'] and e['_fm']['ret30'] is not None]

def winrate(rows, key, thr=0.0):
    vals = [r['_fm'][key] for r in rows if r['_fm'] and r['_fm'].get(key) is not None]
    if not vals:
        return None
    return len(vals), sum(1 for v in vals if v > thr)/len(vals)*100, \
           sum(vals)/len(vals), sorted(vals)[len(vals)//2]

print('\n===== ② BUY_LOW 信号→后续走势（有覆盖样本） =====')
for key, label, thr in [('ret15','+15min方向胜率',0.0), ('ret30','+30min方向胜率',0.0),
                        ('ret_close','收盘方向胜率',0.0),
                        ('mfe30','30min内触+0.5%止盈比例',0.5), ('mfe60','60min内触+0.5%比例',0.5)]:
    r = winrate(valid, key, thr)
    if r:
        print(f'{label}: n={r[0]}, 胜/触率={r[1]:.1f}%, 均值={r[2]:+.3f}%, 中位={r[3]:+.3f}%')

# ---------- 4. 频率 ----------
print('\n===== ① 信号频率 =====')
per_day = collections.Counter(e['_day'] for e in entries)
print('每日 BUY_LOW 数:', {d: per_day.get(d,0) for d in RENKO_DAYS})
print(f'日均信号(入场): {len(entries)/13:.1f} 条/日, 涉及 {len(set(e["code"] for e in entries))} 只票')

# 扫描池（decision_trace 每日被扫描的票）
pool = collections.defaultdict(set)
for f in sorted(glob.glob(os.path.join(TRACES, 'decision_trace_2026-*.jsonl'))):
    day = dayof(f, 'decision_trace_')
    if day == EXCLUDE_DAY or day not in RENKO_DAYS:
        continue
    for d in iter_jsonl(f):
        pool[day].add(d['code'])
entry_set = set((e['_day'], e['code']) for e in entries)
stock_days = sum(len(pool[d]) for d in RENKO_DAYS if d in pool)
zero_sd = sum(1 for d in RENKO_DAYS for c in pool.get(d,()) if (d,c) not in entry_set)
print(f'扫描池规模/日: {sorted(set(len(pool[d]) for d in pool))} 只; '
      f'总票日={stock_days}, 零信号票日={zero_sd}, 零信号日占比={zero_sd/stock_days*100:.1f}%')
print(f'日均信号/票(按池): {len(entries)/stock_days:.2f} 条/票/日')

# 振幅与 NO_SWING 盲区（快照可得日 + decision_trace 采样近似）
def day_ohlc(code, day):
    bars = snap.get(code, {}).get(day)
    if bars and len(bars) >= 60:
        o = bars[0]['o']; h = max(b['h'] for b in bars); l = min(b['l'] for b in bars)
        return (h-l)/o*100, 'snap'
    return None, None

# decision_trace 采样价近似振幅
dt_px = collections.defaultdict(list)  # (day,code) -> [price]
for f in sorted(glob.glob(os.path.join(TRACES, 'decision_trace_2026-*.jsonl'))):
    day = dayof(f, 'decision_trace_')
    if day == EXCLUDE_DAY or day not in RENKO_DAYS:
        continue
    for d in iter_jsonl(f):
        if d.get('price'):
            dt_px[(day, d['code'])].append(d['price'])

amp_rows = []
for d in RENKO_DAYS:
    for c in pool.get(d, ()):
        amp, src = day_ohlc(c, d)
        if amp is None:
            pxs = dt_px.get((d, c))
            if len(pxs) >= 20:
                amp = (max(pxs)-min(pxs))/pxs[0]*100
                src = 'dt_approx'
        if amp is not None:
            amp_rows.append({'day': d, 'code': c, 'amp': amp, 'src': src,
                             'has_sig': (d, c) in entry_set})
n_snap_src = sum(1 for r in amp_rows if r['src']=='snap')
print(f'振幅可得票日: {len(amp_rows)}/{stock_days} (快照源 {n_snap_src}, 采样近似 {len(amp_rows)-n_snap_src})')
for thr in (3.0,):
    sub = [r for r in amp_rows if r['amp'] > thr]
    nosig = [r for r in sub if not r['has_sig']]
    print(f'振幅>{thr:.0f}% 票日: {len(sub)}, 其中零信号: {len(nosig)}, '
          f'NO_SWING盲区比例={len(nosig)/len(sub)*100 if sub else 0:.1f}%')
    # 按日看盲区
    by_day = collections.Counter(r['day'] for r in nosig)
    print('  盲区票日按日分布:', dict(sorted(by_day.items())))
    # 快照源单独口径
    sub2 = [r for r in sub if r['src']=='snap']
    nosig2 = [r for r in sub2 if not r['has_sig']]
    print(f'  [仅快照源] 振幅>{thr:.0f}%票日 {len(sub2)}, 零信号 {len(nosig2)}, '
          f'比例={len(nosig2)/len(sub2)*100 if sub2 else 0:.1f}%')

# ---------- 5. 拦截分析 ----------
print('\n===== ③ 拦截分析（shadow_signals） =====')
shadow = []
for f in sorted(glob.glob(os.path.join(TRACES, 'shadow_signals_2026-*.jsonl'))):
    day = dayof(f, 'shadow_signals_')
    if day == EXCLUDE_DAY:
        continue
    for d in iter_jsonl(f):
        d['_day'] = day
        shadow.append(d)
print(f'shadow 总条数(≤09-14): {len(shadow)}, 覆盖 {len(set(s["_day"] for s in shadow))} 天')
miss = collections.Counter(s['miss_reason'] for s in shadow)
for k, v in miss.most_common():
    print(f'  {v:4d}  {k}')

# 被拦信号事后表现（BUY 方向）
print('\n--- 被拦 BUY 信号事后表现（前向收益, 有覆盖样本） ---')
for reason in miss:
    rows = [s for s in shadow if s['miss_reason'] == reason and s.get('best_signal_type') == 'buy']
    enriched = []
    for s in rows:
        fm = fwd_metrics(s['code'], s['_day'], s['scan_time'], s['current_price'])
        if fm and fm['ret30'] is not None:
            enriched.append((s, fm))
    if not enriched:
        print(f'  {reason}: 买入向 {len(rows)} 条, 可算前向 0 条(无分钟数据)')
        continue
    r30 = [fm['ret30'] for _, fm in enriched]
    mfe = [fm['mfe30'] for _, fm in enriched]
    tp_hit = sum(1 for v in mfe if v >= 0.5)/len(mfe)*100
    pos = sum(1 for v in r30 if v > 0)/len(r30)*100
    print(f'  {reason}: 买入向 {len(rows)} 条, 可算前向 {len(enriched)} 条; '
          f'+30min均值={sum(r30)/len(r30):+.3f}%, 方向胜率={pos:.1f}%, '
          f'30min内触+0.5%比例={tp_hit:.1f}% '
          f'(→拦截"正确率": {100-tp_hit:.1f}%, 以未触止盈为拦对)')

# decision_trace HOLD_NO_SWING 等待原因归并
print('\n--- decision_trace HOLD_NO_SWING 等待原因归并 ---')
wait_norm = collections.Counter()
tot_hold = 0
for f in sorted(glob.glob(os.path.join(TRACES, 'decision_trace_2026-*.jsonl'))):
    day = dayof(f, 'decision_trace_')
    if day == EXCLUDE_DAY:
        continue
    for d in iter_jsonl(f):
        if d.get('decision_reason') != 'HOLD_NO_SWING':
            continue
        tot_hold += 1
        w = (d.get('swing_meta') or {}).get('wait') or '无wait字段'
        if w.startswith('等Renko向下砖'):
            key = '等Renko砖形方向'
        elif w.startswith('MACD15预热中'):
            key = 'MACD15预热中(早盘)'
        elif w.startswith('持仓中·距目标止盈'):
            key = '持仓中等待止盈'
        elif w.startswith('等MACD15转正'):
            key = '等MACD15转正'
        else:
            key = w[:30]
        wait_norm[key] += 1
print(f'HOLD_NO_SWING 总数: {tot_hold}')
for k, v in wait_norm.most_common():
    print(f'  {v:6d} ({v/tot_hold*100:4.1f}%)  {k}')

# ---------- 6. 时段分布 ----------
print('\n===== ④ 时段分布与胜率 =====')
bk_rows = collections.defaultdict(list)
for e in entries:
    bk_rows[bucket(e['ts'])].append(e)
for bk in ['早盘09:30-10:00','午前10:00-11:30','午后13:00-14:00','尾盘14:00-15:00']:
    rows = bk_rows.get(bk, [])
    vrows = [e for e in rows if e['_fm'] and e['_fm']['ret30'] is not None]
    line = f'{bk}: 信号 {len(rows)} 条({len(rows)/len(entries)*100:.1f}%)'
    if vrows:
        r30 = [e['_fm']['ret30'] for e in vrows]
        tp = [e['_fm']['mfe30'] for e in vrows]
        line += (f', 可算 {len(vrows)} 条, +30min胜率={sum(1 for v in r30 if v>0)/len(r30)*100:.1f}%, '
                 f'均值={sum(r30)/len(r30):+.3f}%, 触+0.5%比例={sum(1 for v in tp if v>=0.5)/len(tp)*100:.1f}%')
    print(line)

# ---------- 7. 按票差异 ----------
print('\n===== ⑤ 按票差异 =====')
name_map = {}
for e in entries:
    name_map[e['code']] = e['name']
per_code = collections.defaultdict(list)
for e in entries:
    per_code[e['code']].append(e)
print(f'{"代码":<8}{"名称":<12}{"信号数":>5}{"可算":>5}{"+30min胜率":>11}{"+30min均值":>11}{"触0.5%比例":>11}{"收盘胜率":>10}')
code_stat = []
for c, rows in sorted(per_code.items(), key=lambda kv: -len(kv[1])):
    vrows = [e for e in rows if e['_fm'] and e['_fm']['ret30'] is not None]
    if vrows:
        r30 = [e['_fm']['ret30'] for e in vrows]
        tp = [e['_fm']['mfe30'] for e in vrows]
        rc = [e['_fm']['ret_close'] for e in vrows if e['_fm']['ret_close'] is not None]
        w30 = sum(1 for v in r30 if v > 0)/len(r30)*100
        m30 = sum(r30)/len(r30)
        tpr = sum(1 for v in tp if v >= 0.5)/len(tp)*100
        wc = sum(1 for v in rc if v > 0)/len(rc)*100 if rc else float('nan')
        code_stat.append((c, name_map.get(c,''), len(rows), len(vrows), w30, m30, tpr, wc))
        print(f'{c:<8}{name_map.get(c,""):<12}{len(rows):>5}{len(vrows):>5}'
              f'{w30:>10.1f}%{m30:>+10.3f}%{tpr:>10.1f}%{wc:>9.1f}%')
    else:
        code_stat.append((c, name_map.get(c,''), len(rows), 0, None, None, None, None))
        print(f'{c:<8}{name_map.get(c,""):<12}{len(rows):>5}    0   (无分钟数据)')

# ---------- 8. 大盘共振 ----------
print('\n===== ⑥ 大盘共振 gate 与信号表现 =====')
res = []
for f in sorted(glob.glob(os.path.join(TRACES, 'index_resonance_2026-*.jsonl'))):
    day = dayof(f, 'index_resonance_')
    if day == EXCLUDE_DAY:
        continue
    for d in iter_jsonl(f):
        d['_day'] = day
        res.append(d)
print(f'index_resonance 条数: {len(res)}, 覆盖日: {sorted(set(r["_day"] for r in res))}')
for gp in (True, False):
    rows = [r for r in res if r.get('gate_pass') == gp and r.get('action') == 'BUY_LOW']
    enriched = []
    for r in rows:
        fm = fwd_metrics(r['code'], r['_day'], r['scan_time'], r['price'])
        if fm and fm['ret30'] is not None:
            enriched.append((r, fm))
    if enriched:
        r30 = [fm['ret30'] for _, fm in enriched]
        tp = [fm['mfe30'] for _, fm in enriched]
        print(f'gate_pass={gp}: BUY {len(rows)} 条, 可算 {len(enriched)} 条, '
              f'+30min胜率={sum(1 for v in r30 if v>0)/len(r30)*100:.1f}%, '
              f'均值={sum(r30)/len(r30):+.3f}%, 触+0.5%比例={sum(1 for v in tp if v>=0.5)/len(tp)*100:.1f}%')
    else:
        print(f'gate_pass={gp}: BUY {len(rows)} 条, 可算 0 条')

# ---------- 9. manual 通道活跃度 ----------
print('\n===== 附: manual 通道(manual_signals)活跃度 =====')
ev = collections.Counter(); verdict = collections.Counter()
for f in sorted(glob.glob(os.path.join(LOGS, 'manual_signals_2026-*.jsonl'))):
    day = dayof(f, 'manual_signals_')
    if day == EXCLUDE_DAY:
        continue
    for d in iter_jsonl(f):
        if d.get('event') == 'build_signal':
            verdict[(d.get('verdict'), bool(d.get('pushed')))] += 1
print(dict(verdict))

# ---------- 10. 出口(成对)表现速览 ----------
print('\n===== 附: SELL_HIGH 出口速览 =====')
real_pnl = []
for x in exits:
    ep, sp = x.get('entry_price'), x.get('price')
    if ep and sp:
        real_pnl.append((sp/ep-1)*100)
reason = collections.Counter((x.get('exit_reason') or '')[:14] for x in exits)
print('exit_reason 分布:', dict(reason.most_common()))
if real_pnl:
    print(f'成对交易毛收益: n={len(real_pnl)}, 均值={sum(real_pnl)/len(real_pnl):+.3f}%, '
          f'中位={sorted(real_pnl)[len(real_pnl)//2]:+.3f}%, '
          f'<0.136%双边成本占比={sum(1 for v in real_pnl if v<0.136)/len(real_pnl)*100:.1f}%')
