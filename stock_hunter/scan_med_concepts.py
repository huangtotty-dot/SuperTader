# -*- coding: utf-8 -*-
import sys, json, re
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

PATH = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json'
data = json.load(open(PATH, encoding='utf-8'))

MED_KW = re.compile(r'药|医|疫苗|器械|CXO|CDMO|创新药|中药|血制品|诊断|医疗|制药|生物|基因|细胞|医美|眼科|牙科|耗材|影像|放疗|病理|检验')

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

concept_stocks = defaultdict(list)
for code, rec in data.items():
    if not isinstance(rec, dict):
        continue
    name = rec.get('name') or rec.get('stock_name') or rec.get('jiuyan_name') or ''
    desc = rec.get('jiuyan_concept_desc') or {}
    for c in get_concepts(rec):
        if MED_KW.search(c):
            concept_stocks[c].append((code, name, isinstance(desc, dict) and c in desc))

print(f'{"概念":<20}\t携带数\t覆盖数')
for c in sorted(concept_stocks, key=lambda x: -len(concept_stocks[x])):
    lst = concept_stocks[c]
    cov = sum(1 for _, _, ok in lst if ok)
    print(f'{c}\t{len(lst)}\t{cov}')

print('\n--- 未覆盖概念明细（携带股票列表）---')
for c in sorted(concept_stocks, key=lambda x: -len(concept_stocks[x])):
    lst = concept_stocks[c]
    cov = sum(1 for _, _, ok in lst if ok)
    if cov < len(lst):
        print(f'\n[{c}] {cov}/{len(lst)}')
        for code, name, ok in lst:
            print(f'  {code} {name} {"(已有)" if ok else ""}')

# 打印一条已有注释样例参考
print('\n--- 云南白药 中药品牌 注释样例 ---')
for code, rec in data.items():
    if isinstance(rec, dict) and '云南白药' in str(rec):
        d = rec.get('jiuyan_concept_desc') or {}
        if '中药品牌' in d:
            print(code, d['中药品牌'])
            break
