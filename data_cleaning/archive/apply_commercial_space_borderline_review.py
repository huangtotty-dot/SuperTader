# -*- coding: utf-8 -*-
"""将独立二次复审建议合并为商业航天最终审计文件。"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
audit_path = ROOT / "commercial_space_audit_20261027.json"
review_path = ROOT / "commercial_space_borderline_review_20261027.json"
audit = json.loads(audit_path.read_text(encoding="utf-8"))
review = json.loads(review_path.read_text(encoding="utf-8"))
by_code = {row["code"]: row for row in audit}
assert len(audit) == len(by_code) == 90

for change in review["recommended_changes"]:
    row = by_code[change["code"]]
    field = change["field"]
    assert row["name"] == change["name"]
    assert row[field] == change["from"], (change["code"], field)
    row[field] = change["to"]
    evidence = change.get("evidence_recommendation")
    if evidence:
        row["evidence_title"] = evidence["title"]
        row["evidence_date"] = evidence["date"]
        row["evidence_url"] = evidence["url"]
    row["borderline_review"] = "2026-10-27二次复核修正"

for code in ("600879", "603809"):
    row = by_code[code]
    row["action"] = "纠错"
    row["review_note"] = next(
        item["rationale"] for group in ("core_review", "removed_review")
        for item in review[group] if item["code"] == code
    )

assert Counter(row["verdict"] for row in audit) == {"核心": 14, "相关": 60, "摘除": 16}
assert Counter(row["action"] for row in audit) == {"纠错": 74, "摘除": 16}
assert sum(bool(row["pairs"]) for row in audit) == 74
assert min(len(row["reason"]) for row in audit) >= 150
audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({
    "rows": len(audit),
    "verdicts": Counter(row["verdict"] for row in audit),
    "actions": Counter(row["action"] for row in audit),
    "retained": sum(bool(row["pairs"]) for row in audit),
    "concepts": len({c for row in audit for p in row["pairs"] for c in p["concepts"]}),
    "minimum_reason_chars": min(len(row["reason"]) for row in audit),
}, ensure_ascii=False, indent=2, default=dict))
