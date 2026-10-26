# -*- coding: utf-8 -*-
"""AI4S 制药：把 `AI4S制药领域` 补成**二级概念**，三级标签保持在其后（2026-10-19，owner）。

## 背景

`tag_ai4s_pharma_20261017.py` 当时把韭研图的「分类」列（蛋白合成/基因测序仪/实验鼠…）
**直接写成了 `jiuyan_concept`**，等于把**三级概念**当成了二级概念。owner 口径：

    医药（一级） → AI4S制药领域（二级） → 蛋白合成/实验鼠/…（三级）

现有字段只有 `jiuyan_category` + `jiuyan_concept` 两级，故用 **`|` 串**编码：
`jiuyan_concept = "AI4S制药领域|<三级标签>|<原 concept 其余 token>"`。
`data_loader` 的子概念汇总本来就会按 `|` 拆成多个子概念 ⇒ 二级、三级都会出现在报表里，
且 `所属板块` 保留完整串。

## 覆盖

49 只（沿用 `tag_ai4s_pharma_20261017.ROWS`）：
  · 46 只 legacy（category = 医药/创新药/中药，单板块）⇒ 改 `jiuyan_concept`；
  · 3 只编号槽位（药石科技/成都先导/凯莱英：AI应用 + 医药）⇒ 只改**医药槽**的 concept。
每只保留原有概念（如恒瑞的 ADC、昭衍的 CXO），只在其前面插入二级概念 + 三级标签。
写盘前自证：49 只的医药侧 concept 必须以 `AI4S制药领域` 开头且包含截图三级标签。

## 用法

    python stock_hunter/tag_ai4s_level2_20261019.py            # 干跑
    python stock_hunter/tag_ai4s_level2_20261019.py --apply    # 真写（备份 tmp/）
"""
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tag_ai4s_pharma_20261017 as base  # noqa: E402

LEVEL2 = "AI4S制药领域"
MERGES = {"创新药": "医药", "中药": "医药"}


def slots(v):
    return [(i, str(v.get("jiuyan_category%d" % i) or "").strip(),
             str(v.get("jiuyan_concept%d" % i) or "").strip())
            for i in range(1, 10)
            if str(v.get("jiuyan_category%d" % i) or "").strip()]


def dedupe(tokens):
    out = []
    for t in tokens:
        t = str(t or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def rebuild(concept, level3):
    """二级概念 + 截图三级标签 + 原有 token（去重、保序）。"""
    old = [t.strip() for t in str(concept or "").split("|") if t.strip()]
    l3 = [t.strip() for t in str(level3 or "").split("|") if t.strip()]
    return "|".join(dedupe([LEVEL2] + l3 + old))


def main():
    apply = "--apply" in sys.argv
    d = json.load(open(PATH, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    todo, skip, wrong = [], [], []

    for code, name, level3 in base.ROWS:
        v = d.get(code)
        if not isinstance(v, dict):
            wrong.append((code, name, "不在 watchlist"))
            continue
        real = str(v.get("name") or "")
        if real != name:
            wrong.append((code, name, "表内实名为 %r" % real))
            continue
        sl = slots(v)
        if sl:
            med = [(i, c, n) for i, c, n in sl if MERGES.get(c, c) == "医药"]
            if not med:
                wrong.append((code, name, "有编号槽但没有医药槽，拒绝自动加槽"))
                continue
            changes = []
            for i, c, n in med:
                new = rebuild(n, level3)
                if new != n:
                    changes.append((i, c, n, new))
            if not changes:
                skip.append((code, real, "已是目标口径"))
            else:
                todo.append((code, real, "slot", changes))
        else:
            cats = [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]
            if not any(MERGES.get(c, c) == "医药" for c in cats):
                wrong.append((code, name, "legacy 但不在医药板块，拒绝改"))
                continue
            con = str(v.get("jiuyan_concept") or "").strip()
            new = rebuild(con, level3)
            if new == con:
                skip.append((code, real, "已是目标口径"))
            else:
                todo.append((code, real, "legacy", ([c for c in cats], con, new)))

    print("=" * 78)
    print("AI4S 二级概念补齐（%s）" % ("真写" if apply else "干跑"))
    print("=" * 78)
    for code, name, kind, payload in todo:
        if kind == "legacy":
            cats, con, new = payload
            print("%s %-8s %s\n    %r → %r" % (code, name, "|".join(cats), con, new))
        else:
            print("%s %-8s" % (code, name))
            for i, c, con, new in payload:
                print("    cat%d/%s: %r → %r" % (i, c, con, new))
    print("-" * 78)
    print("待改 %d · 跳过 %d · 拒绝 %d" % (len(todo), len(skip), len(wrong)))
    for c, n, why in wrong:
        print("  ⚠️ %s %-8s %s" % (c, n, why))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    if wrong:
        print("\n有拒绝项 ⇒ 不写盘（先人工确认）")
        return

    for code, name, kind, payload in todo:
        v = d[code]
        if kind == "legacy":
            cats, con, new = payload
            v["jiuyan_concept"] = new
            v["jiuyan_level2_note_2026_10_19"] = "二级概念 %s 前置：%r → %r" % (LEVEL2, con, new)
        else:
            for i, c, con, new in payload:
                v["jiuyan_concept%d" % i] = new
                v.setdefault("jiuyan_level2_note_2026_10_19", []).append(
                    "cat%d/二级概念 %s 前置：%r → %r" % (i, LEVEL2, con, new))
        v["updated_at"] = now

    # 写盘前自证：49 只医药侧 concept 必须以二级概念开头，且含截图三级标签
    bad = []
    for code, name, level3 in base.ROWS:
        v = d.get(code) or {}
        sl = slots(v)
        if sl:
            med = [n for _, c, n in sl if MERGES.get(c, c) == "医药"]
        else:
            med = [str(v.get("jiuyan_concept") or "")]
        if not med or not all(str(n).startswith(LEVEL2) for n in med):
            bad.append((code, name, "缺二级概念"))
            continue
        l3 = [t.strip() for t in str(level3).split("|") if t.strip()]
        if not all(t in str(med[0]).split("|") for t in l3):
            bad.append((code, name, "缺三级标签 %r" % l3))
    if bad:
        raise SystemExit("自证失败：%r，拒绝写盘" % bad[:5])

    bdir = os.path.join(ROOT, "tmp")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, "watchlist_jiuyan.json.bak_%s_ai4s_level2"
                       % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(PATH, bak)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)
    print("\n已写入 %s（indent=1）" % PATH)
    print("备份 %s" % bak)


if __name__ == "__main__":
    main()
