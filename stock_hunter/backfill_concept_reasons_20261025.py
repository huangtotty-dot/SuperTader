# -*- coding: utf-8 -*-
"""给**每一只**股票补齐「为什么是这种判词」的理由（owner 2026-10-25）。

之前只有人工逐条写的票有 `jiuyan_concept_reason`，其余是靠前端拿 F10 主营兜底。
这里把兜底也落库，并且**按判词加一句定性**，让面板里每只票都有「主营 + 判定依据」：

    核心 → 「主营：XX，与本标签一致」
    相关 → 「主营：XX，本标签只是其中一块」
    边缘 → 「主营：XX，本标签只是延伸/关联」
    错标 → 「主营：XX，与本标签不符（建议摘牌）」

只补空的，人工写过的（人工判词）一律保留。

用法：``python stock_hunter/backfill_concept_reasons_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
PROF = r"E:\superTrader\t_io\cache\company_profile.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_reasons"
MARK = "2026-10-25 15:00:00"

TAIL = {
    "核心": "与本标签一致",
    "相关": "本标签只是其中一块",
    "边缘": "本标签只是延伸/关联",
    "错标": "与本标签不符（建议摘牌）",
}


def concepts_of(rec):
    out = []
    for i in list(range(1, 10)) + [0]:
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        v = rec.get(ck)
        out += [str(t).strip() for t in (v if isinstance(v, list) else str(v or "").split("|")) if str(t).strip()]
    return list(dict.fromkeys(out))


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    try:
        prof = json.load(open(PROF, encoding="utf-8"))
    except Exception:
        prof = {}

    filled = kept = stocks = 0
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        cons = concepts_of(rec)
        if not cons:
            continue
        verdict = str(rec.get("jiuyan_tag_verdict") or "").strip()
        p = prof.get(code) or {}
        biz = (str(p.get("brief_title") or "").strip()
               or str(p.get("brief") or "").strip()[:46]
               or str(rec.get("business_summary") or "").strip()
               or str(rec.get("sector") or "").strip())
        if not biz:
            continue
        tail = TAIL.get(verdict, "未判")
        text = f"主营：{biz[:52]}；{tail}"
        d = rec.get("jiuyan_concept_reason")
        if not isinstance(d, dict):
            d = {}
        add = {}
        for c in cons:
            if str(d.get(c) or "").strip():
                kept += 1
                continue
            add[c] = text
        if add:
            d.update(add)
            rec["jiuyan_concept_reason"] = d
            rec["updated_at"] = MARK
            filled += len(add)
            stocks += 1
    print(f"补写 {filled} 条理由，涉及 {stocks} 只；保留人工理由 {kept} 条")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
