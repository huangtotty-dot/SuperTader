# -*- coding: utf-8 -*-
"""合并PCB两部分独立二次复审，形成最终审计。"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
audit_path = ROOT / "pcb_audit_20261027.json"
audit = json.loads(audit_path.read_text(encoding="utf-8"))
by_code = {row["code"]: row for row in audit}
assert len(audit) == len(by_code) == 89
parts = [json.loads((ROOT / f"pcb_borderline_review_part{i}_20261027.json").read_text(encoding="utf-8")) for i in (1, 2)]
taxonomy = set(json.loads((ROOT / "pcb_taxonomy_20261027.json").read_text(encoding="utf-8")))
for part in parts:
    company_review = {x["code"]: x for x in part["company_review"]}
    for change in part["recommended_changes"]:
        row = by_code[change["code"]]
        field = change["field"]
        assert row["name"] == change["name"] and row[field] == change["from"], (change["code"], field)
        row[field] = change["to"]
        evidence = change.get("evidence_recommendation")
        if evidence is None and change.get("url"):
            evidence = {"title": change["title"], "date": change["date"], "url": change["url"]}
        if evidence:
            row["evidence_title"] = evidence["title"]
            row["evidence_date"] = evidence["date"]
            row["evidence_url"] = evidence["url"]
        row["borderline_review"] = "2026-10-27二次复核修正"
        row["review_note"] = company_review[change["code"]]["rationale"]
for row in audit:
    assert (row["verdict"] == "摘除") == (not row["pairs"])
    assert all(p["category"] == "PCB" and set(p["concepts"]) <= taxonomy for p in row["pairs"])
counts = Counter(row["verdict"] for row in audit)
assert counts == {"核心": 52, "相关": 24, "摘除": 13}, counts
combined = {
    "reviewed_at": "2026-10-27", "scope": "PCB 89只独立二次边界、概念及数字复审", "parts": parts,
    "summary": {"reviewed_companies": 89, "verdict_changes": {"301511": "相关→核心"},
                "final_verdicts": dict(counts), "field_change_count": sum(len(p["recommended_changes"]) for p in parts)},
}
(ROOT / "pcb_borderline_review_20261027.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"rows": len(audit), "verdicts": counts, "actions": Counter(x["action"] for x in audit),
                  "retained": sum(bool(x["pairs"]) for x in audit),
                  "concepts": len({c for x in audit for p in x["pairs"] for c in p["concepts"]}),
                  "occurrences": sum(len(p["concepts"]) for x in audit for p in x["pairs"]),
                  "minimum_reason_chars": min(len(x["reason"]) for x in audit),
                  "field_changes": sum(len(p["recommended_changes"]) for p in parts)}, ensure_ascii=False, indent=2, default=dict))
