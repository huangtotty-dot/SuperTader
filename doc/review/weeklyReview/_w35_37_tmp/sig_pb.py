import sys, json, glob
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding='utf-8')
# position_builder 中 verdict=signal 的 (date,code) 去重 + 首次扫描时间
sig = defaultdict(lambda: {'name':None,'first':None,'n':0,'pushed':None,'scores':set()})
for fp in sorted(glob.glob('t_io/traces/position_builder_2026-*.jsonl')):
    date = fp.split('_')[-1].replace('.jsonl','')
    if date < '2026-08-17': continue
    with open(fp, encoding='utf-8') as f:
        for ln in f:
            d = json.loads(ln)
            if d.get('verdict') == 'signal':
                e = sig[(date, d['code'])]
                e['name'] = d.get('name'); e['n'] += 1
                st = d.get('scan_time')
                if e['first'] is None or st < e['first']: e['first'] = st
                e['scores'].add(d.get('composite_score'))
                ic = d.get('intraday_confirm')
                if isinstance(ic, dict) and ic.get('pushed'): e['pushed'] = True
print('position_builder verdict=signal 去重 (date,code):', len(sig))
for (date,code), e in sorted(sig.items()):
    print(date, code, e['name'], 'rows=', e['n'], 'first=', e['first'], 'scores=', sorted(e['scores']), 'pushed=', e['pushed'])
# intraday_confirm 字段结构
print()
for fp in ['t_io/traces/position_builder_2026-09-01.jsonl']:
    with open(fp, encoding='utf-8') as f:
        for ln in f:
            d = json.loads(ln)
            if d.get('verdict') == 'signal':
                print(json.dumps({k: d.get(k) for k in ('scan_time','code','name','composite_score','channel','approach_status','gated','gated_from','intraday_confirm','conditions','suggested_qty')}, ensure_ascii=False)[:800])
                break
