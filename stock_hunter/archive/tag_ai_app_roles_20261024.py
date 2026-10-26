"""2026-10-24 AI应用整治·步骤二：按 2026-08/09/10 月炒作主线重标龙头/中军 + 二级概念收敛
依据：三个月并行调研（连板网/财联社/东财等当月复盘证据）。
字段：jiuyan_role ∈ {龙头, 中军}；jiuyan_role_reason ≤60字；concept1 收敛为分支名。
幂等：重复执行结果相同。仅动下表列出的股票，其余不碰。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')

# code: (二级概念, role or '', reason)
TAGS = {
    # ── AI影视短剧（8/31《后西游记》起，9-10月AI漫剧主线）──
    '002343': ('AI影视短剧', '龙头', '10/8-15五连板AI漫剧高度龙；与阶跃星辰共建AI实验室'),
    '301052': ('AI影视短剧', '龙头', '10/8-15六日涨超52%，AI漫剧IP方弹性最大标的'),
    '000892': ('AI影视短剧', '龙头', '8/31短剧板块龙头、9月5日4板、10月5天3板'),
    '300413': ('AI影视短剧', '中军', '首部上星全AI长剧《后西游记》播出方，8/31-9/1连续20cm'),
    '300418': ('AI影视短剧', '中军', 'SkyReels视频模型+海外短剧平台，26H1 AI短剧收入15.35亿'),
    '001330': ('AI影视短剧', '', ''),
    '301262': ('AI影视短剧', '', ''),
    '300528': ('AI影视短剧', '', ''),
    '300624': ('AI影视短剧|Minimax', '', ''),
    # ── AI营销（贯穿三个月，业绩最易兑现分支）──
    '002354': ('AI营销', '龙头', '8/28大模型应用主线龙头、9/4再触涨停，AI营销转型标签'),
    '301171': ('AI营销', '龙头', '9/4触涨停涨18.4%，出海AI营销辨识度最高'),
    '603598': ('AI营销', '龙头', '9/29与10/9两度涨停，AI营销+短剧双标签弹性标的'),
    '300058': ('AI营销', '中军', 'AI营销总中军，26H1 AI收入28.45亿+81%，10月日均成交超200亿'),
    '000676': ('AI营销', '', ''),
    # ── AI办公/智能体（9/22 Muse 行情）──
    '688111': ('AI办公|微软生态', '中军', 'AI办公容量中军，9/22主力净买入2.43亿，机构参与度高'),
    # ── AI制药（9月分支）──
    '688222': ('AI制药', '龙头', '9月AI制药龙头，9/16-21四日累计涨41.6%'),
    '002821': ('AI制药', '中军', 'AI制药/CXO大票趋势中军，9/14板块启动日涨超4%'),
    '300725': ('AI制药', '', ''),
    # ── 物理AI/世界模型（10月下半月新主线）──
    '301313': ('物理AI', '龙头', '10月三度20cm涨停，物理AI/AI 3D数字孪生龙头'),
    # ── AI玩具/端侧（轮动分支，强度存疑，只给概念不给角色）──
    '002292': ('AI玩具', '', ''),
}

obj = json.loads(WL.read_text(encoding='utf-8'))
changed, missing = [], []
for code, (concept, role, reason) in TAGS.items():
    e = obj.get(code)
    if not isinstance(e, dict):
        missing.append(code)
        continue
    # 确保第 1 对为 ('AI应用', concept)；其余配对原样顺延
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        p = (e.get(f'jiuyan_concept{k}') or '').strip()
        if c or p:
            pairs.append((c, p))
    rest = [(c, p) for (c, p) in pairs if c != 'AI应用']
    new_pairs = [('AI应用', concept)] + rest
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(new_pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        e[f'jiuyan_concept{k}'] = p
    if role:
        e['jiuyan_role'] = role
        e['jiuyan_role_reason'] = reason
    else:
        e.pop('jiuyan_role', None)
        e.pop('jiuyan_role_reason', None)
    changed.append(code)

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print(f'打标 {len(changed)} 只, 异常 {missing}')

# 重载验证
import re
from collections import Counter
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def first_cat(e):
    for k in range(1, 10):
        v = (e.get(f'jiuyan_category{k}') or '').strip()
        if v:
            return {'创新药': '医药', '中药': '医药'}.get(v, v)
    v = (e.get('jiuyan_category') or '').strip()
    return re.split(r'\s*\|\s*', v)[0].strip() if v else ''
ai = [(c, e) for c, e in obj2.items() if isinstance(e, dict) and first_cat(e) == 'AI应用']
print(f'AI应用(GUI口径) = {len(ai)} 只')
roles = Counter(e.get('jiuyan_role', '无') for _, e in ai)
print(f'role 分布: {dict(roles)}')
print('\n龙头/中军明细:')
for c, e in sorted(ai):
    if e.get('jiuyan_role'):
        print(f"  {c} {e.get('name','')} [{e['jiuyan_role']}] {e.get('jiuyan_concept1','')} — {e.get('jiuyan_role_reason','')}")
