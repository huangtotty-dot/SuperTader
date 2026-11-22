# -*- coding: utf-8 -*-
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
taxonomy = set(json.loads((ROOT / "pcb_taxonomy_20261027.json").read_text(encoding="utf-8")))
rows = []
for i in range(1, 4):
    rows.extend(json.loads((ROOT / f"pcb_audit_group{i}_20261027.json").read_text(encoding="utf-8")))
assert len(rows) == len({x["code"] for x in rows}) == 89
assert all(p["category"] == "PCB" and set(p["concepts"]) <= taxonomy for x in rows for p in x["pairs"])
(ROOT / "pcb_audit_20261027.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("rows", len(rows))
print("verdicts", Counter(x["verdict"] for x in rows))
print("actions", Counter(x["action"] for x in rows))
print("concepts", len({c for x in rows for p in x["pairs"] for c in p["concepts"]}))
print("occurrences", sum(len(p["concepts"]) for x in rows for p in x["pairs"]))
print("min_reason", min(len(x["reason"]) for x in rows))
print("removed", [(x["code"], x["name"]) for x in rows if x["verdict"] == "摘除"])
print("related", [(x["code"], x["name"]) for x in rows if x["verdict"] == "相关"])
