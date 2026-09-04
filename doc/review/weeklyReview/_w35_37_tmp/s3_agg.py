import sys, json, glob
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding='utf-8')

with open('t_io/validation/signal_outcomes.json', encoding='utf-8') as f:
    recs = json.load(f)['records']
by_dc = {(r['date'], r['code']): r for r in recs}
by_code = defaultdict(list)
for r in recs: by_code[r['code']].append(r)

veto_pairs = {}
for fp in sorted(glob.glob('t_io/traces/position_builder_2026-*.jsonl')):
    date = fp.split('_')[-1].replace('.jsonl', '')
    if date < '2026-08-17':
        continue
    with open(fp, encoding='utf-8') as f:
        for ln in f:
            d = json.loads(ln)
            veto = (d.get('timing') or {}).get('veto') or []
            if veto:
                key = (date, d['code'])
                e = veto_pairs.setdefault(key, {'name': d.get('name'), 'verdict': d.get('verdict'), 'vetoes': set(), 'hits': 0})
                e['vetoes'].update(veto); e['hits'] += 1

print('=== t_veto 去重 (date,code) 对数:', len(veto_pairs))
for (date, code), v in sorted(veto_pairs.items()):
    print(date, code, v['name'], 'verdict=', v['verdict'], 'hits=', v['hits'], 'veto=', sorted(v['vetoes']))
print()
print('=== 被否决股 outcomes 表现 ===')
for (date, code), v in sorted(veto_pairs.items()):
    r = by_dc.get((date, code))
    if not r or not r.get('returns'):
        cands = [c for c in by_code.get(code, []) if c['date'] >= date and c.get('returns')]
        r = cands[0] if cands else None
    if not r:
        print(date, code, v['name'], ': outcomes 无记录/无收益')
        continue
    out = []
    for hz in ('1', '3', '5'):
        h = (r['returns'] or {}).get(hz)
        if h:
            ret = h.get('ret'); mdd = h.get('max_drawdown'); out.append('T+%s=%s(mdd=%s,%s)' % (hz, ('%.2f%%' % (ret*100)) if ret is not None else h.get('status'), ('%.1f%%' % (mdd*100)) if mdd is not None else 'NA', h['status']))
        else:
            out.append('T+%s=无' % hz)
    print('veto日%s %s %s | outcome信号日%s verdict=%s | %s' % (date, code, r['name'], r['date'], r['verdict'], ' '.join(out)))

print()
print('=== signal 股逐只明细 ===')
for r in recs:
    if r['verdict'] == 'signal':
        out = []
        for hz in ('1', '3', '5'):
            h = (r.get('returns') or {}).get(hz)
            if h:
                ret = h.get('ret'); mdd = h.get('max_drawdown'); out.append('T+%s(%s): %s mdd=%s %s' % (hz, h['date'], ('%.2f%%' % (ret*100)) if ret is not None else h.get('status'), ('%.2f%%' % (mdd*100)) if mdd is not None else 'NA', h['status']))
            else:
                out.append('T+%s: 无' % hz)
        print(r['date'], r['code'], r['name'], 'score=%s price=%s' % (r['score'], r['price']), '|', ' | '.join(out))
