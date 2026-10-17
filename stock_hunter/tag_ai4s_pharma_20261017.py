# -*- coding: utf-8 -*-
"""韭研「AI4S 制药领域(260809)」→ 补进 **医药** 板块（owner 2026-10-17）。

同族脚本：`tag_agri_*` / `tag_coalchem_*` / `tag_cybersec_*` / `tag_lowalt_snow_20261017`。

## 口径（与前几个脚本完全一致，两条硬约束）

1. **只增不改** —— 两个字段都空才写主类目；只有旧字段则**下沉**（原类目按 `|` 拆进
   `category1..N`，**且必须同时镜像 `concept1..N`**）；已有槽位则写最后一个非空槽之后的空槽。
   覆盖主类目 = 换板块 = 改 OSC 选股输入。
2. **归并后判重** —— 两个消费者都已把 `创新药/中药` 归到 `医药`
   （`prescreen.MERGES` 与 `data_loader` 的 merge 同义）⇒ 这类票**本来就在医药板块里**，
   不能再补一个 `医药` 槽（否则同一板块占两格）。

细分（`jiuyan_concept`）= 图的**「分类」列**（蛋白合成/基因测序仪/实验鼠/英矽智能…），
一只票命中多行就按 `|` 连接。

## 名单

49 只唯一票，按名称唯一解析；`迈威生物` 表内实名是 **`迈威生物-U`**(688062)，已按实名纳入。
全部落在 watchlist 内（无需报告"不在池"的票）。

## 用法

    python stock_hunter/tag_ai4s_pharma_20261017.py            # 干跑
    python stock_hunter/tag_ai4s_pharma_20261017.py --apply    # 真写（备份到 tmp/）
"""
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")

CAT = "医药"
# 归并后即「医药」的类目（与 prescreen.MERGES / data_loader merge 同义）
MERGES = {"创新药": "医药", "中药": "医药"}

# (代码, 截图实名, 分类标签)；同一票多行 → 合并成 | 连接
ROWS = [
    ("301080", "百普赛斯", "蛋白合成"),
    ("301047", "义翘神州", "蛋白合成"),
    ("688137", "近岸蛋白", "蛋白合成"),
    ("688690", "纳微科技", "蛋白纯化"),
    ("688758", "赛分科技", "蛋白纯化"),
    ("688293", "奥浦迈", "原料试剂耗材|CDMO"),
    ("688105", "诺唯赞", "原料试剂耗材"),
    ("688026", "洁特生物", "原料试剂耗材"),
    ("688114", "华大智造", "基因测序仪"),
    ("000710", "贝瑞基因", "基因测序仪"),
    ("002030", "达安基因", "基因测序仪"),
    ("603658", "安图生物", "基因测序仪"),
    ("688289", "圣湘生物", "基因测序仪"),
    ("688046", "药康生物", "实验鼠"),
    ("688265", "南模生物", "实验鼠"),
    ("688796", "百奥赛图", "实验鼠|晶泰控股"),
    ("603127", "昭衍新药", "实验猴|药物安评"),
    ("603259", "药明康德", "实验猴|药物发现|药物安评|CDMO"),
    ("300692", "中赋科技", "实验猴"),
    ("300149", "睿智医药", "实验猴|药物发现|CDMO"),
    ("300759", "康龙化成", "实验猴|药物发现|药物安评|临床CRO|CDMO"),
    ("688710", "益诺思", "实验猴|药物安评"),
    ("688202", "美迪西", "实验猴|药物发现|药物安评|英矽智能"),
    ("688073", "毕得医药", "分子砌块"),
    ("688131", "皓元医药", "分子砌块|英矽智能"),
    ("300725", "药石科技", "分子砌块|CDMO"),
    ("688222", "成都先导", "药物发现|自研"),
    ("301096", "百诚医药", "药物发现|自研"),
    ("600222", "太龙药业", "药物发现"),
    ("600721", "百花医药", "药物发现|临床CRO"),
    ("301230", "泓博医药", "药物发现|自研"),
    ("600276", "恒瑞医药", "自研"),
    ("688621", "阳光诺和", "药物发现|华为|临床CRO"),
    ("600535", "天士力", "华为"),
    ("600267", "海正药业", "英矽智能"),
    ("688062", "迈威生物-U", "英矽智能"),
    ("600196", "复星医药", "英矽智能"),
    ("301093", "华兰股份", "晶泰控股"),
    ("300347", "泰格医药", "临床CRO"),
    ("300404", "博济医药", "临床CRO"),
    ("301520", "万邦医药", "临床CRO"),
    ("688238", "和元生物", "临床CRO"),
    ("301333", "诺思格", "临床CRO"),
    ("603456", "九洲药业", "CDMO"),
    ("300363", "博腾股份", "CDMO"),
    ("301509", "金凯生科", "CDMO"),
    ("301201", "诚达药业", "CDMO"),
    ("002821", "凯莱英", "CDMO"),
    ("002399", "海普瑞", "CDMO"),
]
# 截图名的别名（表内实名不同）
NAME_ALIAS = {"迈威生物": "迈威生物-U"}
STOCKS = [(c, n, t) for c, n, t in ROWS if c and n]


def slots(v):
    return [(i, str(v.get("jiuyan_category%d" % i) or "").strip())
            for i in range(1, 10) if str(v.get("jiuyan_category%d" % i) or "").strip()]


def legacy_parts(v):
    return [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]


def cat_set(v):
    return {c for _, c in slots(v)} | set(legacy_parts(v))


def in_board(v, board="医药"):
    """归并后是否已经在这个板块里（创新药/中药 都算医药）。"""
    return any(MERGES.get(c, c) == board for c in cat_set(v))


def apply_cat(v, cat, concept):
    """只增不改地写入 (cat, concept)。返回 (kind, detail)。"""
    if in_board(v, cat):
        return "skip", "已在医药板块"
    sl = slots(v)
    leg = legacy_parts(v)
    lcon = str(v.get("jiuyan_concept") or "").strip()

    if not sl and not leg:
        v["jiuyan_category"] = cat
        if not lcon:
            v["jiuyan_concept"] = concept
        return "added", cat

    if not sl and leg:
        n = len(leg)
        if n + 1 > 9:
            return "full", "旧类目 %d 段，槽位不够" % n
        for k, part in enumerate(leg, start=1):
            v["jiuyan_category%d" % k] = part
            if lcon and not str(v.get("jiuyan_concept%d" % k) or "").strip():
                v["jiuyan_concept%d" % k] = lcon          # ← 下沉必须镜像，否则票会掉出猎手池
        v["jiuyan_category%d" % (n + 1)] = cat
        if concept and not str(v.get("jiuyan_concept%d" % (n + 1)) or "").strip():
            v["jiuyan_concept%d" % (n + 1)] = concept
        return "slotted", "%s → cat%d（原类目 %s，板块未变）" % (cat, n + 1, "|".join(leg))

    last = max(i for i, _ in sl)
    free = next((i for i in range(last + 1, 10)
                 if not str(v.get("jiuyan_category%d" % i) or "").strip()), None)
    if free is None:
        return "full", "次级槽用满"
    for i, _c in sl:
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
    added, slotted, skip, wrong = [], [], [], []
    for code, alias, concept in STOCKS:
        v = d.get(code)
        if not isinstance(v, dict):
            wrong.append((code, alias, "不在 watchlist"))
            continue
        real = str(v.get("name") or "")
        want = NAME_ALIAS.get(alias, alias)
        if real != want:
            wrong.append((code, alias, "表内实名为 %r" % real))
            continue
        if "ST" in real.upper() or "退" in real:
            wrong.append((code, alias, "ST/退市，按 owner 口径剔除"))
            continue
        kind, detail = apply_cat(v, CAT, concept)
        if kind in ("added", "slotted"):
            v["updated_at"] = now
        {"added": added, "slotted": slotted, "skip": skip}.get(kind, wrong).append(
            (code, real, detail) if kind != "wrong" else (code, real, detail))

    print("=" * 78)
    print("AI4S 制药领域 → 医药板块：名单 %d 只" % len(STOCKS))
    print("=" * 78)
    print("① 写主类目（原本没有任何板块）: %d" % len(added))
    if added:
        print("     " + "、".join("%s %s" % (c, n) for c, n, _ in added))
    print("② 写次级槽、板块不变          : %d" % len(slotted))
    for c, n, why in slotted:
        print("     %s %-8s %s" % (c, n, why))
    print("③ 已在医药板块、跳过          : %d" % len(skip))
    if skip:
        print("     " + "、".join("%s %s" % (c, n) for c, n, _ in skip))
    if wrong:
        print("⚠️ 未处理 %d 只:" % len(wrong))
        for c, n, why in wrong:
            print("     %s %-8s %s" % (c, n, why))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    bdir = os.path.join(ROOT, "tmp")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, "watchlist_jiuyan.json.bak_%s_ai4s"
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
