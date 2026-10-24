"""2026-10-24 AI应用·AI语料概念归并（owner 选方案 1）

"AI语料"描述的是"内容可作训练数据"这一资产属性，不是业务分类；按 owner 决定，
并入数据要素，与中文在线/掌阅的"小说数据"统一口径为 **数据要素_内容数据**。

  301231 荣信文化  AI应用/AI语料 → 数据要素/数据要素_内容数据（少儿图书版权库）
  601999 出版传媒  AI应用/AI语料 → 数据要素/数据要素_内容数据（辽海/辽教社教材教辅+一般图书）
  603999 读者传媒  AI应用/AI语料 → 数据要素/数据要素_内容数据（《读者》期刊+图书）
  300364 中文在线  数据要素_小说数据 → 数据要素_内容数据（保留 AI应用/AI影视短剧）
  603533 掌阅科技  数据要素_小说数据 → 数据要素_内容数据（保留 AI应用/AI影视短剧）

- 小说数据归零后由 内容数据 承接，不再出现"小说/图书/期刊"三套口径。
- 三家出版社离开 AI应用 面板（其业务是出版，不是 AI 应用），仍在猎手池（数据要素非空）。
- 同时写入 jiuyan_concept_desc（GUI tooltip 口径，参照医药注释样式）。

幂等；改前备份 backup/watchlist_jiuyan_20261024_before_yuliao_merge.json。
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_yuliao_merge.json')
NOW = '2026-10-24 17:05:00'

obj = json.loads(WL.read_text(encoding='utf-8'))
BK.parent.mkdir(parents=True, exist_ok=True)
if not BK.exists():
    shutil.copy2(WL, BK)

def norm(x):
    if x is None:
        return ''
    if isinstance(x, (list, tuple)):
        return '|'.join(str(v).strip() for v in x if str(v).strip())
    return str(x).strip()

def get_pairs(e):
    out = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            out.append((c, norm(e.get(f'jiuyan_concept{k}'))))
    if out:
        return out
    lc = (e.get('jiuyan_category') or '').strip()
    lp = norm(e.get('jiuyan_concept'))
    return [(t.strip(), lp) for t in lc.split('|') if t.strip()] if lc else []

def put_pairs(e, pairs):
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        if p:
            e[f'jiuyan_concept{k}'] = p
    e['jiuyan_category'] = '|'.join(c for c, _ in pairs)
    e['jiuyan_concept'] = '|'.join(p for _, p in pairs if p)
    e['updated_at'] = NOW

CONTENT = '数据要素_内容数据'
DESC = ('手握规模化正版内容库（小说、图书、教材教辅、期刊）的出版与阅读公司——'
        '内容可作为大模型训练语料对外授权。看得懂的一句话：「有版权内容库」的出版/阅读公司。')

EDITS = {
    '301231': ([('AI应用', 'AI语料')], [('数据要素', CONTENT)], '荣信文化（少儿图书版权库）'),
    '601999': ([('AI应用', 'AI语料')], [('数据要素', CONTENT)], '出版传媒（教材教辅+一般图书）'),
    '603999': ([('AI应用', 'AI语料')], [('数据要素', CONTENT)], '读者传媒（期刊+图书）'),
    '300364': ([('AI应用', 'AI影视短剧'), ('数据要素', '数据要素_小说数据')],
               [('AI应用', 'AI影视短剧'), ('数据要素', CONTENT)], '中文在线（小说数据→内容数据）'),
    '603533': ([('AI应用', 'AI影视短剧'), ('数据要素', '数据要素_小说数据')],
               [('AI应用', 'AI影视短剧'), ('数据要素', CONTENT)], '掌阅科技（小说数据→内容数据）'),
}

log, skipped = [], []
for code, (expect, new, why) in EDITS.items():
    e = obj.get(code)
    if not e:
        skipped.append(f'{code} 不存在')
        continue
    cur = get_pairs(e)
    if cur == new:
        log.append(f'{code} {e.get("name")} 已是目标状态，跳过')
    elif cur != expect:
        skipped.append(f'{code} {e.get("name")} 现状{cur} != 预期{expect}，跳过')
        continue
    else:
        put_pairs(e, new)
        log.append(f'{code} {e.get("name")} {why}：{expect} → {new}')
    # 概念注释（只增不覆盖）
    d = e.get('jiuyan_concept_desc')
    if not isinstance(d, dict):
        d = {}
    if not d.get(CONTENT):
        d[CONTENT] = DESC
        e['jiuyan_concept_desc'] = d
        log.append(f'    + concept_desc[{CONTENT}]')

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print('\n'.join(log))
if skipped:
    print('\n!! 跳过:')
    print('\n'.join(skipped))

# ── 重载验证 ──
obj2 = json.loads(WL.read_text(encoding='utf-8'))
from collections import Counter, defaultdict

def eff(e):
    out = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            out.append((c, norm(e.get(f'jiuyan_concept{k}'))))
    if out:
        return out
    lc = (e.get('jiuyan_category') or '').strip()
    lp = norm(e.get('jiuyan_concept'))
    return [(t.strip(), lp) for t in lc.split('|') if t.strip()] if lc else []

cats = Counter()
pool = 0
yuliao, content = [], []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rs = eff(e)
    if any(p for _, p in rs):
        pool += 1
    for cat, con in rs:
        cats[cat] += 1
        toks = [t.strip() for t in con.split('|') if t.strip()]
        if 'AI语料' in toks:
            yuliao.append(code)
        if CONTENT in toks:
            content.append((code, e.get('name')))

print(f'\n残留 AI语料: {yuliao or "无"}')
print(f'AI应用 行数={cats["AI应用"]}  数据要素 行数={cats["数据要素"]}  全池={pool}')
print(f'{CONTENT} 成员({len(content)}): {content}')
for code in EDITS:
    print(code, obj2[code].get('name'), eff(obj2[code]))
