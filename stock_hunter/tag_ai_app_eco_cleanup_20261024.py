"""2026-10-24 AI应用二级概念梳理（第二轮）：解散"生态"类鸡肋概念，按实际业务归类。

owner 指令：去掉类似"华为生态"这种鸡肋分类，按公司实际业务分类。
核查依据：公司公告/互动易/研报（见文件末 SOURCES）+ t_io/cache/company_profile.json。

一、解散 4 个"关系链"概念（不描述业务，同一桶内公司彼此无关）
  华为生态 → 金财互联(AI财税)、酷特智能(C2M+AI Agent)、能科科技(工业软件/工业AI)
  字节生态 → 盛通股份(机器人编程/AI教育)、汉得信息(B端AI智能体/企业软件)
  甲骨文生态 → 新亚电子(高速铜缆，甲骨文只是其终端客户之一)、荣联科技(本就归数据要素)
  Minimax   → 万兴/掌阅本就归AI影视短剧；博睿数据归AI其他

二、按业务重新归类（新建 AI制造，2 家共同点明确）
  002530 金财互联 → AI应用/AI办公     （欣智悦财税大模型，盘古底座；与税友同桶）
  300840 酷特智能 → AI应用/AI制造     （服装C2M + 酷特AI Agents 2.0，盘古/昇腾/鸿蒙）
  603859 能科科技 → AI应用/AI制造     （乐/灵系列工业软件+工业AI Agent，25Q1-Q3 AI收入占30.8%）
  002599 盛通股份 → AI应用/AI教育     （乐博乐博机器人编程+盛通人工智能教育平台）
  300170 汉得信息 → AI应用/AI办公     （B端AI应用综合服务商，AI智能体/MCP/PaaS）
  688229 博睿数据 → AI应用/AI其他     （APM/可观测性 Bonree ONE + AIOps；单票，不单开桶）
  605277 新亚电子 → 超节点/连接器     （高速铜缆经安费诺进AI服务器链——本就不是AI应用）
  300624 万兴科技 → AI影视短剧       （去掉Minimax令牌）
  603533 掌阅科技 → AI影视短剧       （去掉Minimax令牌，保留数据要素_小说数据）
  688228 开普云   → AI应用/AI办公     （修正 step5 只改 legacy、被 category1=AI其他 盖住的漏改）

三、legacy 字段幽灵概念清理（这些股票有编号槽位，legacy 只是兜底/审计但会误导统计）
  蓝色光标/引力传媒/上海钢联/昆仑万维/荣联科技/奥飞娱乐/每日互动/华勤技术/初灵信息/华策影视
  ← 其 legacy 里的 Minimax / 甲骨文生态 / AI应用 / 其他 / 短剧 / 算力租赁 均为陈旧令牌，
    按"pairs 优先"约定重建 legacy。

幂等；改前备份 backup/watchlist_jiuyan_20261024_before_eco_cleanup.json。
SOURCES:
  金财互联: https://api3.cls.cn/share/article/1713073  https://www.geekpark.net/news/325214
  酷特智能: https://cnga.org.cn/cngahtml/xwzx/ppjj/20250609/1181.html  https://www.guanhai.com.cn/p/401721.html
  能科科技: http://vip.stock.finance.sina.com.cn/q/go.php/vReport_Show/kind/lastest/rptid/815702529022/index.phtml
  盛通股份: https://www.jiemodui.com/N/138932.html  https://stock.10jqka.com.cn/20250122/c665651726.shtml
  新亚电子: https://kjy4.china.com/shehui/13002708/20250926/48859385.html
  博睿数据: https://www.bonree.com/s/news/9353  https://wap.stockstar.com/detail/JC2025051200018222
"""
import json
import shutil
from pathlib import Path

import sys
sys.stdout.reconfigure(encoding='utf-8')

WL = Path(r'E:\superTrader\stock_hunter\watchlist_jiuyan.json')
BK = Path(r'E:\superTrader\backup\watchlist_jiuyan_20261024_before_eco_cleanup.json')
NOW = '2026-10-24 16:40:00'

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

# code: (expect_pairs, new_pairs, 说明)
EDITS = {
    '002530': ([('AI应用', '华为生态')], [('AI应用', 'AI办公')], '金财互联→AI办公(财税大模型)'),
    '300840': ([('AI应用', '华为生态')], [('AI应用', 'AI制造')], '酷特智能→AI制造(C2M/AI Agents)'),
    '603859': ([('AI应用', '华为生态'), ('数据要素', '物理AI_数据采集')],
               [('AI应用', 'AI制造'), ('数据要素', '物理AI_数据采集')], '能科科技→AI制造(工业AI)'),
    '002599': ([('AI应用', '字节生态')], [('AI应用', 'AI教育')], '盛通股份→AI教育'),
    '300170': ([('AI应用', '字节生态')], [('AI应用', 'AI办公')], '汉得信息→AI办公(B端AI智能体)'),
    '605277': ([('AI应用', '甲骨文生态')], [('超节点', '连接器')], '新亚电子→超节点/连接器(高速铜缆)'),
    '688229': ([('AI应用', 'Minimax')], [('AI应用', 'AI其他')], '博睿数据→AI其他(AIOps)'),
    '300624': ([('AI应用', 'AI影视短剧|Minimax'), ('云计算', 'SaaS软件与服务')],
               [('AI应用', 'AI影视短剧'), ('云计算', 'SaaS软件与服务')], '万兴科技去Minimax'),
    '603533': ([('AI应用', 'AI影视短剧|Minimax'), ('数据要素', '数据要素_小说数据')],
               [('AI应用', 'AI影视短剧'), ('数据要素', '数据要素_小说数据')], '掌阅科技去Minimax'),
    '688228': ([('AI应用', 'AI其他')], [('AI应用', 'AI办公')], '开普云修正为AI办公(step5被cat1盖住)'),
}

# legacy 幽灵令牌清理：有编号槽位的股票，按 pairs 重建 legacy（业务归属不变）
RESET_LEGACY = [
    ('002642', [('数据要素', '数据要素_数据存储')]),
    ('300058', [('AI应用', 'AI营销')]),
    ('603598', [('AI应用', 'AI营销')]),
    ('300226', [('数据要素', '数据要素_大宗数据')]),
    ('300418', [('AI应用', 'AI影视短剧')]),
    ('002292', [('AI应用', 'AI玩具')]),
    ('300766', [('数据要素', '数据要素_营销数据|数据要素_数据安全')]),
    ('603296', [('超节点', '交换机')]),
    ('300250', [('数据要素', '数据要素_数据分析')]),
    ('300133', [('AI应用', 'AI影视短剧'), ('冰雪经济', '培训')]),
]

log, skipped = [], []
for code, (expect, new, why) in EDITS.items():
    e = obj.get(code)
    if not e:
        skipped.append(f'{code} 不存在')
        continue
    cur = get_pairs(e)
    if cur == new:  # 幂等
        log.append(f'{code} {e.get("name")} 已是目标状态，跳过')
        continue
    if cur != expect:
        skipped.append(f'{code} {e.get("name")} 现状{cur} != 预期{expect}，跳过')
        continue
    put_pairs(e, new)
    log.append(f'{code} {e.get("name")} {why}：{expect} → {new}')

for code, expect in RESET_LEGACY:
    e = obj.get(code)
    if not e:
        skipped.append(f'{code} 不存在')
        continue
    cur = get_pairs(e)
    if cur != expect:
        skipped.append(f'{code} {e.get("name")} 现状{cur} != 预期{expect}，跳过 legacy 重建')
        continue
    before = (e.get('jiuyan_category'), e.get('jiuyan_concept'))
    put_pairs(e, expect)
    log.append(f'{code} {e.get("name")} legacy 重建 {before} → {(e.get("jiuyan_category"), e.get("jiuyan_concept"))}')

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

ai = defaultdict(set)
cats = Counter()
pool = 0
for code, e in obj2.items():
    if not isinstance(e, dict):
        continue
    rs = eff(e)
    if any(p.strip() for _, p in rs):
        pool += 1
    for cat, con in rs:
        cats[cat] += 1
        if cat == 'AI应用':
            for t in con.split('|'):
                if t.strip():
                    ai[t.strip()].add(code)

bad = [t for t in ai if t in ('华为生态', '字节生态', '甲骨文生态', 'Minimax', '微软生态')]
print(f'\nAI应用关系链概念残留: {bad or "无"}')
print(f'AI应用 行数={cats["AI应用"]}  超节点 行数={cats["超节点"]}  全池(有概念)={pool}')
for k in sorted(ai, key=lambda x: -len(ai[x])):
    print(f'  {len(ai[k]):3d} {k}')
for code in list(EDITS) + [c for c, _ in RESET_LEGACY]:
    print(code, obj2[code].get('name'), eff(obj2[code]))
