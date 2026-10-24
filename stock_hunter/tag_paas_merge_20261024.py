"""2026-10-24 "数据中台服务商"归并到 云计算/PaaS平台即服务（owner 选方案 A）

核查结论：
  688031 星环科技-U —— 企业级AI/大数据基础设施软件；TDH/TDC(数据云PaaS)/ArgoDB/
                        Sophon/Hippo；2025 营收 4.48亿(+20.5%)，软件+服务占 80.98%
  688118 普元信息   —— 软件基础平台占 92.71%；智能数据中台/低代码/中间件三条线，
                        数据中台只是其中一条（约 1/6），"数据中台服务商"名不副实
  ⇒ 两者都是平台软件/底座厂商，不是 AI 应用；并入既有 云计算/PaaS平台即服务
    （原成员：梦网科技、优刻得-W）。

变更：
  688031 AI应用/数据中台服务商 → 云计算/PaaS平台即服务
  688118 AI应用/数据中台服务商 → 云计算/PaaS平台即服务
  "数据中台服务商" 归零（该二级概念消失）
  给 PaaS 的 4 只成员补 jiuyan_concept_desc（GUI 分组头内联解释；只增不覆盖）

副作用（owner 已知情）：两只 cats[0] 由 AI应用 → 云计算，OSC 主板块口径随之变化。
幂等；改前备份 backup/watchlist_jiuyan_20261024_before_paas_merge.json。
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_paas_merge.json')
NOW = '2026-10-24 18:40:00'

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

CAT, CON = '云计算', 'PaaS平台即服务'
DESC = ('以平台即服务方式提供软件/开发/数据底座的厂商——中间件、低代码平台、'
        '数据云与大数据基础软件。看得懂的一句话：「卖平台软件/开发底座」的公司。')

EDITS = {
    '688031': ([('AI应用', '数据中台服务商')], '星环科技（数据云/大数据基础软件）'),
    '688118': ([('AI应用', '数据中台服务商')], '普元信息（低代码/中间件/数据中台）'),
}

log, skipped = [], []
for code, (expect, why) in EDITS.items():
    e = obj.get(code)
    if not e:
        skipped.append(f'{code} 不存在')
        continue
    cur = get_pairs(e)
    new = [(CAT, CON)]
    if cur == new:
        log.append(f'{code} {e.get("name")} 已是目标状态，跳过')
    elif cur != expect:
        skipped.append(f'{code} {e.get("name")} 现状{cur} != 预期{expect}，跳过')
        continue
    else:
        put_pairs(e, new)
        log.append(f'{code} {e.get("name")} {why}：{expect} → {new}')

# PaaS 全成员补注释（只增不覆盖）
for code in ['688031', '688118', '002123', '688158']:
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
        log.append(f'    + {code} {e.get("name")} concept_desc[{CON}]')

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
paas, zhongtai = [], []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rs = eff(e)
    if any(p for _, p in rs):
        pool += 1
    for cat, con in rs:
        cats[cat] += 1
        toks = [t.strip() for t in con.split('|') if t.strip()]
        if '数据中台服务商' in toks:
            zhongtai.append(code)
        if cat == CAT and CON in toks:
            paas.append(f'{code}{e.get("name")}')

print(f'\n残留 数据中台服务商: {zhongtai or "无"}')
print(f'AI应用 行数={cats["AI应用"]}  云计算 行数={cats["云计算"]}  全池={pool}')
print(f'{CAT}/{CON} 成员({len(paas)}): {paas}')
for code in EDITS:
    print(code, obj2[code].get('name'), eff(obj2[code]))
