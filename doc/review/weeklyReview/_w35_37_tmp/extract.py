import sys, re, glob, os
sys.stdout.reconfigure(encoding='utf-8')
for fp in sorted(glob.glob('doc/review/dailyReview/2026-0[89]-*_复盘.md')):
    date = os.path.basename(fp)[:10]
    if date < '2026-08-17':
        continue
    lines = open(fp, encoding='utf-8').read().splitlines()
    idxs = [i for i, l in enumerate(lines) if re.match(r'^#{2,4} .*建仓', l)]
    for i in idxs:
        level = len(re.match(r'^#+', lines[i]).group())
        j = i + 1
        while j < len(lines) and not (lines[j].startswith('#') and len(re.match(r'^#+', lines[j]).group()) <= level):
            j += 1
        seg = lines[i:j]
        print('='*20, os.path.basename(fp), 'lines', i+1, '-', j, '='*10)
        print('\n'.join(seg)[:2200])
        print()
