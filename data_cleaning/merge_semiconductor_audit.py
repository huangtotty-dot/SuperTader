# -*- coding: utf-8 -*-
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
taxonomy = set(json.loads((ROOT / "semiconductor_taxonomy_20261027.json").read_text(encoding="utf-8")))
rows = []
for i in range(1, 7):
    rows.extend(json.loads((ROOT / f"semiconductor_audit_group{i}_20261027.json").read_text(encoding="utf-8")))
assert len(rows) == len({x["code"] for x in rows}) == 161
assert all(set(x) == {"code", "name", "action", "pairs", "verdict", "reason", "evidence_title", "evidence_date", "evidence_url"} for x in rows)
assert all(p["category"] == "半导体" and set(p["concepts"]) <= taxonomy for x in rows for p in x["pairs"])
(ROOT / "semiconductor_audit_20261027.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("rows", len(rows))
print("verdicts", Counter(x["verdict"] for x in rows))
print("actions", Counter(x["action"] for x in rows))
print("concepts", len({c for x in rows for p in x["pairs"] for c in p["concepts"]}))
print("occurrences", sum(len(p["concepts"]) for x in rows for p in x["pairs"]))
print("min_reason", min(len(x["reason"]) for x in rows))
print("removed", [(x["code"], x["name"]) for x in rows if x["verdict"] == "摘除"])
print("related", [(x["code"], x["name"]) for x in rows if x["verdict"] == "相关"])
