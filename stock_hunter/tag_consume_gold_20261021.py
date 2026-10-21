# -*- coding: utf-8 -*-
"""大消费/黄金 一级概念 jiuyan_concept 打标 (2026-10-21)"""
import sys, json, shutil, unicodedata, os, random
sys.stdout.reconfigure(encoding='utf-8')

SRC = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json'
BAK_SAME_DIR = r'E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261021'
BAK_DIR = r'E:\superTrader\backup'
BAK_COPY = os.path.join(BAK_DIR, 'watchlist_jiuyan_20261021.json')

MAPPING = {
    '大消费_休闲食品': '良品铺子 三只松鼠 洽洽食品 盐津铺子 五芳斋 劲仔食品 好想你 甘源食品 有友食品 桂发祥 青岛食品 广州酒家 来伊份 金字火腿 朗源股份',
    '大消费_卤制品': '绝味食品 煌上煌 紫燕食品',
    '大消费_烘焙食品': '桃李面包 立高食品 元祖股份 一鸣食品 麦趣尔 南侨食品 海融科技 巴比食品',
    '大消费_预制菜': '味知香 惠发食品 安井食品 全聚德 金陵饭店 春雪食品 国联水产 海欣食品',
    '大消费_植物蛋白': '承德露露 养元饮品 海南椰岛 祖名股份 维维股份',
    '大消费_功能饮料': '东鹏饮料',
    '大消费_乳酸菌': '燕塘乳业 均瑶健康',
    '大消费_酒类': '酒鬼酒 皇台酒业 永顺泰 舍得酒业 水井坊',
    '大消费_果汁': '国投中鲁 安得利 国中水务',
    '大消费_常温奶': '伊利股份 皇氏集团 天润乳业 李子园 品渥食品 庄园牧场 燕塘乳业 麦趣尔',
    '大消费_鲜奶': '一鸣食品 光明乳业 三元股份 新乳业 阳光乳业',
    '大消费_奶酪': '妙可蓝多',
    '大消费_炼乳': '熊猫乳品',
    '大消费_餐饮老字号': '同庆楼 西安饮食 全聚德 广州酒家',
    '大消费_酒店': '华天酒店 君亭酒店 锦江酒店 首旅酒店 金陵饭店',
    '大消费_免税概念': '中国中免 王府井 格力地产 海汽集团 海南发展 上海机场 白云机场 海南机场 海峡股份 海南高速 南宁百货 中百集团 东百集团 海印股份 百联股份 友好集团 新华锦 居然之家 众信旅游',
    '大消费_百货零售': '永辉超市 中央商场 大商股份 徐家汇 益民集团 新世界 上海九百 百联股份 三江购物 杭州解百 宁波中百 百大集团 家家悦 银座股份 通程控股 友阿股份 中百集团 武商集团 汉商集团 东百集团 新华都 南京商旅 王府井 翠微股份 华联股份 广百股份 海印股份 中兴商业 大连友谊 红旗连锁 茂业商业 国光连锁 合肥百货 重庆百货 新华百货 南宁百货 汇嘉时代 国芳集团 欧亚集团 彩虹股份',
    '大消费_冰雪经济': '长白山 大连圣亚 三特索道 峨眉山A 亚泰集团 晶雪节能 冰山冷热 莱茵体育',
    '大消费_文旅旅游': '长白山 大连圣亚 曲江文旅 西安旅游 九华旅游 丽江股份 云南旅游 峨眉山A 黄山旅游 张家界 三峡旅游 西域旅游 天目湖 西藏旅游 桂林旅游 三特索道 岭南股份 宋城演艺 亚泰集团',
    '大消费_北交所': '美之高 海达尔 盖世食品 太湖雪 鸿智科技',
    '黄金_珠宝连锁': '老凤祥 周大生 豫园股份 中国黄金 潮宏基 明牌珠宝 莱绅通灵 迪阿股份 曼卡龙 菜百股份 金一文化 特力A 深中华A 航民股份',
    '黄金_金矿': '紫金矿业 山东黄金 白银有色 中金黄金 鹏欣资源 赤峰黄金 山金国际 招金黄金 恒邦股份 湖南黄金 西部黄金 四川黄金 晓程科技 盛达资源',
    '黄金_伴生金矿': '上海建工 和邦生物 中电电机 铜陵有色 北方铜业 湖南白银 株冶集团 云南铜业 中色股份',
    '黄金_回收': '贵研铂业 豫光金铅 飞南资源 中色股份 锌业股份',
}

def norm(s):
    s = unicodedata.normalize('NFKC', s)
    return s.replace(' ', '').replace('\u3000', '').strip()

# name -> [tags]
name2tags = {}
for tag, names in MAPPING.items():
    for n in names.split():
        name2tags.setdefault(norm(n), []).append(tag)

data = json.load(open(SRC, encoding='utf-8'))
# build normalized-name index
idx = {}
for code, rec in data.items():
    idx.setdefault(norm(rec.get('name', '')), code)

total = len(data)
tagged_before = sum(1 for r in data.values() if r.get('jiuyan_concept'))

stats = {tag: 0 for tag in MAPPING}
st_skip, unmatched = [], []
new_label_records = 0   # 原本无标签、本次获得标签
append_records = 0      # 原本有标签、本次追加
hit_codes = set()

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
    rec['jiuyan_concept'] = merged if len(merged) > 1 else merged[0]
    # 统一：新增后一律用列表更利于多标签；单元素保持跟随原类型
    if len(merged) > 1:
        rec['jiuyan_concept'] = merged
    for t in tags:
        stats[t] += 1
    hit_codes.add(code)

# 备份
shutil.copy2(SRC, BAK_SAME_DIR)
os.makedirs(BAK_DIR, exist_ok=True)
shutil.copy2(SRC, BAK_COPY)

json.dump(data, open(SRC, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ===== 验证：重新加载 =====
d2 = json.load(open(SRC, encoding='utf-8'))
tagged_after = sum(1 for r in d2.values() if r.get('jiuyan_concept'))
cov = {'大消费': 0, '黄金': 0}
for r in d2.values():
    jc = r.get('jiuyan_concept')
    lst = jc if isinstance(jc, list) else [jc] if jc else []
    for l1 in cov:
        if any(t.startswith(l1 + '_') for t in lst):
            cov[l1] += 1

print(f'总股票数: {total}')
print(f'原本有标签: {tagged_before} / 当前有标签: {tagged_after}')
print(f'本次新增标签记录(原无标签): {new_label_records}')
print(f'本次追加标签记录(原有标签): {append_records}')
print()
print('=== 各二级概念命中数 ===')
for tag, c in stats.items():
    print(f'{tag} -> {c}')
print()
print(f'=== 一级概念覆盖率（有该概念标签的股票数）===')
for l1, c in cov.items():
    print(f'{l1}: {c} 只')
print()
print('=== ST 跳过 ===')
for x in st_skip: print(x)
print('=== 未匹配（大消费）===')
for n, t in unmatched:
    if t[0].startswith('大消费'): print(n, t)
print('=== 未匹配（黄金）===')
for n, t in unmatched:
    if t[0].startswith('黄金'): print(n, t)
print()
print('=== 跨概念多标签示例 ===')
multi = [(c, r['name'], r['jiuyan_concept']) for c, r in d2.items()
         if isinstance(r.get('jiuyan_concept'), list) and any(t.startswith(('大消费_', '黄金_')) for t in r['jiuyan_concept'])]
for target in ['中色股份', '燕塘乳业', '王府井', '长白山', '金陵饭店', '广州酒家', '一鸣食品']:
    for c, n, jc in multi:
        if n == target:
            print(c, n, jc)
            break
print()
print('=== 随机抽 5 只命中股票完整 jiuyan_concept ===')
random.seed(20261021)
sample = random.sample(sorted(hit_codes), 5)
for c in sample:
    r = d2[c]
    print(c, r['name'], repr(r['jiuyan_concept']))
print()
print('备份1:', BAK_SAME_DIR, os.path.exists(BAK_SAME_DIR))
print('备份2:', BAK_COPY, os.path.exists(BAK_COPY))
