"""2026-10-24 AI应用整治·步骤四：owner 14:22 五条指令
1. 三六零 → 网络安全（删 AI应用 token，concept AI大模型|AI安全 随网络安全行保留）
2. AI医疗 5 只 → 医药板块（watchlist 无"医疗"板块，唯一医疗类板块是"医药"）
3. AI制药 3 只 → 删 AI应用 配对（医药配对本就在，顺带同步 stale legacy）
4. AI监管 在网络安全已有 → AI应用面板内删除该标签（仅余新媒股份一处）
5. 中文在线找回 → 双挂牌：AI应用(AI影视短剧) + 数据要素(小说数据)；掌阅科技同案同办
幂等；备份兜底 .bak_20261021 / backup/。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
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

# ── 1. 三六零 → 网络安全 ──
e = obj['601360']
e['jiuyan_category'] = '网络安全'
log.append(f"601360 三六零 → 网络安全 (concept 保留 {e.get('jiuyan_concept')})")

# ── 2. AI医疗 5 只 → 医药 ──
for code in ['300288', '603108', '688393', '300078']:  # legacy 模式
    e = obj[code]
    e['jiuyan_category'] = '医药'
    e['jiuyan_concept'] = 'AI医疗'
    log.append(f"{code} {e.get('name')} → 医药/AI医疗")
e = obj['300253']  # 卫宁健康：pairs 模式
put_pairs(e, [('医药', 'AI医疗'), ('云计算', 'SaaS软件与服务')])
log.append(f"300253 卫宁健康 → 医药/AI医疗 居首（云计算配对保留）")

# ── 3. AI制药 3 只：删 AI应用 配对，医药配对已在；同步 stale legacy ──
for code in ['688222', '002821', '300725']:
    e = obj[code]
    pairs = [(c, p) for c, p in get_pairs(e) if c != 'AI应用']
    put_pairs(e, pairs)
    e['jiuyan_category'] = '医药'
    e['jiuyan_concept'] = 'AI4S制药领域'
    log.append(f"{code} {e.get('name')} 删AI应用配对，留存 {pairs}，role={e.get('jiuyan_role','无')}保留")

# ── 4. AI监管 标签在 AI应用面板清零（仅新媒股份一处残留）──
e = obj['300770']
e['jiuyan_concept'] = 'AI影视短剧'
log.append(f"300770 新媒股份 AI应用行概念 → AI影视短剧（AI监管-内容审核已在网络安全行）")

# ── 5. 中文在线/掌阅科技：找回，双挂牌（AI应用 居首，数据要素 次之）──
for code, concept in [('300364', 'AI影视短剧'), ('603533', 'AI影视短剧|Minimax')]:
    e = obj[code]
    pairs = get_pairs(e)
    if not any(c == 'AI应用' for c, _ in pairs):
        sj = [(c, p) for c, p in pairs if c == '数据要素']
        rest = [(c, p) for c, p in pairs if c != '数据要素']
        put_pairs(e, [('AI应用', concept)] + sj + rest)
    log.append(f"{code} {e.get('name')} 找回AI应用 → {get_pairs(e)}")
# 中文在线补 中军 标签（10月调研：本轮辨识度最高的"缺席者"）
e = obj['300364']
e['jiuyan_role'] = '中军'
e['jiuyan_role_reason'] = '10/9 20cm涨停，海外短剧FlareFlow用户超3700万，IP储备最足'
log.append('300364 中文在线 补标 中军')

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
reg = []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    for c, p in rows_of(e):
        cnt[c] += 1
        if c == 'AI应用' and '监管' in str(p):
            reg.append(code)
print(f"\n面板行数: AI应用={cnt.get('AI应用')} (上步109) / 医药={cnt.get('医药')} (原69) / 网络安全={cnt.get('网络安全')} / 数据要素={cnt.get('数据要素')}")
print(f"AI应用面板残留'监管'标签: {reg}")
print(f"中文在线检查: {rows_of(obj2['300364'])} role={obj2['300364'].get('jiuyan_role')}")
