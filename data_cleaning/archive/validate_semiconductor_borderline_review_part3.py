# -*- coding: utf-8 -*-
import json
from collections import Counter
from pathlib import Path

root = Path(r"E:\superTrader\data_cleaning")
out_path = root / "semiconductor_borderline_review_part3_20261027.json"
data = json.loads(out_path.read_text(encoding="utf-8"))
initial = json.loads((root / "semiconductor_borderline_review_part3_input_20261027.json").read_text(encoding="utf-8"))
research = json.loads((root / "semiconductor_research_group5_20261027.json").read_text(encoding="utf-8"))
research += json.loads((root / "semiconductor_research_group6_20261027.json").read_text(encoding="utf-8"))
expected_codes = [x["code"] for x in research]
initial_by_code = {x["code"]: x for x in initial}

assert set(data) == {"reviewed_at", "scope", "company_review", "numeric_findings", "consistency_findings", "recommended_changes", "summary"}
assert len(data["company_review"]) == len(data["numeric_findings"]) == 53
assert [x["code"] for x in data["company_review"]] == expected_codes
assert [x["code"] for x in data["numeric_findings"]] == expected_codes
assert len(set(expected_codes)) == 53
required_review = {"code", "name", "initial_verdict", "recommended_verdict", "decision", "rationale"}
assert all(required_review <= set(x) for x in data["company_review"])
assert all(x["initial_verdict"] == x["recommended_verdict"] for x in data["company_review"])
assert Counter(x["recommended_verdict"] for x in data["company_review"]) == Counter({"核心": 38, "相关": 14, "摘除": 1})

changes = data["recommended_changes"]
assert len(changes) == 9 and len({x["code"] for x in changes}) == 8
required_change = {"code", "name", "field", "from", "to", "reason", "evidence_title", "evidence_date", "evidence_url"}
assert all(required_change <= set(x) for x in changes)
valid_urls = {x["evidence_url"] for x in initial}
assert all(x["evidence_url"] in valid_urls for x in changes)
assert all(x["evidence_title"] and x["evidence_date"] and x["reason"] for x in changes)
review_by_code = {x["code"]: x for x in data["company_review"]}
for change in changes:
    old = initial_by_code[change["code"]]
    if change["field"] == "reason":
        assert change["from"] == old["reason"]
        assert isinstance(change["to"], str) and len(change["to"]) > 100
        assert review_by_code[change["code"]]["rationale"] == change["to"]
    elif change["field"] == "pairs":
        assert change["code"] == "688187"
        assert change["from"] == old["pairs"]
        assert change["to"] == [{"category": "半导体", "concepts": ["芯片设计-功率半导体", "晶圆制造与IDM"]}]
    else:
        raise AssertionError(change)
assert sum(x["field"] == "reason" for x in changes) == 8
assert sum(x["field"] == "pairs" for x in changes) == 1

for item in data["numeric_findings"]:
    assert item["2025_total_revenue_yi"] > 0
    assert item["revenue_basis"]
    assert item["boundary_note"]
    assert item["source"].startswith("[Source: iFinD")
    for field in ["industry_segments", "product_segments"]:
        for segment in item[field]:
            assert {"name", "revenue_yi", "share_of_total_pct"} <= set(segment)
            if segment["revenue_yi"] is not None:
                assert segment["share_of_total_pct"] is not None

summary = data["summary"]
assert summary["reviewed_companies"] == 53
assert summary["verdict_changed_companies"] == 0
assert summary["concept_changed_companies"] == 1
assert summary["reason_corrected_companies"] == 8
assert summary["numeric_corrected_companies"] == 7
assert summary["field_change_count"] == 9
assert summary["companies_with_field_changes"] == 8
assert summary["fully_unchanged_companies"] == 45

print(json.dumps({
    "validation": "PASS",
    "file": str(out_path),
    "bytes": out_path.stat().st_size,
    "reviewed": 53,
    "verdict_changes": 0,
    "verdict_counts": {"核心": 38, "相关": 14, "摘除": 1},
    "concept_changes": 1,
    "reason_corrections": 8,
    "recommended_changes": 9,
    "affected_companies": 8,
}, ensure_ascii=False, indent=2))
