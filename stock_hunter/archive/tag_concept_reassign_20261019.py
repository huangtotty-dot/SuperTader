# -*- coding: utf-8 -*-
"""韭研概念**改派**：华天科技→封测 / 合盛硅业→半导体材料 / 国林科技→半导体材料
（2026-10-19，owner 指定）。

同族脚本：`tag_agri_20261010.py` / `tag_coalchem_20261010.py` / `tag_cybersec_20261010.py` /
`tag_silicone_20261010.py` / `tag_lowalt_snow_20261017.py`。

## 与「只增不改」铁律的关系（先读）

常规打标**只能往空槽写**，因为两个消费者对槽位顺序敏感：
  · `prescreen.sym2board_map()` 取 `category1..9` 第一个非空（**OSC 选股输入**）；
  · `data_loader` 按 `(category{i}, concept{i})` **成对**读。

本脚本是 owner 明确要求的**改派**（不是新增），但仍守住两条结构约束：
  1. **不动 `category1` 的板块**（半导体/有机硅/煤化工 原样保留）⇒ OSC 板块归属不变；
  2. **不减少概念**：只把 `concept1` 改成 owner 指定值；其余概念全部保留在对应槽位。

旧数据是 legacy 字段（`jiuyan_category` 用 `|` 拆、`jiuyan_concept` 整串套给**每个**
category ⇒ 合盛/国林在猎手里两条 category 行都显示整串概念）。本脚本把它**物化成
编号槽位**，让 `(category, concept)` 精确配对；legacy 字段原样保留作审计。

## 逐只口径

| 代码 | 名称 | 现状 | 改派后 |
|---|---|---|---|
| 002185 | 华天科技 | 无任何韭研标签（不在猎手池） | `半导体 / 封测`（纯新增，写主类目） |
| 603260 | 合盛硅业 | `半导体|有机硅` + 概念整串 | `半导体/半导体材料` + `有机硅/有机硅单体|硅橡胶|中军-硅基全产业链中军` |
| 300786 | 国林科技 | `半导体|煤化工` + `半导体设备|煤化工` | `半导体/半导体材料` + `煤化工/煤化工` |

⚠️ 国林科技的 `半导体设备` 被 owner 指定改为 `半导体材料`（这是本脚本唯一的“改”），
`煤化工` 概念保留。

## 用法

    python stock_hunter/tag_concept_reassign_20261019.py            # 干跑
    python stock_hunter/tag_concept_reassign_20261019.py --apply    # 真写（备份到 tmp/）
"""
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")

# code -> (名称, 目标 [(category, concept), ...], 模式)
#   模式 legacy：纯新增，写 jiuyan_category / jiuyan_concept（无编号槽）
#   模式 slots ：物化为编号槽位（原有 legacy 字段保留作审计）
TARGETS = {
    "002185": ("华天科技", [("半导体", "封测")], "legacy"),
    "603260": ("合盛硅业", [("半导体", "半导体材料"),
                            ("有机硅", "有机硅单体|硅橡胶|中军-硅基全产业链中军")], "slots"),
    "300786": ("国林科技", [("半导体", "半导体材料"),
                            ("煤化工", "煤化工")], "slots"),
}


def slots(v):
    return [(i, str(v.get("jiuyan_category%d" % i) or "").strip(),
             str(v.get("jiuyan_concept%d" % i) or "").strip())
            for i in range(1, 10)
            if str(v.get("jiuyan_category%d" % i) or "").strip()]


def current_pairs(v):
    sl = slots(v)
    if sl:
        return [(c, n) for _, c, n in sl]
    cats = [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]
    con = str(v.get("jiuyan_concept") or "").strip()
    return [(c, con) for c in cats]


def apply_row(v, pairs, mode, now):
    """写入目标 pairs。返回 (ok, detail)。**任何意外结构都拒绝改**。"""
    existing = slots(v)
    if mode == "legacy":
        if existing:
            return False, "已有编号槽位，拒绝按 legacy 写（请人工确认）"
        v["jiuyan_category"] = pairs[0][0]
        v["jiuyan_concept"] = pairs[0][1]
        v["jiuyan_reassign_note_2026_10_19"] = "owner 指定新增：%s/%s" % pairs[0]
        v["updated_at"] = now
        return True, "legacy 写入"

    # slots 模式：只允许从 legacy（无编号槽）物化，避免覆盖未知槽位
    if existing:
        return False, "已有编号槽位 %r，拒绝自动改派（请人工确认）" % existing
    if len(pairs) > 9:
        return False, "目标槽位 %d > 9" % len(pairs)
    for k, (cat, con) in enumerate(pairs, start=1):
        v["jiuyan_category%d" % k] = cat
        v["jiuyan_concept%d" % k] = con
    v["jiuyan_reassign_note_2026_10_19"] = (
        "owner 指定改派：%s；原 legacy=%r / %r"
        % ("；".join("%s/%s" % p for p in pairs),
           v.get("jiuyan_category"), v.get("jiuyan_concept")))
    v["updated_at"] = now
    return True, "物化 %d 个槽位" % len(pairs)


def main():
    apply = "--apply" in sys.argv
    d = json.load(open(PATH, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed, skipped, wrong = [], [], []

    print("=" * 78)
    print("韭研概念改派（%s）" % ("真写" if apply else "干跑"))
    print("=" * 78)
    for code, (name, pairs, mode) in TARGETS.items():
        v = d.get(code)
        if not isinstance(v, dict):
            wrong.append((code, name, "不在 watchlist"))
            continue
        real = str(v.get("name") or "")
        if real != name:
            wrong.append((code, name, "表内实名为 %r" % real))
            continue
        before = current_pairs(v)
        if "ST" in real.upper() or "退" in real:
            wrong.append((code, name, "ST/退市"))
            continue
        if before == pairs:
            skipped.append((code, real, "已是目标口径"))
            print("%s %-6s 已符合：%s" % (code, real, pairs))
            continue
        print("%s %-6s\n    现状: %s\n    目标: %s" % (code, real, before, pairs))
        if apply:
            ok, detail = apply_row(v, pairs, mode, now)
            if ok:
                changed.append((code, real, detail))
            else:
                wrong.append((code, real, detail))
        else:
            changed.append((code, real, "干跑"))

    print("-" * 78)
    print("改派 %d · 跳过 %d · 拒绝 %d" % (len(changed), len(skipped), len(wrong)))
    for c, n, why in wrong:
        print("  ⚠️ %s %-6s %s" % (c, n, why))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    if wrong:
        print("\n有拒绝项 ⇒ 不写盘（先人工确认）")
        return
    bdir = os.path.join(ROOT, "tmp")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, "watchlist_jiuyan.json.bak_%s_concept_reassign"
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
