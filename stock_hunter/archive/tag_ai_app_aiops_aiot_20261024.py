"""2026-10-24 AI应用·解散"AI其他"，按业务改成 AI运维 / AIoT

owner：「AI其他这个二级概念更让人无所适从」。当前 AI其他 只剩 2 只，逐家核查后：

  002421 达实智能 → AI应用/AIoT
    · 自研 AIoT 智能物联网管控平台（"1+4+N"），平台内嵌 AI Agent，接入豆包/千问等国产大模型
    · 业务：智慧空间物联网应用（EMC 空调节能、手术室、地铁环控、数据中心能效/液冷优化）
      + 整体解决方案；2025 1-9 月与 AI 应用相关的 AIoT 平台签约额 3780.6 万元（+102.9%）
    · 不是"其他"——正是一个独立的业务方向：AIoT（智能物联/智慧空间）

  688229 博睿数据 → AI应用/AI运维
    · APM/可观测性（Bonree ONE，2025H1 市占率 22.06% 国内第一）
    · AI：智能根因分析 Agent（ReAct+MCP）、"小睿助理"、LLM 可观测、Sage AI 工作台
    · 2025 营收 1.29 亿（-8.13%），AI 能力嵌入平台售卖、未单列收入——但业务就是 AIOps

AI其他 归零后删除该二级概念（不再保留 dumping ground）。

幂等；改前备份 backup/watchlist_jiuyan_20261024_before_aiops_aiot.json。
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_aiops_aiot.json')
NOW = '2026-10-24 18:10:00'

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

DESC = {
    'AIoT': ('把AI装进楼宇/园区/医院/数据中心等空间的物联网平台公司——设备接入、能耗优化、'
             '安防通行、会议室等场景智能体。看得懂的一句话：「给建筑与园区做AIoT」的公司。'),
    'AI运维': ('用AI做IT运维与可观测性的公司——性能监控、故障根因分析、日志/指标/链路可观测、'
               '运维智能体自动处置。看得懂的一句话：「给IT系统做AI运维监控」的公司。'),
}

EDITS = {
    '002421': ([('AI应用', 'AI其他')], [('AI应用', 'AIoT')], '达实智能（AIoT智慧空间平台）'),
    '688229': ([('AI应用', 'AI其他')], [('AI应用', 'AI运维')], '博睿数据（APM/可观测+AIOps）'),
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
    con = new[-1][1]
    d = e.get('jiuyan_concept_desc')
    if not isinstance(d, dict):
        d = {}
    if not d.get(con):
        d[con] = DESC[con]
        e['jiuyan_concept_desc'] = d
        log.append(f'    + concept_desc[{con}]')

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

ai = defaultdict(list)
pool = 0
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rs = eff(e)
    if any(p for _, p in rs):
        pool += 1
    for cat, con in rs:
        if cat == 'AI应用':
            for t in con.split('|'):
                if t.strip():
                    ai[t.strip()].append(e.get('name'))

print(f'\n残留 AI其他: {ai.get("AI其他") or "无"}')
print(f'全池={pool}')
for k in sorted(ai, key=lambda x: -len(ai[x])):
    print(f'  {len(ai[k]):3d} {k}  {ai[k]}')
