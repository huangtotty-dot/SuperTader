# -*- coding: utf-8 -*-
"""
D3 执行层损耗与事故诊断 —— auto 通道
数据窗口: 2026-08-31 ~ 2026-09-14 (11 个交易日), 2026-09-15 盘中数据仅作旁注, 不入统计
只读 t_io/, 输出统计 JSON 到本目录
"""
import sys, json, glob, collections, os, re
sys.stdout.reconfigure(encoding='utf-8')

ROOT = r'E:\superTrader'
OUT  = r'E:\superTrader\doc\diagnosis\2026-09-15_做T能力专题诊断'
LAST_DAY = '20260914'          # 统计截止(含)
DAYS = ['20260831','20260901','20260902','20260903','20260904','20260907',
        '20260908','20260909','20260910','20260911','20260914']
COST_RT = 0.00136              # 双边成本


def day_of(path):
    m = re.search(r'(20\d{2})-?(\d{2})-?(\d{2})', os.path.basename(path))
    return m.group(1)+m.group(2)+m.group(3) if m else ''

def load_jsonl(path):
    rows, dirty = [], 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: rows.append(json.loads(line))
            except Exception: dirty += 1
    return rows, dirty

R = {}   # 结果收集
dirty_total = 0

# ============ 加载 bridge events ============
evs, evs_0915 = [], []
for f in sorted(glob.glob(os.path.join(ROOT, 't_io/bridge/events_2026*.jsonl'))):
    day = day_of(f)
    rows, d = load_jsonl(f); dirty_total += d
    for e in rows: e['_day'] = day
    if day <= LAST_DAY: evs.extend(rows)
    else: evs_0915.extend(rows)
R['数据范围'] = {'统计窗口': f'{DAYS[0]}~{DAYS[-1]}', '交易日数': len(DAYS),
               '事件总数_窗口内': len(evs), '事件_0915旁注': len(evs_0915), '脏行': dirty_total}

def by(ev, arr=None):
    return [e for e in (arr if arr is not None else evs) if e.get('event') == ev]

# ============ ① 通道分布: 信号 / 成交 / 损益贡献 ============
sig_c  = collections.Counter((e.get('action')) for e in by('signal'))
sig_d  = collections.Counter((e['_day'], e.get('action')) for e in by('signal'))
fills  = by('fill'); orders = by('order'); rej = by('reject')
fill_side = collections.Counter(e['side'] for e in fills)

# 卖出腿 (buyback_armed 带 sell_action) 与回补 (buyback_filled) 配对
armed  = by('buyback_armed'); filled = by('buyback_filled')
armed_by_action = collections.Counter(e.get('sell_action') for e in armed)
# 配对: code + round(sell_price,3) + filled._ts >= armed._ts, 一次性消耗
used = set(); leg_rows = []
for a in armed:
    key_p = round(a['sell_price'], 3)
    m = None
    for i, fl in enumerate(filled):
        if i in used: continue
        if fl['code'] == a['code'] and round(fl['sell_price'], 3) == key_p and fl['_ts'] >= a['_ts']:
            m = (i, fl); break
    qty = a.get('qty', 0)
    if m:
        i, fl = m; used.add(i)
        q = fl.get('qty', qty)
        gross = (a['sell_price'] - fl['price']) * q
        cost  = COST_RT * a['sell_price'] * q
        leg_rows.append({'action': a.get('sell_action'), 'code': a['code'], 'qty': q,
                         'sell': a['sell_price'], 'buy': fl['price'],
                         'premium_pct': (fl['price']/a['sell_price']-1)*100,
                         'gross': gross, 'cost': cost, 'net': gross-cost,
                         'hold_min': (fl['_ts']-a['_ts'])/60, 'day': a['_day'], 'state':'filled'})
    else:
        leg_rows.append({'action': a.get('sell_action'), 'code': a['code'], 'qty': qty,
                         'sell': a['sell_price'], 'day': a['_day'], 'state':'open'})

chan = {}
for a in set(x['action'] for x in leg_rows):
    rows = [x for x in leg_rows if x['action']==a]
    done = [x for x in rows if x['state']=='filled']
    chan[a] = {'卖出腿数': len(rows), '已回补': len(done), '未回补': len(rows)-len(done),
               '毛损益': round(sum(x['gross'] for x in done),1),
               '成本扣减': round(sum(x['cost'] for x in done),1),
               '净损益': round(sum(x['net'] for x in done),1),
               '回补溢价均值%': round(sum(x['premium_pct'] for x in done)/len(done),3) if done else None,
               '持有分钟_中位': round(sorted(x['hold_min'] for x in done)[len(done)//2],1) if done else None}
R['①通道分布'] = {'信号数': dict(sig_c), '信号按日': {f'{k[0]}_{k[1]}': v for k,v in sorted(sig_d.items())},
                '成交笔数': dict(fill_side), 'armed按通道': dict(armed_by_action), '通道损益': chan}

# ============ ② 拒单与拦截归因 ============
rej_rows = [{'day': e['_day'], 'code': e['code'], 'side': e['side'], 'reason': e['reason']} for e in rej]
risk_kinds = collections.Counter(e.get('kind') for e in by('risk'))
risk_day   = collections.Counter((e['_day'], e.get('kind')) for e in by('risk'))

# backtrace: 区分实盘 run 与回放/复盘 run
bt, bt_dirty = load_jsonl(os.path.join(ROOT, 't_io/logs/auto_backtrace.jsonl'))
dirty_total += bt_dirty
def is_live(e):
    rid = e.get('_run_id','')
    d = e.get('time','')[:10].replace('-','')
    return (rid[:8] == d and rid[9:13] <= '1530' and DAYS[0] <= d <= DAYS[-1])
bt_live   = [e for e in bt if is_live(e)]
bt_replay = [e for e in bt if not is_live(e)]
bt_c  = collections.Counter(e.get('event') for e in bt_live)
block_map = {  # 归类: 资金/地板/冷却/仓位闸/趋势闸/时机闸/其他
    'slot_full':'仓位闸(槽位满)', 'max_pos_cap':'仓位闸(个股上限)', 'buyback_capped':'仓位闸(回补帽)',
    'sell_skip':'地板(底仓保护)', 'morning_sell_blocked':'时机闸(开盘缓冲)',
    'inflight_skip':'冷却/在途(inflight)', 'buy_blocked':'硬止损/资金', 'build_gate_block':'趋势闸(build_gate)'}
blocks = collections.Counter()
for e in bt_live:
    ev = e.get('event')
    if ev in block_map: blocks[block_map[ev]] += 1
# risk 侧细分 base_deferred 原因
bd_reasons = collections.Counter()
for e in by('risk'):
    if e.get('kind') == 'base_deferred':
        d = e.get('detail','')
        r = 'slot_full' if 'slot_full' in d else ('TREND_BREAKDOWN' if 'TREND_BREAKDOWN' in d else d[:30])
        bd_reasons[r] += 1
R['②拒单拦截'] = {
    'reject逐条': rej_rows,
    'reject分类': {'交易账号未登录(GMTERM)': sum(1 for e in rej if 'GMTERM' in e['reason'])},
    'risk事件分类': dict(risk_kinds),
    'risk按日': {f'{k[0]}_{k[1]}': v for k,v in sorted(risk_day.items())},
    'base_deferred细分': dict(bd_reasons),
    'backtrace实盘拦截分类': dict(blocks),
    'backtrace污染': {'总行数': len(bt), '实盘run行数': len(bt_live), '回放/复盘run行数': len(bt_replay)}}

# ============ ③ 执行质量: 延迟与滑点 ============
signals = by('signal')
s2o, o2f, slips = [], [], []
used_o, used_f = set(), set()
sig_side = lambda a: 'SELL' if a in ('SELL_HIGH','TARGET_SELL','TREND_EXIT','PANIC_SELL','TAIL') else 'BUY'
for s in signals:
    sd = sig_side(s.get('action','BUY'))
    best = None
    for j, o in enumerate(orders):
        if j in used_o: continue
        if o['code']==s['code'] and o['side']==sd and 0 <= o['_ts']-s['_ts'] <= 300:
            if best is None or o['_ts']-s['_ts'] < best[0]: best = (o['_ts']-s['_ts'], j)
    if best:
        d, j = best; used_o.add(j); s2o.append(d); o = orders[j]
        for k, fl in enumerate(fills):
            if k in used_f: continue
            if fl['code']==o['code'] and fl['side']==o['side'] and 0 <= fl['_ts']-o['_ts'] <= 900:
                used_f.add(k); o2f.append(fl['_ts']-o['_ts'])
                if o.get('price'):
                    slip = (fl['price']-o['price'])/o['price']*100
                    slips.append(slip if o['side']=='BUY' else -slip)  # 正=不利方向
                break
def stat(a):
    if not a: return None
    a2 = sorted(a); n=len(a2)
    return {'n': n, '均值': round(sum(a2)/n,2), '中位': round(a2[n//2],2),
            'p90': round(a2[int(n*0.9)],2), '最大': round(a2[-1],2)}
R['③执行质量'] = {'信号→委托延迟_s': stat(s2o), '委托→成交延迟_s': stat(o2f),
                '滑点_正为不利_%': stat(slips), '滑点明细': [round(x,3) for x in slips]}

# ============ ④ 回补链健康 ============
delayed   = by('buyback_delayed'); downg = by('buyback_downgrade')
blocked   = by('buyback_blocked'); expired = by('buyback_expired'); restored = by('buyback_restored')
prem_filled  = [round(x['premium_pct'],2) for x in leg_rows if x['state']=='filled']
prem_downg   = [round(e.get('premium',0)*100,2) for e in downg]
tail_armed   = [e for e in armed if e.get('sell_action')=='TAIL']
tail_filled  = [x for x in leg_rows if x['action']=='TAIL' and x['state']=='filled']
forced_live  = [e for e in bt_live if e.get('event')=='tail_buyback_forced']
open_legs = [x for x in leg_rows if x['state']=='open']
R['④回补链'] = {
    'armed': len(armed), 'filled': len([x for x in leg_rows if x['state']=='filled']),
    'delayed': len(delayed), 'downgrade': len(downg), 'blocked': len(blocked), 'expired': len(expired),
    'restored跨日恢复': len(restored),
    '转化率%': round(100*len([x for x in leg_rows if x['state']=='filled'])/max(len(armed),1),1),
    'filled溢价分布%': sorted(prem_filled), 'downgrade溢价分布%': sorted(prem_downg),
    'delayed溢价分布%': sorted(round(e.get('premium',0)*100,2) for e in delayed),
    'TAIL通道': {'armed': len(tail_armed), 'filled': len(tail_filled),
                '净损益': round(sum(x['net'] for x in tail_filled),1)},
    'tail_buyback_forced实盘run': len(forced_live),
    '未回补开口腿': [{'day':x['day'],'code':x['code'],'qty':x['qty'],'sell':round(x['sell'],3)} for x in open_legs],
    '回补净损益合计': round(sum(x['net'] for x in leg_rows if x['state']=='filled'),1),
    '高接腿数_premium>0': sum(1 for x in leg_rows if x['state']=='filled' and x['premium_pct']>0.01)}

# ============ ⑤ 尾盘归位 ============
ca, ca_dirty = load_jsonl(os.path.join(ROOT, 't_io/logs/closure_audit.jsonl')); dirty_total += ca_dirty
ca_win = [e for e in ca if DAYS[0] <= e['date'].replace('-','') <= DAYS[-1]]
tail_bad = []
for e in ca_win:
    if e.get('phase') == 'tail_reconcile': continue
    if not e.get('ok'):
        probs = [p for p in e.get('problems',[]) if '模拟盘' not in p]
        unreb = sum(d.get('unrebuilt',0) for d in e.get('details',[]))
        uncls = sum(d.get('unclosed_buy',0) for d in e.get('details',[]))
        tail_bad.append({'date': e['date'], 'problems实口径': len(probs), '未接回股数': unreb, '未平正T股数': uncls,
                         '问题原文': e.get('problems',[])})
# 14:50 后仍有 order/fill
late = [e for e in orders+fills if e.get('time','')[11:16] >= '14:50']
R['⑤尾盘归位'] = {'closure_audit窗口条数': len(ca_win),
                'ok=false天数(去重)': len(set(x['date'] for x in tail_bad)), '明细': tail_bad,
                '14:50后委托/成交笔数': len(late)}

# ============ ⑥ 事故清单 ============
# 6a heartbeat 缺口
hb_gaps = []
for f in sorted(glob.glob(os.path.join(ROOT, 't_io/bridge/heartbeat_2026-*.jsonl'))):
    day = day_of(f)
    if day > LAST_DAY: continue
    rows, d = load_jsonl(f); dirty_total += d
    ts = [e['_ts'] for e in rows if '_ts' in e]
    if not ts: continue
    gaps = []
    for a, b in zip(ts, ts[1:]):
        if b - a > 120:
            from datetime import datetime
            gaps.append({'起': datetime.fromtimestamp(a).strftime('%H:%M:%S'),
                         '止': datetime.fromtimestamp(b).strftime('%H:%M:%S'),
                         '缺口_s': round(b-a)})
    from datetime import datetime
    hb_gaps.append({'day': day, '心跳条数': len(ts),
                    '首条': datetime.fromtimestamp(ts[0]).strftime('%H:%M:%S'),
                    '末条': datetime.fromtimestamp(ts[-1]).strftime('%H:%M:%S'),
                    '缺口>120s': gaps})
# 6b BUY_PENDING 生命周期
reqs = {e['request_id']: e for e in by('buy_confirm_request')}
outc = collections.Counter()
pend = []
for rid, r in reqs.items():
    o = '挂起未决(截至窗口末)'
    for e in by('buy_confirm_superseded'):
        if e.get('request_id')==rid: o='superseded被回补取代'
    for e in by('buy_confirm_approved_not_executed'):
        if e.get('request_id')==rid: o='approved但未执行(超时300s)'
    for e in by('buy_confirm_expired'):
        if e.get('request_id')==rid: o='跨日作废'
    outc[o]+=1
    pend.append({'rid': rid, 'day': r['_day'], 'action': r.get('action'), '结局': o})
# 6c 推送失败
push_fail, push_tot = [], 0
for f in sorted(glob.glob(os.path.join(ROOT, 't_io/bridge/pushes_2026*.jsonl'))):
    day = day_of(f)
    if day > LAST_DAY: continue
    rows, d = load_jsonl(f); dirty_total += d
    push_tot += len(rows)
    for e in rows:
        if e.get('sent') is False:
            push_fail.append({'day': day, 'time': e.get('time'), 'title': e.get('title')})
# 6d 其他
sess_down = [e for e in by('risk') if e.get('kind')=='session_down']
strat_exit_day = collections.Counter(e['_day'] for e in by('risk') if e.get('kind')=='strategy_exit')
R['⑥事故清单'] = {
    'heartbeat逐日': hb_gaps,
    'BUY_PENDING结局分布': dict(outc), 'BUY_PENDING逐条': pend,
    '推送总数': push_tot, '推送失败sent=false': push_fail,
    'session_down': [{'day':e['_day'],'detail':e.get('detail','')[:100]} for e in sess_down],
    'strategy_exit按日': dict(strat_exit_day),
    'buy_pending_expired风险': [e.get('detail','')[:80] for e in by('risk') if e.get('kind')=='buy_pending_expired']}

# ============ ⑦ 复盘交叉验证 ============
kw = ['GMTERM','未登录','断链','挂起','崩溃','冻结','事故','拒单','BUY_PENDING','session_down','中断','死亡','失控']
cross = []
for d in DAYS:
    p = os.path.join(ROOT, 'doc/review/dailyReview', f'{d[:4]}-{d[4:6]}-{d[6:]}_复盘.md')
    if not os.path.exists(p):
        cross.append({'day': d, '复盘': '缺失'}); continue
    txt = open(p, encoding='utf-8').read()
    hits = {k: txt.count(k) for k in kw if txt.count(k)}
    grade = re.search(r'总判定[：:]\s*((?:🔴|🟡|🟢)[^\n|—]{0,10})', txt)
    cross.append({'day': d, '复盘': '存在', '判定': grade.group(1).strip() if grade else '?',
                  '关键词命中': hits})
R['⑦复盘交叉验证'] = cross
R['数据质量'] = {'脏行合计': dirty_total,
              'backtrace回放污染行': len(bt_replay),
              '说明': ['fill 无 order_id, 信号→委托→成交按 code+side+时间窗贪心配对, 可能错配',
                      'auto 通道为仿真/镜像口径(见 closure_audit 模拟盘标注), 损益为镜像口径',
                      'buyback_filled 的 fill_qty 为整笔成交量, matched qty 为回补对应量',
                      '09-15 数据不入统计']}

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, 'D3_stats.json'), 'w', encoding='utf-8') as f:
    json.dump(R, f, ensure_ascii=False, indent=2, default=str)
print(json.dumps(R, ensure_ascii=False, indent=2, default=str))
