# -*- coding: utf-8 -*-
"""合并光通信独立二次复审的数字修正。"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
audit_path = ROOT / "optical_comm_audit_20261027.json"
review = json.loads((ROOT / "optical_comm_borderline_review_20261027.json").read_text(encoding="utf-8"))
audit = json.loads(audit_path.read_text(encoding="utf-8"))
by_code = {row["code"]: row for row in audit}
assert len(audit) == len(by_code) == 45
for change in review["recommended_changes"]:
    row = by_code[change["code"]]
    assert row["name"] == change["name"] and row[change["field"]] == change["from"]
    row[change["field"]] = change["to"]
    row["borderline_review"] = "2026-10-27二次复核数字修正"
assert Counter(row["verdict"] for row in audit) == {"核心": 30, "相关": 12, "摘除": 3}
assert Counter(row["action"] for row in audit) == {"纠错": 42, "摘除": 3}
assert min(len(row["reason"]) for row in audit) >= 180
audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({
    "rows": len(audit), "verdicts": Counter(row["verdict"] for row in audit),
    "retained": sum(bool(row["pairs"]) for row in audit),
    "concepts": len({c for row in audit for p in row["pairs"] for c in p["concepts"]}),
    "minimum_reason_chars": min(len(row["reason"]) for row in audit),
    "numeric_corrections": [x["code"] for x in review["recommended_changes"]],
}, ensure_ascii=False, indent=2, default=dict))
