"""2026-10-24 AI应用整治·步骤三：深度清洗
owner 指令（10-24 14:04）：
  ① 二级概念是裸"AI应用"的，拆到具体分支（AI营销/AI游戏/AI医疗…）
  ② 真身在其他板块、AI应用是多余挂牌的，删除AI应用身份（博汇科技范式）
  ③ 顺带统一：旧"短剧"标签并入"AI影视短剧"，"AI营销|短剧"等混合串收敛

动作类型：
  DEL  = 删除 AI应用 身份（配对模式删配对；legacy 模式删 token）。仅用于双板块且AI身份弱者。
  SET  = 设定/收敛二级概念（配对模式改 AI应用 对的 concept；legacy 模式改 jiuyan_concept）。
  ROLE = 追加龙头标签（南威软件：9月政务智能体三连板，调研证据在案）。
幂等；全程有备份兜底（.bak_20261021 / backup/）。
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')

# ── 删除 AI应用 身份（真身已在别的板块）──
DEL = ['688004',  # 博汇科技 → 网络安全（owner 点名范式）
       '300042',  # 朗科科技 → 半导体/存储
       '603068',  # 博通集成 → 半导体/无线芯片
       '603236',  # 移远通信 → 半导体+机器人（模组）
       '603296',  # 华勤技术 → 超节点（AI服务器/交换机ODM）
       '688286',  # 敏芯股份 → 机器人（MEMS）
       '002328',  # 新朋股份 → 液冷
       '002892',  # 科力尔 → 机器人（伺服）
       '688400',  # 凌云光 → 机器人（机器视觉）
       '300846',  # 首都在线 → 云计算/算力租赁
       '301248']  # 杰创智能 → 低空经济/算力租赁

# ── 设定二级概念 ──
SET = {
    # AI营销（广告/营销/导购/电商服务）
    '002027': 'AI营销', '300785': 'AI营销', '600228': 'AI营销', '301102': 'AI营销',
    '000917': 'AI营销', '002400': 'AI营销', '002799': 'AI营销', '002878': 'AI营销',
    '300063': 'AI营销', '300071': 'AI营销', '300612': 'AI营销', '605168': 'AI营销',
    '002103': 'AI营销', '002181': 'AI营销', '002291': 'AI营销', '002712': 'AI营销',
    '300242': 'AI营销', '300426': 'AI营销', '600556': 'AI营销',
    # AI影视短剧（内容/影视/IP/短剧，旧"短剧"标签统一于此）
    '300133': 'AI影视短剧', '300770': 'AI影视短剧', '300571': 'AI影视短剧',
    '601595': 'AI影视短剧', '601599': 'AI影视短剧', '600715': 'AI影视短剧',
    '600640': 'AI影视短剧', '300182': 'AI影视短剧|微软生态', '300291': 'AI影视短剧',
    # AI游戏
    '002558': 'AI游戏', '002624': 'AI游戏', '002174': 'AI游戏', '002555': 'AI游戏',
    '300002': 'AI游戏',
    # AI医疗
    '300288': 'AI医疗', '603108': 'AI医疗', '688393': 'AI医疗', '300078': 'AI医疗',
    '300253': 'AI医疗|Minimax',
    # AI教育
    '300654': 'AI教育', '300248': 'AI教育',
    # AI办公（SaaS/协同/政务智能体）
    '600588': 'AI办公', '688095': 'AI办公', '688369': 'AI办公', '603636': 'AI办公',
    # AI玩具
    '300459': 'AI玩具', '002862': 'AI玩具', '300822': 'AI玩具',
    # AI终端（端侧硬件：AI眼镜/耳机/PC/模组/交互屏）
    '001314': 'AI终端', '002313': 'AI终端', '002351': 'AI终端', '300622': 'AI终端',
    '002981': 'AI终端', '002952': 'AI终端', '002841': 'AI终端',
    # AI视觉
    '688088': 'AI视觉', '002835': 'AI视觉', '688327': 'AI视觉|微软生态',
    # AI大模型
    '002195': 'AI大模型', '002230': 'AI大模型|微软生态', '601360': 'AI大模型|AI安全',
    # AI语料（出版/IP语料）
    '601999': 'AI语料', '603999': 'AI语料', '301231': 'AI语料',
    # AI其他（身份存疑，集中放置便于日后修剪）
    '002577': 'AI其他', '002811': 'AI其他', '300784': 'AI其他', '300795': 'AI其他',
    '002429': 'AI其他', '600738': 'AI其他', '000829': 'AI其他', '300479': 'AI其他',
    '300812': 'AI其他', '688228': 'AI其他', '688316': 'AI算力云',
}
ROLE = {'603636': ('龙头', '9/21-23政务智能体三连板，个人Agent分支池内辨识度最高')}

obj = json.loads(WL.read_text(encoding='utf-8'))
stats = {'del': 0, 'set': 0, 'role': 0, 'skip': []}

def get_pairs(e):
    out = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            p = e.get(f'jiuyan_concept{k}', '')
            if isinstance(p, (list, tuple)):
                p = '|'.join(str(x).strip() for x in p if str(x).strip())
            out.append((c, str(p or '').strip()))
    return out

def put_pairs(e, pairs):
    for k in range(1, 10):
        e.pop(f'jiuyan_category{k}', None)
        e.pop(f'jiuyan_concept{k}', None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f'jiuyan_category{k}'] = c
        if p:
            e[f'jiuyan_concept{k}'] = p

for code in DEL:
    e = obj.get(code)
    if not isinstance(e, dict):
        stats['skip'].append(code + '(不存在)')
        continue
    pairs = get_pairs(e)
    if pairs:
        new = [(c, p) for c, p in pairs if c != 'AI应用']
        if len(new) == len(pairs):
            stats['skip'].append(code + '(无AI应用配对)')
            continue
        if not new:
            stats['skip'].append(code + '(唯一板块,拒删)')
            continue
        put_pairs(e, new)
    else:
        legacy = (e.get('jiuyan_category') or '')
        toks = [t.strip() for t in legacy.split('|') if t.strip() and t.strip() != 'AI应用']
        if not toks:
            stats['skip'].append(code + '(唯一板块,拒删)')
            continue
        e['jiuyan_category'] = '|'.join(toks)
    stats['del'] += 1

for code, concept in SET.items():
    e = obj.get(code)
    if not isinstance(e, dict):
        stats['skip'].append(code + '(不存在)')
        continue
    pairs = get_pairs(e)
    if pairs:
        new = [(c, concept if c == 'AI应用' else p) for c, p in pairs]
        put_pairs(e, new)
    else:
        if (e.get('jiuyan_category') or '').strip() != 'AI应用':
            stats['skip'].append(code + '(legacy分类异常:' + str(e.get('jiuyan_category')) + ')')
            continue
        e['jiuyan_concept'] = concept
    stats['set'] += 1

for code, (role, reason) in ROLE.items():
    e = obj.get(code)
    if isinstance(e, dict):
        e['jiuyan_role'] = role
        e['jiuyan_role_reason'] = reason
        stats['role'] += 1

WL.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
print(f"DEL={stats['del']} SET={stats['set']} ROLE={stats['role']} 跳过={stats['skip']}")

# ── 重载验证 ──
obj2 = json.loads(WL.read_text(encoding='utf-8'))
def rows_of(e):
    pairs = []
    for k in range(1, 10):
        c = (e.get(f'jiuyan_category{k}') or '').strip()
        if c:
            p = e.get(f'jiuyan_concept{k}', '')
            if isinstance(p, (list, tuple)):
                p = '|'.join(str(x).strip() for x in p if str(x).strip())
            pairs.append((c, str(p or '').strip()))
    if pairs:
        return pairs
    lc = (e.get('jiuyan_category') or '').strip()
    lp = e.get('jiuyan_concept', '')
    if isinstance(lp, (list, tuple)):
        lp = '|'.join(str(x).strip() for x in lp if str(x).strip())
    return [(t.strip(), str(lp or '')) for t in lc.split('|') if t.strip()] if lc else []

from collections import Counter
concepts = Counter()
ai_count = 0
orphan = []
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rows = rows_of(e)
    if not rows:
        orphan.append(code)
    for c, p in rows:
        if c == 'AI应用':
            ai_count += 1
            main = p.split('|')[0] if p else '(空)'
            concepts[main] += 1
print(f'\nAI应用面板行数: {ai_count} (原120)')
print('二级概念分布:')
for k, v in concepts.most_common():
    print(f'  {k}: {v}')
print(f'\n无任何板块的孤儿票: {orphan}')
# 抽查博汇科技
bh = obj2.get('688004', {})
print(f"\n博汇科技抽查: category={bh.get('jiuyan_category')!r} pairs={[(bh.get(f'jiuyan_category{k}'), bh.get(f'jiuyan_concept{k}')) for k in range(1,3)]}")
