import sys, json, glob
from collections import Counter, defaultdict
sys.stdout.reconfigure(encoding='utf-8')

with open('t_io/validation/signal_outcomes.json', encoding='utf-8') as f:
    recs = json.load(f)['records']

# 从 position_builder 推断每条 outcome 的通道归属（取当日该 code 最后一次扫描）
chan_map = {}
for fp in sorted(glob.glob('t_io/traces/position_builder_2026-*.jsonl')):
    date = fp.split('_')[-1].replace('.jsonl', '')
    if date < '2026-08-17':
        continue
    with open(fp, encoding='utf-8') as f:
        for ln in f:
            d = json.loads(ln)
            chs = d.get('channels') or {}
            ice = (chs.get('iceberg') or {}).get('verdict')
            brk = (chs.get('breakout') or {}).get('verdict')
            chan_map[(date, d.get('code'))] = {'iceberg': ice, 'breakout': brk, 'channel': d.get('channel'), 'verdict': d.get('verdict')}

groups = defaultdict(list)
no_map = 0
for r in recs:
    m = chan_map.get((r['date'], r['code']))
    if not m:
        no_map += 1
        continue
    for ch in ('iceberg', 'breakout'):
        if m[ch] in ('signal', 'approaching', 'watch_signal'):
            groups[ch].append((r, m[ch]))
print('outcomes 无法在 position_builder 找到映射:', no_map, '/', len(recs))
for ch, items in groups.items():
    print()
    print('=== 通道 %s: 映射 outcome 行 %d（verdict 分布 %s）===' % (ch, len(items), dict(Counter(v for _, v in items))))
    for hz in ('1', '3', '5'):
        ok = [(r, v) for r, v in items if r.get('returns') and r['returns'].get(hz) and r['returns'][hz].get('status') == 'ok']
        if not ok:
            print('  T+%s: 无结算行' % hz); continue
        wins = sum(1 for r, _ in ok if r['returns'][hz]['ret'] > 0)
        avg = sum(r['returns'][hz]['ret'] for r, _ in ok) / len(ok)
        print('  T+%s: n=%d win=%.1f%% avg=%.2f%%' % (hz, len(ok), wins/len(ok)*100, avg*100))

print()
print('=== signal 6行的通道归属 ===')
for r in recs:
    if r['verdict'] == 'signal':
        print(r['date'], r['code'], r['name'], chan_map.get((r['date'], r['code'])))
