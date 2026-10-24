# -*- coding: utf-8 -*-
"""分类改名 / 合并（owner 2026-10-24）。

    锂电池  → 电池          （改名，概念不动）
    算力金属 → 有色金属      （合入，概念不动：铜/锡/钨/锑/锗/镓/锆/黄金_*）

只动 `jiuyan_category` / `jiuyan_category1..9` 里的**分类 token**（复合分类按 `|` 拆开后逐段替换），
`jiuyan_concept*` 一个字不改 —— 概念名（电解液/正极材料/铜/黄金_伴生金矿…）在新分类下依然成立。

生效范围：整张 watchlist（不只是猎手池），因为 OSC 的 `sym2board_map` 读老字段。

用法：``python stock_hunter/rename_categories_20261024.py [--apply]``（默认 dry-run）
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261024_rename"
MARK = "2026-10-24 13:00:00"

RENAME = {"锂电池": "电池", "算力金属": "有色金属"}


def cat_fields(rec):
    return [k for k in rec if k == "jiuyan_category" or
            (k.startswith("jiuyan_category") and k[len("jiuyan_category"):].isdigit())]


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    changed = Counter()
    hits = []
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        touched = []
        for k in cat_fields(rec):
            v = rec.get(k)
            if not isinstance(v, str) or not v.strip():
                continue
            toks = [t.strip() for t in v.split("|") if t.strip()]
            new = [RENAME.get(t, t) for t in toks]
            if new != toks:
                for a, b in zip(toks, new):
                    if a != b:
                        changed[f"{a}→{b}"] += 1
                touched.append((k, v, "|".join(new)))
        if touched:
            hits.append((code, rec.get("name") or "?", touched))
            if apply:
                for k, _old, newv in touched:
                    rec[k] = newv
                rec["updated_at"] = MARK

    print(f"涉及 {len(hits)} 只；替换次数 {dict(changed)}")
    for code, name, touched in hits[:15]:
        for k, old, new in touched:
            print(f"  {code} {name}: {k} {old} → {new}")
    if len(hits) > 15:
        print(f"  …… 其余 {len(hits) - 15} 只同理")

    # 冲突检查：同一条记录出现两个相同分类
    dup = []
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        seen = Counter()
        for k in cat_fields(rec):
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                for t in v.split("|"):
                    if t.strip():
                        seen[t.strip()] += 1
        bad = [t for t, n in seen.items() if n > 1]
        if bad:
            dup.append((code, rec.get("name"), bad))
    print(f"\n改名后重复分类：{len(dup)} 只", dup[:5])

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
