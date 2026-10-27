# -*- coding: utf-8 -*-
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader")
src = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
out = ROOT / "data_cleaning" / "military_current_20261027.json"
data = json.loads(src.read_text(encoding="utf-8"))
rows = []
for code, rec in data.items():
    pairs = []
    for i in list(range(1, 10)) + [0]:
        suffix = "" if i == 0 else str(i)
        cats = rec.get("jiuyan_category" + suffix, "")
        cons = rec.get("jiuyan_concept" + suffix, "")
        cats = cats if isinstance(cats, list) else [x for x in str(cats).split("|") if x]
        cons = cons if isinstance(cons, list) else [x for x in str(cons).split("|") if x]
        for cat in cats:
            for con in cons:
                pairs.append({"category": cat, "concept": con, "slot": i})
    if not any(p["category"] == "军工" for p in pairs):
        continue
    rows.append({
        "code": code,
        "name": rec.get("name", ""),
        "pairs": pairs,
        "verdict": rec.get("jiuyan_tag_verdict", ""),
        "reasons": rec.get("jiuyan_concept_reason", {}),
        "business_summary": rec.get("business_summary", ""),
        "sector": rec.get("sector", ""),
    })
out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
print("军工股票", len(rows))
print("旧概念", Counter(p["concept"] for r in rows for p in r["pairs"] if p["category"] == "军工"))
for r in rows:
    print(r["code"], r["name"], "；".join(f'{p["category"]}/{p["concept"]}' for p in r["pairs"]))
print(out)
