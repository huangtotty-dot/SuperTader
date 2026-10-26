# -*- coding: utf-8 -*-
"""为未覆盖的医药系二级概念撰写 jiuyan_concept_desc 注释。
只改 jiuyan_concept_desc 字段；已有注释不覆盖；保存前先刷新两处备份。
"""
import sys, json, shutil, os

sys.stdout.reconfigure(encoding='utf-8')

PATH = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json'
BAK1 = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261021'
BAK2 = r'E:\superTrader\backup\watchlist_jiuyan_20261021.json'

# ---------- 1. 备份 ----------
shutil.copy2(PATH, BAK1)
shutil.copy2(PATH, BAK2)
print(f'备份完成:\n  {BAK1} ({os.path.getsize(BAK1)} bytes)\n  {BAK2} ({os.path.getsize(BAK2)} bytes)')

# ---------- 2. 新注释 ----------
NEW_NOTES = {
    '肿瘤疫苗': (
        '做防癌/治癌疫苗的公司——包括「预防性疫苗」（HPV疫苗防宫颈癌、带状疱疹疫苗等）'
        '和仍在临床阶段的「治疗性肿瘤疫苗」（mRNA个性化肿瘤疫苗，Moderna/斯微生物/艾博生物方向）。'
        '看得懂的一句话：用疫苗「防癌症或治癌症」。'
        '生意特征是研发周期长、审批极严：预防性疫苗看批签发量与接种率放量，'
        '治疗性疫苗看临床数据读出，弹性大风险也大。'
    ),
    '数据要素_医疗数据': (
        '手握医保、医院、公共健康数据并做医疗信息化的公司——医保支付系统、'
        '区域健康大数据平台、医保基金监管都算。'
        '看得懂的一句话：「管医保和医院数据」的公司，如医保DRG/DIP付费系统、区域全民健康信息平台。'
        '生意特征是政府/医保项目制订单，收入看财政信息化预算；'
        '弹性在数据要素资产化（数据入表、授权运营变现）真正落地。'
    ),
    'AI4S制药领域|CDMO': (
        'AI技术加持的「药物生产外包」公司：替创新药企做工艺开发和规模化生产，'
        '同时用AI辅助合成路线设计、工艺优化。'
        '看得懂的一句话：「用AI提效的制药代工厂」，如凯莱英以AI+连续反应技术做小分子工艺开发。'
        '生意特征是按订单收费、重资产，核心看在手订单与大客户商业化放量；'
        'AI是提效降本的加分项，不是独立收入。（注：此标签为旧版写法，新版词典已收编入「CDMO」）'
    ),
    'AI4S制药领域|分子砌块|CDMO': (
        '分子砌块=药物合成的标准化「乐高积木」小分子模块，这类公司卖砌块库，'
        '并向中下游延伸到中间体/原料药的CDMO生产外包。'
        '看得懂的一句话：「卖造药积木+代工」，如药石科技的海量长尾砌块库。'
        '生意特征是砌块SKU极多、毛利高，客户管线推进会带动「砌块→中间体→原料药」逐级放量，'
        '看产品库规模与CDMO转化率。（注：此标签为旧版写法，分子砌块在新词典归「生命科学上游」）'
    ),
    'AI4S制药领域|药物发现|自研': (
        '用平台技术+AI做新药「最早期筛选」、同时拥有自研管线的公司——'
        '典型手段是DEL（DNA编码化合物库）筛选：一次性从上亿个分子里捞出候选药。'
        '看得懂的一句话：「帮药企大海捞针找新药苗子」，如成都先导的DEL平台。'
        '生意特征是平台服务费+里程碑付款，弹性看自研管线临床进展与对外授权（license-out）。'
        '（注：此标签为旧版写法，纯药物发现服务在新词典归「临床前CRO」）'
    ),
}

# 非医药系、明确排除（命中“药”字但不属于医药概念）：农药、导弹-弹药

data = json.load(open(PATH, encoding='utf-8'))

def get_concepts(rec):
    tags = []
    v = rec.get('jiuyan_concept')
    if v:
        tags.extend(v if isinstance(v, list) else [v])
    for i in range(1, 5):
        v = rec.get(f'jiuyan_concept{i}')
        if v:
            tags.extend(v if isinstance(v, list) else [v])
    return [t for t in tags if isinstance(t, str)]

written = {c: [] for c in NEW_NOTES}
skipped = {c: [] for c in NEW_NOTES}
for code, rec in data.items():
    if not isinstance(rec, dict):
        continue
    tags = set(get_concepts(rec))
    if not tags:
        continue
    for c, note in NEW_NOTES.items():
        if c not in tags:
            continue
        desc = rec.get('jiuyan_concept_desc')
        if not isinstance(desc, dict):
            desc = {}
            rec['jiuyan_concept_desc'] = desc
        if c in desc:
            skipped[c].append(code)  # 已有注释，不覆盖
        else:
            desc[c] = note
            written[c].append(code)

print('\n--- 写入明细 ---')
for c in NEW_NOTES:
    print(f'[{c}] 新写入 {len(written[c])} 只: {", ".join(written[c])}'
          + (f'；已有跳过 {len(skipped[c])} 只: {", ".join(skipped[c])}' if skipped[c] else ''))

# ---------- 3. 保存 ----------
with open(PATH, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print(f'\n已保存: {PATH} ({os.path.getsize(PATH)} bytes)')

# ---------- 4. 重载验证 ----------
import re
MED_KW = re.compile(r'药|医|疫苗|器械|CXO|CDMO|创新药|中药|血制品|诊断|医疗|制药|生物|基因|细胞|医美|眼科|牙科|耗材|影像|放疗|病理|检验')
EXCLUDE = {'农药', '导弹-弹药'}  # 非医药系

data2 = json.load(open(PATH, encoding='utf-8'))
from collections import defaultdict
cs = defaultdict(list)
for code, rec in data2.items():
    if not isinstance(rec, dict):
        continue
    desc = rec.get('jiuyan_concept_desc') or {}
    for c in set(get_concepts(rec)):
        if MED_KW.search(c):
            cs[c].append(c in desc if isinstance(desc, dict) else False)

print('\n--- 重载验证（医药系概念 → 携带数 → desc覆盖数）---')
all_ok = True
for c in sorted(cs, key=lambda x: -len(cs[x])):
    lst = cs[c]
    cov = sum(lst)
    mark = ''
    if c in EXCLUDE:
        mark = '（非医药系，范围外）'
    elif cov != len(lst):
        mark = ' ❌'
        all_ok = False
    else:
        mark = ' ✅'
    print(f'{c} → {len(lst)} → {cov}{mark}')
print('\n医药系概念全部 100% 覆盖' if all_ok else '\n仍有未覆盖的医药系概念！')
