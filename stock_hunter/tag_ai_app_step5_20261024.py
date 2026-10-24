"""2026-10-24 AI应用/数据要素整治·步骤五：owner 14:38 五条指令
1. 物理AI从AI应用删除（凡拓数创挪入数据要素）
2. AI其他10只逐一核查：删7（郑中设计/天音控股/兆驰股份/雷柏科技/利安科技/易天股份/丽尚国潮）
   留3改标（米奥会展→AI营销；开普云/神思电子→AI办公）——依据子代理逐家业务核查
3. 韭研物理AI*14表 → 数据要素补漏：新增3只（龙旗科技/风语筑/创维数字），
   双挂7只（福莱新材/天奇股份/奥普特/科远智慧/华数传媒/能科科技/天娱数科），已有3只跳过
4. 韭研AI应用*7表补漏：新增榕基软件(AI政务→AI办公)、达实智能(AI其他)；
   税友股份(金融IT→双挂AI办公·AI财税)；理工能科太勉强不纳入
5. 删除思特奇、青云科技的AI应用身份（GUI"自研"/"AI算力云"组）
幂等；备份兜底 .bak_20261021 / backup/。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
NOW = '2026-10-24 14:40:00'
obj = json.loads(WL.read_text(encoding='utf-8'))
log = []

def get_pairs(e):
    out = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            out.append((c, str(e.get(f'jiuyan_concept{k}') or '').strip()))
    return out

def put_pairs(e, pairs):
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        if p:
            e[f'jiuyan_concept{k}'] = p
    # legacy 同步（pairs 优先，legacy 仅兜底/审计）
    e['jiuyan_category'] = '|'.join(c for c, _ in pairs)
    e['jiuyan_concept'] = '|'.join(p for _, p in pairs if p)

def ensure_pair(e, cat, concept):
    """pairs 模式追加配对；legacy 模式先把 legacy 转成配对再追加"""
    pairs = get_pairs(e)
    if not pairs:
        lc = (e.get('jiuyan_category') or '').strip()
        lp = e.get('jiuyan_concept', '')
        if isinstance(lp, (list, tuple)):
            lp = '|'.join(map(str, lp))
        pairs = [(t.strip(), str(lp or '')) for t in lc.split('|') if t.strip()]
    if any(c == cat for c, _ in pairs):
        return False
    pairs.append((cat, concept))
    put_pairs(e, pairs)
    return True

def new_entry(name, cat, concept, source):
    return {'name': name, 'business_summary': '', 'concept_boards': [], 'industry_boards': [],
            'sector': '', 'sector_type': '', 'jiuyan_category': cat, 'jiuyan_concept': concept,
            'jiuyan_category1': cat, 'jiuyan_concept1': concept,
            'primary_source': source, 'updated_at': NOW}

def clear_board(code):
    e = obj[code]
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    e['jiuyan_category'] = ''
    e['jiuyan_concept'] = ''
    e.pop('jiuyan_role', None)
    e.pop('jiuyan_role_reason', None)

PHY = '物理AI_数据采集'

# ── 指令1：凡拓数创 AI应用→数据要素（龙头标签随票走）──
e = obj['301313']
put_pairs(e, [('数据要素', PHY)])
log.append(f"301313 凡拓数创 → 数据要素/{PHY}（龙头保留）")

# ── 指令3：物理AI*14 补漏 ──
for code, name in [('603341', '龙旗科技'), ('603466', '风语筑'), ('000810', '创维数字')]:
    if code not in obj:
        obj[code] = new_entry(name, '数据要素', PHY, 'owner_20261024_物理AI表')
        log.append(f'{code} {name} 新增 → 数据要素/{PHY}')
for code in ['605488', '002009', '688686', '002380', '000156', '603859', '002354']:
    if ensure_pair(obj[code], '数据要素', PHY):
        log.append(f"{code} {obj[code].get('name')} 双挂 +数据要素/{PHY}")

# ── 指令4：AI应用*7 补漏 ──
if '002474' not in obj:
    obj['002474'] = new_entry('榕基软件', 'AI应用', 'AI办公', 'owner_20261024_AI应用表')
    log.append('002474 榕基软件 新增 → AI应用/AI办公（AI政务+华为昇腾）')
if '002421' not in obj:
    obj['002421'] = new_entry('达实智能', 'AI应用', 'AI其他', 'owner_20261024_AI应用表')
    log.append('002421 达实智能 新增 → AI应用/AI其他（物理AI+医用物流机器人+液冷）')
if ensure_pair(obj['603171'], 'AI应用', 'AI办公'):
    log.append(f"603171 税友股份 双挂 +AI应用/AI办公（AI财税）→ {get_pairs(obj['603171'])}")

# ── 指令5：删除思特奇/青云科技 AI应用身份 ──
for code in ['300608', '688316']:
    clear_board(code)
    log.append(f"{code} {obj[code].get('name')} 清除AI应用身份（移出面板，保留名称兜底）")

# ── 指令2：AI其他 10 只处置 ──
for code in ['002811', '000829', '002429', '002577', '300784', '300812', '600738']:
    clear_board(code)
    log.append(f"{code} {obj[code].get('name')} 剔除出AI应用（业务核查：搭边勉强/无实质AI）")
RETAG = {'300795': 'AI营销', '688228': 'AI办公', '300479': 'AI办公'}
for code, concept in RETAG.items():
    e = obj[code]
    e['jiuyan_concept'] = concept  # 均为 legacy 单板块模式
    log.append(f"{code} {e.get('name')} 改标 → {concept}")

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print('\n'.join(log))

# ── 重载验证 ──
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def rows_of(e):
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            p = e.get(f'jiuyan_concept{k}', '')
            if isinstance(p, (list, tuple)):
                p = '|'.join(map(str, p))
            pairs.append((c, str(p or '')))
    if pairs:
        return pairs
    lc = (e.get('jiuyan_category') or '').strip()
    lp = e.get('jiuyan_concept', '')
    if isinstance(lp, (list, tuple)):
        lp = '|'.join(map(str, lp))
    return [(t.strip(), str(lp or '')) for t in lc.split('|') if t.strip()] if lc else []

from collections import Counter
cnt = Counter()
phy_members, ai_concepts = [], Counter()
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    for c, p in rows_of(e):
        cnt[c] += 1
        if c == '数据要素' and '物理AI' in str(p):
            phy_members.append(f"{code}{e.get('name', '')}")
        if c == 'AI应用':
            ai_concepts[str(p).split('|')[0] if p else '(空)'] += 1
print(f"\n面板行数: AI应用={cnt.get('AI应用')} (上步102) / 数据要素={cnt.get('数据要素')} (上步80)")
print(f"数据要素·物理AI成员({len(phy_members)}): {sorted(phy_members)}")
print(f"AI应用二级分布: {dict(ai_concepts.most_common())}")
