"""2026-10-24 AI应用整治·步骤一补：真正「挪出」——删除数据要素概念股的 AI应用 配对
背景：上午的 move 脚本只把（数据要素,…）对提到第 1 位，但猎手面板按**每个配对一行**分组，
AI应用 对仍在 ⇒ 面板"没有改变"。本脚本把 13 只股票的 ('AI应用',…) 配对整对删除：
  · 11 只 = AI应用概念里带 数据要素_* 的（owner 10-24 指令"挪过去"）
  · +2 只 = 航天信息/中广天择：10-21 已归数据要素板块，残留的裸 ('AI应用','AI应用') 对是噪音
其余配对（数据要素/网络安全 等）原样保留。幂等：无 AI应用 对则跳过。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
CODES = ['000681', '002235', '002642', '300226', '300250', '300364',
         '300766', '603000', '603533', '688500', '688787',
         '600271', '603721']

obj = json.loads(WL.read_text(encoding='utf-8'))
removed, noop = [], []
for code in CODES:
    e = obj.get(code)
    if not isinstance(e, dict):
        noop.append(code + '(不存在)')
        continue
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        p = (e.get(f'jiuyan_concept{k}') or '').strip()
        if c or p:
            pairs.append((c, p))
    new_pairs = [(c, p) for (c, p) in pairs if c != 'AI应用']
    if len(new_pairs) == len(pairs):
        noop.append(code)
        continue
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(new_pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        e[f'jiuyan_concept{k}'] = p
    removed.append(code)

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print(f'删除AI应用配对 {len(removed)} 只: {removed}')
print(f'无配对跳过 {len(noop)}: {noop}')

# 重载验证：模拟 data_loader 口径（有配对用配对，否则 legacy 按|拆）统计面板行数
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def panel_cats(e):
    cats = [(e.get(f'jiuyan_category{k}') or '').strip() for k in range(1, 10)]
    cats = [c for c in cats if c]
    if cats:
        return cats
    legacy = (e.get('jiuyan_category') or '').strip()
    return [c.strip() for c in legacy.split('|') if c.strip()] if legacy else []
from collections import Counter
rows = Counter()
for e in obj2.values():
    if isinstance(e, dict):
        for c in panel_cats(e):
            rows[c] += 1
print(f"\n面板行数验证: AI应用={rows.get('AI应用')} (原133) / 数据要素={rows.get('数据要素')}")
