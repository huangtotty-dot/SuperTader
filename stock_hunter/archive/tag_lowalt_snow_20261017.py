# -*- coding: utf-8 -*-
"""韭研概念追加：**低空经济** + **冰雪经济**（2026-10-17，owner 两张截图）。

同族脚本：`tag_agri_20261010.py` / `tag_coalchem_20261010.py` / `tag_cybersec_20261010.py` /
`tag_silicone_20261010.py`。

## 口径（务必先读，动数据前两条都是硬约束）

1. **两个消费者的取法不一样**：
   - `prescreen.sym2board_map()`：**先**按序取 `jiuyan_category1..9` 的第一个非空，
     **全空**才回退 `jiuyan_category`（按 `|` 拆）⇒ **一股一个板块**，那是 **OSC 选股输入**；
   - `data_loader.WatchlistLoader`：按 `(jiuyan_category{i}, jiuyan_concept{i})` **成对**读，
     **全空**才回退 `jiuyan_category` + 单个 `jiuyan_concept`。
2. **铁律 = 只增不改**：
   - 全空 ⇒ 写主类目 `jiuyan_category`（纯新增）；
   - 只有旧字段 ⇒ **下沉**：原类目按 `|` 拆进 `category1..N`，**且必须同时镜像
     `concept1..N` = 原 `jiuyan_concept`**（漏了这步，猎手池按「韭研概念非空」过滤时
     这些票会**整个掉出池**——2026-10-16 那次就是这么伤到 8 只的）；新概念进 `category(N+1)`；
   - 已有槽位 ⇒ 新概念只能写**最后一个非空槽之后**的第一个空槽（填前面会顶掉 `cats[0]`）。
3. **ST/退市 不录**；**名称逐只校验**（对不上只报告不动手）；**幂等**（重跑只补空槽）。

## 名单来源

- **低空经济**：韭研「低空经济/亿航智能(250330)」图。原 `tag_lowalt_20261016.py`（04fdb60）
  就是照这张图打的，2026-10-17 被整体回退过 ⇒ 本脚本**沿用同一份 72 只名单与细分**（逐字照抄，
  不重抄一遍以免出错）。
- **冰雪经济**：韭研「冰雪经济(261013)更新」图，27 只，细分 = 图左「分类」列
  （场地设备 / 旅游消费 / 培训 / 个人装备）。四列股票已按名称唯一解析（无重名/无缺失）。

**西域旅游(300859) 同时属于两张图**：低空经济先打 ⇒ 它进主类目；冰雪经济随后进次级槽
（板块归属保持不变）。

## 截图里加不了的（不在 watchlist）

天宜上佳(688033) · 东方时尚(603377) · 中化岩土(002542) · 航天宏图(688066) · 岭南股份(002717)

## 用法

    python stock_hunter/tag_lowalt_snow_20261017.py            # 干跑，只打印
    python stock_hunter/tag_lowalt_snow_20261017.py --apply    # 真写（备份到 tmp/）

备份写 `tmp/`：`.gitignore` 只挡 `watchlist_jiuyan.backup_*.json`，`.bak_*` 落在
`stock_hunter/` 下会脏 `git status`。
"""
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")

# (概念, [(代码, 截图实名, 细分)])
CONCEPTS = [
    ("低空经济", [
        ("000099", "中信海直", "旅游|应急"),
        ("000420", "吉林化纤", "制造链|碳纤维"),
        ("000524", "岭南控股", "零部件|旅游"),
        ("000701", "厦门信达", "应急"),
        ("000738", "航发控制", "动力系统"),
        ("000801", "四川九洲", "系统服务"),
        ("000868", "安凯客车", "制造链"),
        ("001696", "宗申动力", "零部件|动力系统"),
        ("002006", "精工科技", "碳纤维"),
        ("002023", "海特高新", "eVTOL|动力系统"),
        ("002085", "万丰奥威", "eVTOL"),
        ("002097", "山河智能", "eVTOL"),
        ("002111", "威海广泰", "运维|应急"),
        ("002120", "韵达股份", "物流"),
        ("002253", "川大智胜", "系统服务"),
        ("002352", "顺丰控股", "物流"),
        ("002363", "隆基机械", "零部件"),
        ("002376", "新北洋", "物流"),
        ("002389", "航天彩虹", "无人机"),
        ("002419", "天虹股份", "物流"),
        ("002520", "日发精机", "零部件"),
        ("002590", "万安科技", "零部件"),
        ("002660", "茂硕电源", "无人机|农业"),
        ("002669", "康达新材", "碳纤维"),
        ("002682", "龙洲股份", "电力"),
        ("002708", "光洋股份", "零部件"),
        ("002735", "王子新材", "零部件"),
        ("002957", "科瑞技术", "物流"),
        ("002984", "森麒麟", "零部件"),
        ("002999", "天禾股份", "农业"),
        ("003004", "声迅股份", "空管|系统服务"),
        ("300107", "建新股份", "eVTOL"),
        ("300284", "苏交科", "智慧灯杆|培训"),
        ("300411", "金盾股份", "eVTOL"),
        ("300424", "航新科技", "运维"),
        ("300484", "蓝海华腾", "动力系统"),
        ("300542", "新晨科技", "运维|气象探测"),
        ("300681", "英搏尔", "制造链|动力系统"),
        ("300690", "双一科技", "碳纤维"),
        ("300719", "安达维尔", "碳纤维"),
        ("300771", "智莱科技", "物流"),
        ("300859", "西域旅游", "运营链|旅游"),
        ("300900", "广联航空", "无人机|碳纤维"),
        ("300975", "商络电子", "eVTOL"),
        ("300989", "蕾奥规划", "解决方案"),
        ("301002", "崧盛股份", "智慧灯杆"),
        ("301091", "深城交", "空管|解决方案|智慧灯杆"),
        ("301248", "杰创智能", "系统服务"),
        ("301398", "星源卓镁", "零部件"),
        ("600038", "中直股份", "eVTOL"),
        ("600562", "国睿科技", "空管"),
        ("600576", "祥源文旅", "运营链"),
        ("600580", "卧龙电驱", "动力系统"),
        ("600843", "上工申贝", "运动飞机"),
        ("600862", "中航高科", "碳纤维"),
        ("600990", "四创电子", "运维"),
        ("601238", "广汽集团", "制造链"),
        ("603018", "华设集团", "服务|解决方案"),
        ("603261", "立航科技", "无人机"),
        ("603308", "应流股份", "零部件|动力系统"),
        ("603679", "华体科技", "智慧灯杆"),
        ("603879", "永悦科技", "物流"),
        ("603950", "长源东谷", "零部件"),
        ("605001", "威奥股份", "碳纤维"),
        ("605056", "咸亨国际", "服务"),
        ("605178", "时空科技", "智慧灯杆"),
        ("688070", "纵横股份", "无人机|农业"),
        ("688297", "中无人机", "无人机"),
        ("688387", "信科移动-U", "智慧灯杆"),
        ("688522", "纳睿雷达", "空管"),
        ("688567", "孚能科技", "零部件"),
        ("688631", "莱斯信息", "空管|系统服务"),
    ]),
    ("冰雪经济", [
        # 场地设备
        ("000811", "冰轮环境", "场地设备"),
        ("002639", "雪人集团", "场地设备"),
        ("301010", "晶雪节能", "场地设备"),
        ("000530", "冰山冷热", "场地设备"),
        ("002899", "英派斯", "场地设备"),
        ("301287", "康力源", "场地设备"),
        ("002051", "中工国际", "场地设备"),
        ("002523", "天桥起重", "场地设备"),
        # 旅游消费
        ("600593", "大连圣亚", "旅游消费"),
        ("603099", "长白山", "旅游消费"),
        ("600853", "龙建股份", "旅游消费"),
        ("601188", "龙江交通", "旅游消费"),
        ("000715", "中兴商业", "旅游消费"),
        ("600202", "哈空调", "旅游消费"),
        ("300859", "西域旅游", "旅游消费"),
        ("000558", "天府文旅", "旅游消费"),
        ("600697", "欧亚集团", "旅游消费"),
        ("600655", "豫园股份", "旅游消费"),
        ("600158", "中体产业", "旅游消费"),
        ("000501", "武商集团", "旅游消费"),
        ("000002", "万科A", "旅游消费"),
        # 培训
        ("300133", "华策影视", "培训"),
        ("002659", "凯文教育", "培训"),
        ("002701", "奥瑞金", "培训"),
        # 个人装备
        ("002780", "三夫户外", "个人装备"),
        ("300005", "探路者", "个人装备"),
        ("600177", "雅戈尔", "个人装备"),
    ]),
]


def slots(v):
    return [(i, str(v.get("jiuyan_category%d" % i) or "").strip())
            for i in range(1, 10) if str(v.get("jiuyan_category%d" % i) or "").strip()]


def legacy_parts(v):
    return [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]


def cat_set(v):
    return {c for _, c in slots(v)} | set(legacy_parts(v))


def apply_concept(v, cat, concept, now):
    """把 (cat, concept) 写进这只票。返回 (kind, detail)；kind ∈ added/slotted/skip/full。"""
    if cat in cat_set(v):
        return "skip", "已含该概念"
    sl = slots(v)
    leg = legacy_parts(v)
    lcon = str(v.get("jiuyan_concept") or "").strip()

    if not sl and not leg:
        # ① 纯新增：主类目
        v["jiuyan_category"] = cat
        if not lcon:
            v["jiuyan_concept"] = concept
        return "added", cat

    if not sl and leg:
        # ② 下沉：原类目连同 concept 一起镜像，新概念接在后面
        n = len(leg)
        if n + 1 > 9:
            return "full", "旧类目 %d 段，槽位不够" % n
        for k, part in enumerate(leg, start=1):
            v["jiuyan_category%d" % k] = part
            if lcon and not str(v.get("jiuyan_concept%d" % k) or "").strip():
                v["jiuyan_concept%d" % k] = lcon
        v["jiuyan_category%d" % (n + 1)] = cat
        if concept and not str(v.get("jiuyan_concept%d" % (n + 1)) or "").strip():
            v["jiuyan_concept%d" % (n + 1)] = concept
        return "slotted", "%s → cat%d（原类目 %s，板块未变）" % (cat, n + 1, "|".join(leg))

    # ③ 已有槽位：只能写最后一个非空槽之后的空槽
    last = max(i for i, _ in sl)
    free = next((i for i in range(last + 1, 10)
                 if not str(v.get("jiuyan_category%d" % i) or "").strip()), None)
    if free is None:
        return "full", "次级槽用满"
    for i, _c in sl:                                   # 顺手补既有空 concept 槽
        if lcon and not str(v.get("jiuyan_concept%d" % i) or "").strip():
            v["jiuyan_concept%d" % i] = lcon
    v["jiuyan_category%d" % free] = cat
    if concept and not str(v.get("jiuyan_concept%d" % free) or "").strip():
        v["jiuyan_concept%d" % free] = concept
    return "slotted", "%s → cat%d（板块未变）" % (cat, free)


def main():
    apply = "--apply" in sys.argv
    d = json.load(open(PATH, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = {"added": [], "slotted": [], "skip": [], "wrong": []}

    for cat, stocks in CONCEPTS:
        print("=" * 78)
        print("%s —— 名单 %d 只" % (cat, len(stocks)))
        print("=" * 78)
        added, slotted, skip, wrong = [], [], [], []
        for code, name, concept in stocks:
            v = d.get(code)
            if not isinstance(v, dict):
                wrong.append((code, name, "不在 watchlist"))
                continue
            real = str(v.get("name") or "")
            if real != name:
                wrong.append((code, name, "表内实名为 %r" % real))
                continue
            if "ST" in real.upper() or "退" in real:
                wrong.append((code, name, "ST/退市，按 owner 口径剔除"))
                continue
            kind, detail = apply_concept(v, cat, concept, now)
            if kind in ("added", "slotted"):
                v["updated_at"] = now
            if kind == "added":
                added.append((code, real, detail))
            elif kind == "slotted":
                slotted.append((code, real, detail))
            elif kind == "skip":
                skip.append((code, real, detail))
            else:
                wrong.append((code, real, detail))
        print("  ① 写主类目（原本没有任何板块）: %d" % len(added))
        if added:
            print("     " + "、".join("%s %s" % (c, n) for c, n, _ in added))
        print("  ② 写次级槽、板块不变          : %d" % len(slotted))
        for c, n, why in slotted:
            print("     %s %-8s %s" % (c, n, why))
        print("  ③ 幂等跳过                    : %d" % len(skip))
        if wrong:
            print("  ⚠️ 未处理 %d 只（只报告不动手）:" % len(wrong))
            for c, n, why in wrong:
                print("     %s %-8s %s" % (c, n, why))
        total["added"] += added
        total["slotted"] += slotted
        total["skip"] += skip
        total["wrong"] += wrong
        print()

    pool = sum(1 for v in d.values()
               if isinstance(v, dict) and (slots(v) or legacy_parts(v)))
    print("小计：主类目 %d · 次级槽 %d · 跳过 %d · 未处理 %d ｜ 有类目票 %d"
          % (len(total["added"]), len(total["slotted"]), len(total["skip"]),
             len(total["wrong"]), pool))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    bdir = os.path.join(ROOT, "tmp")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, "watchlist_jiuyan.json.bak_%s_lowalt_snow"
                       % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(PATH, bak)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)
    print("\n已写入 %s（indent=1，与原文一致）" % PATH)
    print("备份 %s" % bak)


if __name__ == "__main__":
    main()
