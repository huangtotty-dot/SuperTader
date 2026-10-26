"""2026-10-24 步骤五补丁：5 只"新增"股票其实已在 watchlist 名称兜底层，改为就地打标
603341 龙旗科技 / 603466 风语筑 / 000810 创维数字 → 数据要素/物理AI_数据采集
002474 榕基软件 → AI应用/AI办公；002421 达实智能 → AI应用/AI其他
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
NOW = '2026-10-24 14:45:00'
TAGS = {
    '603341': ('龙旗科技', '数据要素', '物理AI_数据采集'),
    '603466': ('风语筑', '数据要素', '物理AI_数据采集'),
    '000810': ('创维数字', '数据要素', '物理AI_数据采集'),
    '002474': ('榕基软件', 'AI应用', 'AI办公'),
    '002421': ('达实智能', 'AI应用', 'AI其他'),
}
obj = json.loads(WL.read_text(encoding='utf-8'))
for code, (name, cat, concept) in TAGS.items():
    e = obj.setdefault(code, {'name': name})
    if (e.get('jiuyan_category1') or e.get('jiuyan_category') or '').strip():
        print(f'{code} 已有归属，跳过: {e.get("jiuyan_category1") or e.get("jiuyan_category")}')
        continue
    e.setdefault('business_summary', '')
    e.setdefault('concept_boards', [])
    e.setdefault('industry_boards', [])
    e.setdefault('sector', '')
    e.setdefault('sector_type', '')
    e['name'] = e.get('name') or name
    e['jiuyan_category1'] = cat
    e['jiuyan_concept1'] = concept
    e['jiuyan_category'] = cat
    e['jiuyan_concept'] = concept
    e['primary_source'] = 'owner_20261024_韭研表补漏'
    e['updated_at'] = NOW
    print(f'{code} {name} 就地打标 → {cat}/{concept}')

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')

# 终验
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def rows_of(e):
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            pairs.append((c, str(e.get(f'jiuyan_concept{k}') or '')))
    if pairs:
        return pairs
    lc = (e.get('jiuyan_category') or '').strip()
    lp = e.get('jiuyan_concept', '')
    if isinstance(lp, (list, tuple)):
        lp = '|'.join(map(str, lp))
    return [(t.strip(), str(lp or '')) for t in lc.split('|') if t.strip()] if lc else []
from collections import Counter
cnt = Counter()
phy = []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    for c, p in rows_of(e):
        cnt[c] += 1
        if c == '数据要素' and '物理AI' in str(p):
            phy.append(f"{code}{e.get('name', '')}")
print(f"\n终验: AI应用={cnt.get('AI应用')} / 数据要素={cnt.get('数据要素')}")
print(f"物理AI成员({len(phy)}): {sorted(phy)}")
