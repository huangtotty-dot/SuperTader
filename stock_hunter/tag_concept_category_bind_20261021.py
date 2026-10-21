# -*- coding: utf-8 -*-
"""把新概念（大消费 / 黄金 / 数据要素）绑定到 `jiuyan_category`（2026-10-21）。

## 为什么需要这一步

`tag_consume_gold_20261021.py` / `tag_dataelement_20261021.py` 只写了
`jiuyan_concept`（细分），没写 `jiuyan_category`（大类目）。而猎手的
`data_loader` 是**成对**读的：

    if cat and str(cat).strip():        # ← category 空 ⇒ 这一对整体不产出
        pairs.append((cat, concept))
    ...
    else:  record["韭研分类"] = ""   record["韭研概念"] = ""     # 概念被丢掉

⇒ 194 只"本来没有大类目"的票：概念写了也**看不见**，而且因为池子是
`韭研概念 != ""` 过滤的，它们**整个掉出猎手池**。owner 报「跑完猎手 GUI 上
不显示这些概念」正是这个。

## 口径（沿用本仓铁律：只增不改）

* **全空才写主类目**：该票的 `jiuyan_category` 与 `jiuyan_category{1..9}` 全为空
  ⇒ 把概念前缀（`大消费_休闲食品` 的 `大消费`）写进 `jiuyan_category`。
* **已有大类目的不动**（57 只）：它们本来就在某个板块下（AI应用/网络安全/…），
  概念已经作为"细分"显示；改主类目会挪走它们的板块归属。
  如需让它们**同时**出现在新板块下，得按多类目对（cat2/con2）追加 —— 那是另一个决定，
  本脚本不做，见输出里的清单。

用法：``python stock_hunter/tag_concept_category_bind_20261021.py [--apply]``
     不加 ``--apply`` 只打印将要做的改动（dry-run）。
"""
import json
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261021_category"
NEW_PREFIXES = ("大消费", "黄金", "数据要素")


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


def _cats(rec):
    out = []
    c0 = str(rec.get("jiuyan_category") or "").strip()
    if c0:
        out += [x.strip() for x in c0.split("|") if x.strip()]
    for i in range(1, 10):
        v = str(rec.get(f"jiuyan_category{i}") or "").strip()
        if v:
            out.append(v)
    return out


def _pool_size(data):
    """猎手池口径：`韭研概念` 非空（= loader 真的产出了一对 cat/con）。"""
    n = 0
    for rec in data.values():
        if _cats(rec) and _concepts(rec):
            n += 1
    return n


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    before_pool = _pool_size(data)

    todo, skipped = [], []
    for code, rec in data.items():
        cs = [c for c in _concepts(rec) if any(c.startswith(p) for p in NEW_PREFIXES)]
        if not cs:
            continue
        parents = []
        for c in cs:
            p = c.split("_", 1)[0]
            if p in NEW_PREFIXES and p not in parents:
                parents.append(p)
        if not parents:
            continue
        cats = _cats(rec)
        if cats:
            skipped.append((code, rec.get("name", ""), cs, cats))
            continue
        todo.append((code, rec, parents))

    print(f"待写大类目：{len(todo)} 只   已有大类目（保持不动）：{len(skipped)} 只")
    cnt = Counter()
    for code, rec, parents in todo:
        cnt["|".join(parents)] += 1
    print("将写入的大类目分布：", dict(cnt))
    print("\n样例：")
    for code, rec, parents in todo[:8]:
        print(f"  {code} {rec.get('name','')}: category '' -> {'|'.join(parents)}   concept={rec.get('jiuyan_concept')}")

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return

    shutil.copy2(SRC, BAK)
    for code, rec, parents in todo:
        rec["jiuyan_category"] = "|".join(parents)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    d2 = json.load(open(SRC, encoding="utf-8"))
    after_pool = _pool_size(d2)
    print(f"\n已写入。备份: {BAK}")
    print(f"猎手池（概念且有大类目）: {before_pool} -> {after_pool} (+{after_pool - before_pool})")
    # 复核：三个新板块各有多少只
    by_cat = Counter()
    for rec in d2.values():
        for c in _cats(rec):
            if c in NEW_PREFIXES:
                by_cat[c] += 1
    print("新板块股票数：", {k: by_cat[k] for k in NEW_PREFIXES})


if __name__ == "__main__":
    main()
