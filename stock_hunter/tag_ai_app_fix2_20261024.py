"""2026-10-24 AI应用整治·补丁：修两只 legacy 多token 股票的概念串
deepclean 脚本的 legacy 分支只认 category=='AI应用' 精确匹配，这两只是 'AI应用|网络安全'，跳过。
直接改 legacy_concept（legacy 模式下概念串对两个分类行共用，保持混合串但收敛主标签）。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
FIX = {'300770': 'AI影视短剧|AI监管-内容审核',
       '601360': 'AI大模型|AI安全'}

obj = json.loads(WL.read_text(encoding='utf-8'))
for code, concept in FIX.items():
    obj[code]['jiuyan_concept'] = concept
WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')

obj2 = json.loads(WL.read_text(encoding='utf-8'))
for code in FIX:
    e = obj2[code]
    print(code, e.get('name'), '→', e.get('jiuyan_category'), '/', e.get('jiuyan_concept'))

# 终验：AI应用面板不应再有裸'AI应用'/'短剧'主概念
def rows_of(e):
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            pairs.append((c, e.get(f'jiuyan_concept{k}', '')))
    if pairs:
        return pairs
    lc = (e.get('jiuyan_category') or '').strip()
    lp = e.get('jiuyan_concept', '')
    if isinstance(lp, (list, tuple)):
        lp = '|'.join(map(str, lp))
    return [(t.strip(), lp) for t in lc.split('|') if t.strip()] if lc else []
bad = []
n = 0
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    for c, p in rows_of(e):
        if c == 'AI应用':
            n += 1
            if not p or str(p).split('|')[0] in ('AI应用', '短剧', '其他', '自研'):
                bad.append((code, e.get('name', ''), p))
            break
print(f'\n终验: AI应用面板 {n} 行, 残留裸概念 {len(bad)}: {bad}')
