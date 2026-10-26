"""2026-10-24 AI大模型 做实（owner 选方案 A）

口径：**自研通用基座大模型**、对外输出模型/API/一体机的厂商；不含只做垂直应用模型
（天幕/天星/YonGPT/WPS AI）的软件公司——否则几乎所有 AI应用 公司都符合，桶就废了。

现状只有 2 只（科大讯飞、岩山科技），而池内真正拥有通用大模型的还有：
  300418 昆仑万维 —— 天工 4.0 + Skywork 全矩阵开源（奖励/多模态/数学代码/世界模型/视频/音乐）
  688327 云从科技 —— 从容大模型，OpenCompass 多模态 80.7 登顶，昇腾一体机/PortGPT
  601360 三六零   —— 360zhinao2-o1.5，SuperCLUE 2025-05 第 4
其中 360 的 "AI大模型" 令牌原本挂在 **网络安全** 分类下（属性标签用法）——本轮挪到
AI应用 侧，网络安全只留 AI安全。

变更：
  300418 昆仑万维  AI应用/AI影视短剧 → AI应用/AI影视短剧|AI大模型   （同类多概念，不新增行）
  688327 云从科技  AI应用/AI视觉     → AI应用/AI视觉|AI大模型       （同类多概念，不新增行）
  601360 三六零    网络安全/AI大模型|AI安全 → 网络安全/AI安全 + AI应用/AI大模型
  002230 科大讯飞 / 002195 岩山科技：配对不变，只补 concept_desc
  给 5 只成员补 jiuyan_concept_desc（GUI 分组头内联解释；只增不覆盖）

幂等；改前备份 backup/watchlist_jiuyan_20261024_before_llm_bucket.json。
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_llm_bucket.json')
NOW = '2026-10-24 19:10:00'

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

CAT, CON = 'AI应用', 'AI大模型'
DESC = ('自研**通用基座大模型**、并对外输出模型/API/一体机的公司——不含只做垂直应用模型的'
        '软件公司。看得懂的一句话：「自己拥有通用大模型」的公司。')

EDITS = {
    '300418': ([('AI应用', 'AI影视短剧')],
               [('AI应用', 'AI影视短剧|AI大模型')], '昆仑万维（天工4.0/Skywork 全矩阵）'),
    '688327': ([('机器人', '其他'), ('AI应用', 'AI视觉'), ('超节点', '昇腾应用软件伙伴')],
               [('机器人', '其他'), ('AI应用', 'AI视觉|AI大模型'), ('超节点', '昇腾应用软件伙伴')],
               '云从科技（从容大模型，OpenCompass 80.7 第一）'),
    '601360': ([('网络安全', 'AI大模型|AI安全')],
               [('网络安全', 'AI安全'), ('AI应用', 'AI大模型')],
               '三六零（360zhinao2-o1.5，SuperCLUE 第4；令牌从网络安全挪到AI应用）'),
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

# 5 只成员补注释（只增不覆盖）
for code in ['002230', '002195', '300418', '688327', '601360']:
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

print(f'\nAI应用 行数={cats["AI应用"]}  网络安全 行数={cats["网络安全"]}  全池={pool}')
print(f'{CAT}/{CON} 成员({len(members)}): {members}')
for code in list(EDITS) + ['002230', '002195']:
    print(code, obj2[code].get('name'), eff(obj2[code]))
