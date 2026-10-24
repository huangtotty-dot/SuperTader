"""2026-10-24 AI应用整治·步骤一：数据要素概念股挪入「数据要素」板块
规则：指定股票的 category/concept 配对中，将 ('数据要素', ...) 对提升为第 1 位，
其余配对顺序后移保持相对次序。幂等：已第一位的跳过。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
CODES = ['000681', '002235', '002642', '300226', '300250', '300364',
         '300766', '603000', '603533', '688500', '688787']

obj = json.loads(WL.read_text(encoding='utf-8'))
moved, skipped, missing = [], [], []
for code in CODES:
    e = obj.get(code)
    if not isinstance(e, dict):
        missing.append(code)
        continue
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        p = (e.get(f'jiuyan_concept{k}') or '').strip()
        if c or p:
            pairs.append((c, p))
    idx = next((i for i, (c, _) in enumerate(pairs) if c == '数据要素'), None)
    if idx is None:
        missing.append(code + '(无数据要素配对)')
        continue
    if idx == 0:
        skipped.append(code)
        continue
    pairs.insert(0, pairs.pop(idx))
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        e[f'jiuyan_concept{k}'] = p
    moved.append(code)

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print(f'挪移 {len(moved)} 只: {moved}')
print(f'已在首位跳过 {len(skipped)}: {skipped}')
print(f'异常 {len(missing)}: {missing}')

# 重载验证：GUI 口径两板块票数
import re
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def first_cat(e):
    for k in range(1, 10):
        v = (e.get(f'jiuyan_category{k}') or '').strip()
        if v:
            return {'创新药': '医药', '中药': '医药'}.get(v, v)
    v = (e.get('jiuyan_category') or '').strip()
    return re.split(r'\s*\|\s*', v)[0].strip() if v else ''
from collections import Counter
cnt = Counter(first_cat(e) for e in obj2.values() if isinstance(e, dict))
print(f"\n验证 GUI 口径: AI应用={cnt.get('AI应用')} (原121) / 数据要素={cnt.get('数据要素')} (原44)")
