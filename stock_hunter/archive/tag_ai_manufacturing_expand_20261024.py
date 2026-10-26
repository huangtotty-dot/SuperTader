"""2026-10-24 AI应用/AI制造 扩池（owner 决定"把桶做实"）

把 4 只典型工业 AI/工业软件标的纳入 AI应用/AI制造：
  300687 赛意信息  —— 制造/零售企业数字化 + 智能制造（MES/ERP 实施，华为盘古/昇腾生态）
  688083 中望软件  —— CAD/CAM/CAE 研发设计类工业软件（国产替代）
  300353 东土科技  —— 工业互联网核心硬件+软件（工业交换机/边缘控制/Intewell OS）
  688507 索辰科技  —— CAE 仿真软件 + 物理AI开发平台（原已挂 机器人/其他，本次为双挂）

池子影响（owner 已知情）：
  · 前 3 只原无韭研概念 ⇒ 本次进入猎手池（池 1903 → 1906）；
  · 索辰本来就在池内（机器人/其他），只增加一条 AI应用/AI制造；
  · sym2board_map 取 cats[0] ⇒ 前 3 只在 OSC 的"主板块"从空（全市场口径下取申万行业）
    变为"AI应用"，AI应用 板块的涨停家数/强度口径会多这 3 只——这是扩池的预期副作用。

同时给 AI制造 的 6 只成员补 jiuyan_concept_desc（GUI tooltip，只增不覆盖）。

幂等；改前备份 backup/watchlist_jiuyan_20261024_before_ai_manufacturing_expand.json。
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_ai_manufacturing_expand.json')
NOW = '2026-10-24 17:30:00'

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

CAT, CON = 'AI应用', 'AI制造'
DESC = ('把AI卖给工厂和制造业的公司——工业软件（CAD/CAE/PLM）、MES排产、工艺生成、'
        '质量检测、设备运维、数字孪生。看得懂的一句话：「给工厂做AI/工业软件」的公司。')

NEW = {
    '300687': ('赛意信息', 'MES/智能制造实施'),
    '688083': ('中望软件', 'CAD/CAE研发设计工业软件'),
    '300353': ('东土科技', '工业互联网核心硬件+软件'),
}
DUAL = {
    '688507': ('索辰科技', 'CAE仿真+物理AI平台（原挂 机器人/其他，双挂）'),
}

log, skipped = [], []
for code, (name, why) in NEW.items():
    e = obj.get(code)
    if not e:
        skipped.append(f'{code} 不存在')
        continue
    cur = get_pairs(e)
    if cur:
        skipped.append(f'{code} {e.get("name")} 已有概念 {cur}，不按新建处理（请人工确认）')
        continue
    put_pairs(e, [(CAT, CON)])
    e['primary_source'] = 'owner_20261024_AI制造扩池'
    log.append(f'{code} {e.get("name")} 新建 → {CAT}/{CON}（{why}）')

for code, (name, why) in DUAL.items():
    e = obj.get(code)
    if not e:
        skipped.append(f'{code} 不存在')
        continue
    cur = get_pairs(e)
    if any(c == CAT and CON in [t.strip() for t in p.split('|')] for c, p in cur):
        log.append(f'{code} {e.get("name")} 已挂 {CAT}/{CON}，跳过')
    elif any(c == CAT for c, _ in cur):
        skipped.append(f'{code} {e.get("name")} 已有 AI应用 配对但非AI制造：{cur}，请人工确认')
        continue
    else:
        put_pairs(e, cur + [(CAT, CON)])
        e['updated_at'] = NOW
        log.append(f'{code} {e.get("name")} 双挂 → {cur + [(CAT, CON)]}（{why}）')

# AI制造 成员补注释（只增不覆盖）
for code in ['300687', '688083', '300353', '688507', '603859', '300840']:
    e = obj.get(code)
    if not e:
        continue
    if not any(c == CAT and CON in [t.strip() for t in p.split('|')] for c, p in get_pairs(e)):
        continue
    d = e.get('jiuyan_concept_desc')
    if not isinstance(d, dict):
        d = {}
    if not d.get(CON):
        d[CON] = DESC
        e['jiuyan_concept_desc'] = d
        log.append(f'    + {code} concept_desc[{CON}]')

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print('\n'.join(log))
if skipped:
    print('\n!! 跳过:')
    print('\n'.join(skipped))

# ── 重载验证 ──
obj2 = json.loads(WL.read_text(encoding='utf-8'))
from collections import Counter

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
members = []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rs = eff(e)
    if any(p for _, p in rs):
        pool += 1
    for cat, con in rs:
        cats[cat] += 1
        if cat == CAT and CON in [t.strip() for t in con.split('|')]:
            members.append(f'{code}{e.get("name")}')

print(f'\nAI应用 行数={cats["AI应用"]}  全池(有概念)={pool}')
print(f'{CAT}/{CON} 成员({len(members)}): {members}')
for code in list(NEW) + list(DUAL):
    print(code, obj2[code].get('name'), eff(obj2[code]))
