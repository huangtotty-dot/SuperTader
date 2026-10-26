# -*- coding: utf-8 -*-
"""数据要素 一级概念 jiuyan_concept 打标 (2026-10-21)，备份刷新为最新版"""
import sys, json, shutil, unicodedata, os, random
sys.stdout.reconfigure(encoding='utf-8')

SRC = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json'
BAK_SAME_DIR = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261021'
BAK_COPY = r'E:\superTrader\backup\watchlist_jiuyan_20261021.json'

MAPPING = {
    '数据要素_数据存储': '深桑达A 易华录 紫光股份 歌华有线 中国电信 中国移动 中国联通 铜牛信息 云赛智联 美利云 常山北明 湘邮科技 广电运通 荣联科技 南天信息',
    '数据要素_数据清洗': '三维天地 东方国信 飞利信',
    '数据要素_数据标注': '海天瑞声 博彦科技',
    '数据要素_数据分析': '零点有数 汇纳科技 初灵信息 慧辰股份',
    '数据要素_数据确权': '人民网 新华网 中广天择 金桥信息 安妮股份 天亿马',
    '数据要素_网络可视化监管': '中新赛克 恒为科技 迪普科技 浩瀚深度',
    '数据要素_交易所': '浙数文化 安恒信息 华扬联众 零点有数 吉视传媒 中原传媒 浪潮软件 东华软件 东方国信 易华录 市北高新 万达信息 安妮股份',
    '数据要素_医疗数据': '久远银海 国新健康 山大地纬 万达信息',
    '数据要素_司法数据': '金桥信息 华宇软件 通达海',
    '数据要素_大宗数据': '上海钢联 卓创资讯 生意宝 瑞茂通',
    '数据要素_航运数据': '中远海科 盛世科技',
    '数据要素_小说数据': '中文在线 掌阅科技',
    '数据要素_学术数据': '中国科传 同方股份',
    '数据要素_交通数据': '中远海科 信息发展 通行宝 万集科技 金溢科技 千方科技 华铭智能 皖通科技 中公高科 天迈科技',
    '数据要素_汽车数据': '长久物流 中汽股份 捷顺科技',
    '数据要素_营销数据': '每日互动 世纪恒通',
    '数据要素_酒店数据': '石基信息',
    '数据要素_图片数据': '视觉中国',
    '数据要素_其他行业数据': '德生科技 银之杰 和达科技 湘邮科技 广联达 竞业达',
    '数据要素_数据安全': '奇安信 每日互动 竞业达 任子行',
    '数据要素_数据库一体机': '天玑科技',
    '数据要素_北交所': '众诚科技 广道数字',
}

def norm(s):
    s = unicodedata.normalize('NFKC', s)
    return s.replace(' ', '').replace('　', '').strip()

name2tags = {}
for tag, names in MAPPING.items():
    for n in names.split():
        name2tags.setdefault(norm(n), []).append(tag)

data = json.load(open(SRC, encoding='utf-8'))
idx = {}
for code, rec in data.items():
    idx.setdefault(norm(rec.get('name', '')), code)

total = len(data)
tagged_before = sum(1 for r in data.values() if r.get('jiuyan_concept'))

stats = {tag: 0 for tag in MAPPING}
st_skip, unmatched = [], []
new_label_records = 0
append_records = 0
hit_codes = set()
alias_notes = []

for nname, tags in sorted(name2tags.items()):
    code = idx.get(nname)
    if code is None:
        unmatched.append((nname, tags))
        continue
    rec = data[code]
    if 'ST' in norm(rec.get('name', '')):
        st_skip.append((rec['name'], code, tags))
        continue
    old = rec.get('jiuyan_concept') or ''
    old_list = old if isinstance(old, list) else [old] if old else []
    merged = list(old_list)
    for t in tags:
        if t not in merged:
            merged.append(t)
    added = [t for t in tags if t not in old_list]
    if not added:
        continue
    if old_list:
        append_records += 1
    else:
        new_label_records += 1
    if len(merged) > 1:
        rec['jiuyan_concept'] = merged
    else:
        rec['jiuyan_concept'] = merged[0] if not isinstance(old, list) else merged
    for t in tags:
        stats[t] += 1
    hit_codes.add(code)

# 掌阅/通行宝别名核查（仅供报告）
for a, b in [('掌阅传媒', '掌阅科技'), ('通信宝', '通行宝')]:
    alias_notes.append((a, idx.get(norm(a)), b, idx.get(norm(b))))

# 备份刷新为最新版
shutil.copy2(SRC, BAK_SAME_DIR)
os.makedirs(os.path.dirname(BAK_COPY), exist_ok=True)
shutil.copy2(SRC, BAK_COPY)

json.dump(data, open(SRC, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ===== 重载验证 =====
d2 = json.load(open(SRC, encoding='utf-8'))
tagged_after = sum(1 for r in d2.values() if r.get('jiuyan_concept'))
cov = sum(1 for r in d2.values()
          if any(t.startswith('数据要素_') for t in
                 (r['jiuyan_concept'] if isinstance(r.get('jiuyan_concept'), list)
                  else [r['jiuyan_concept']] if r.get('jiuyan_concept') else [])))

print(f'总股票数: {total}')
print(f'原本有标签: {tagged_before} / 当前有标签: {tagged_after}')
print(f'本次新增标签记录(原无标签): {new_label_records}')
print(f'本次追加标签记录(原有标签): {append_records}')
print()
print('=== 各二级概念命中数 ===')
for tag, c in stats.items():
    print(f'{tag} -> {c}')
print(f'\n一级概念覆盖率：数据要素 {cov} 只')
print()
print('=== 别名核查 ===')
for a, ca, b, cb in alias_notes:
    print(f'图中名[{a}]在池中: {ca} / 正确名[{b}]在池中: {cb}')
print()
print('=== ST 跳过 ===')
for x in st_skip: print(x)
print('=== 未匹配 ===')
for n, t in unmatched: print(n, t)
print()
print('=== 跨概念多标签示例 ===')
for target in ['湘邮科技', '竞业达', '每日互动', '万达信息', '金桥信息', '安妮股份', '零点有数', '东方国信', '易华录', '中远海科']:
    for c, r in d2.items():
        if r['name'] == target:
            print(c, r['name'], r['jiuyan_concept'])
            break
print()
print('=== 随机抽 5 只命中股票完整 jiuyan_concept ===')
random.seed(20261021)
for c in random.sample(sorted(hit_codes), 5):
    r = d2[c]
    print(c, r['name'], repr(r['jiuyan_concept']))
print()
print('备份1:', BAK_SAME_DIR, os.path.exists(BAK_SAME_DIR))
print('备份2:', BAK_COPY, os.path.exists(BAK_COPY))
