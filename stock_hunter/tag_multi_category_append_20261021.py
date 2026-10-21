# -*- coding: utf-8 -*-
"""让「已有板块」的票**同时**挂到新板块（多类目对 cat2/con2 …）（2026-10-21）。

`tag_concept_category_bind_20261021.py` 只处理了"原本没有任何大类目"的 194 只（全空才写主类目）。
剩下 57 只本来就在别的板块下（AI应用 / 网络安全 / 算力金属 / 云计算 / 冰雪经济 …），
它们的 `黄金_金矿`、`大消费_百货零售`、`数据要素_数据中心` 只作为**细分概念**显示，
不会出现在「黄金 / 大消费 / 数据要素」板块里。本脚本按**多类目对**把它们挂上去。

## 为什么必须"镜像"

`data_loader` 先收**编号槽** `(jiuyan_category1, jiuyan_concept1) …`；**只要存在任一编号槽，
不带编号的旧字段就整体被忽略**（见 `load_watchlist` 的 `if not pairs:` 分支）。所以动手前必须
把现有的那对**原样镜像进 1 号槽**，否则这只票会丢掉原板块——本仓 2026-10-19 的 lowalt
就因漏镜像伤过 8 只。

用法：``python stock_hunter/tag_multi_category_append_20261021.py [--apply]``
"""
import json
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261021_multi"
NEW_PREFIXES = ("大消费", "黄金", "数据要素")


def _concept_str(val):
    """与 data_loader._concept_str 同口径：list → '|' 连接串。"""
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return "|".join(str(v).strip() for v in val if str(v).strip())
    return str(val).strip()


def _concepts(rec):
    out = []
    jc = rec.get("jiuyan_concept")
    if isinstance(jc, list):
        out += [str(x) for x in jc if x]
    elif jc:
        out.append(str(jc))
    for i in range(1, 10):
        v = rec.get(f"jiuyan_concept{i}")
        if v:
            out += [str(x) for x in (v if isinstance(v, list) else [v]) if x]
    return out


def _loader_pairs(rec):
    """与 `data_loader.load_watchlist` **完全同口径**的现有 (cat, concept) 对。"""
    pairs = []
    for i in range(1, 10):
        cat = str(rec.get(f"jiuyan_category{i}") or "").strip()
        if cat:
            pairs.append((cat, _concept_str(rec.get(f"jiuyan_concept{i}"))))
    if not pairs:
        c0 = str(rec.get("jiuyan_category") or "").strip()
        if c0:
            con = _concept_str(rec.get("jiuyan_concept"))
            for c in [x.strip() for x in c0.split("|") if x.strip()]:
                pairs.append((c, con))
    return pairs


def _pool_size(data):
    return sum(1 for r in data.values() if _loader_pairs(r) and _concepts(r))


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    before_pool = _pool_size(data)

    todo = []
    for code, rec in data.items():
        cs = [c for c in _concepts(rec) if any(c.startswith(p) for p in NEW_PREFIXES)]
        if not cs:
            continue
        pairs = _loader_pairs(rec)
        have = {c for c, _ in pairs}
        add = []
        for parent in NEW_PREFIXES:
            if parent in have:
                continue
            sub = [c for c in cs if c.startswith(parent + "_")]
            if not sub:
                continue
            add.append((parent, "|".join(dict.fromkeys(sub))))
        if not add:
            continue
        todo.append((code, rec, pairs, add))

    print(f"需要追加多类目对的票：{len(todo)} 只")
    cnt = Counter(a[0] for _, _, _, add in todo for a in add)
    print("将追加的板块对：", dict(cnt))
    print("\n样例：")
    for code, rec, pairs, add in todo[:8]:
        print(f"  {code} {rec.get('name','')}: 现有 {pairs} + 追加 {add}")

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return

    shutil.copy2(SRC, BAK)
    for code, rec, pairs, add in todo:
        final = pairs + [p for p in add if p not in pairs]
        # 清空编号槽后按顺序重写（1 号槽 = 原主板块，保证镜像；新板块排在其后）
        for i in range(1, 10):
            rec.pop(f"jiuyan_category{i}", None)
            rec.pop(f"jiuyan_concept{i}", None)
        for i, (c, con) in enumerate(final, start=1):
            rec[f"jiuyan_category{i}"] = c
            rec[f"jiuyan_concept{i}"] = con
        # ⚠️ 不带编号的旧字段**原样保留**：`_jiuyan_board()` / OSC 的 sym2board_map 读它。

    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    d2 = json.load(open(SRC, encoding="utf-8"))
    after_pool = _pool_size(d2)
    print(f"\n已写入。备份: {BAK}")
    print(f"猎手池: {before_pool} -> {after_pool}")
    by_cat = Counter()
    for rec in d2.values():
        for c, _ in _loader_pairs(rec):
            if c in NEW_PREFIXES:
                by_cat[c] += 1
    print("新板块股票数：", {k: by_cat[k] for k in NEW_PREFIXES})
    # 安全校验：不能有票丢掉原本的板块
    lost = []
    for code, rec, pairs, _add in todo:
        now = {c for c, _ in _loader_pairs(rec)}
        for c, _ in pairs:
            if c not in now:
                lost.append((code, rec.get("name"), c))
    print(f"丢失原板块的票：{len(lost)} -> {lost[:5]}")


if __name__ == "__main__":
    main()
