# -*- coding: utf-8 -*-
"""韭研「肺炎球菌疫苗(261007)」→ 补进 **医药** 板块（owner 2026-10-17）。

同族脚本：`tag_ai4s_pharma_20261017.py`（AI4S 制药）、`tag_lowalt_snow_20261017.py` 等 ——
放置逻辑与口径完全一致，本文件只是换了一张名单。

## 口径（只增不改，两条硬约束）

1. 两个字段都空才写主类目；只有旧字段则**下沉**（原类目按 `|` 拆进 `category1..N`，
   **且必须同时镜像 `concept1..N`**）；已有槽位则写最后一个非空槽之后的空槽。
2. **归并后判重**：`创新药/中药` 都算「医药」⇒ 本来就在医药板块的票跳过，不重复占槽。

## 名单

9 只，全部名称唯一解析、全部在池内（`复星医药` 已在医药板块，会走跳过分支）。

## 用法

    python stock_hunter/tag_pneumo_vaccine_20261017.py            # 干跑
    python stock_hunter/tag_pneumo_vaccine_20261017.py --apply    # 真写（备份到 tmp/）
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
    ("300142", "沃森生物", "已上市"),
    ("300601", "康泰生物", "已上市"),
    ("300122", "智飞生物", "已上市"),
    ("688185", "康希诺", "已上市"),
    ("600196", "复星医药", "未上市"),
    ("688739", "成大生物", "未上市"),
    ("603392", "万泰生物", "未上市"),
    ("688670", "金迪克", "未上市"),
    ("688319", "欧林生物", "未上市"),
]
# 截图名的别名（表内实名不同）
NAME_ALIAS = {}
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
    print("肺炎球菌疫苗 → 医药板块：名单 %d 只" % len(STOCKS))
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
