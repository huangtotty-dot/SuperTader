# -*- coding: utf-8 -*-
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
rows = []
for i in (1, 2):
    group = json.loads((ROOT / f"optical_comm_audit_group{i}_20261027.json").read_text(encoding="utf-8"))
    for row in group:
        normalized = []
        for pair in row["pairs"]:
            concepts = pair.get("concepts")
            if concepts is None:
                concepts = [pair["concept"]]
            normalized.append({"category": pair["category"], "concepts": concepts})
        row["pairs"] = normalized
        rows.append(row)
assert len(rows) == len({x["code"] for x in rows}) == 45
(ROOT / "optical_comm_audit_20261027.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("rows", len(rows))
print("verdicts", Counter(x["verdict"] for x in rows))
print("actions", Counter(x["action"] for x in rows))
print("concepts", len({c for x in rows for p in x["pairs"] for c in p["concepts"]}))
print("min_reason", min(len(x["reason"]) for x in rows))
print("removed", [(x["code"], x["name"]) for x in rows if x["verdict"] == "摘除"])
print("related", [(x["code"], x["name"]) for x in rows if x["verdict"] == "相关"])
