# -*- coding: utf-8 -*-
"""PCB清洗独立QA：交付数字直接由JSON重算，不依赖Excel公式缓存。"""
import json
import sys
from collections import Counter
from pathlib import Path
from openpyxl import load_workbook

ROOT = Path(r"E:\superTrader")
DC = ROOT / "data_cleaning"
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_pcb_deep"
PREVIEW = DC / "watchlist_jiuyan_pcb_cleaned_20261027.json"
XLSX = DC / "PCB概念深度清洗_20261027.xlsx"
AUDIT = DC / "pcb_audit_20261027.json"
CURRENT = DC / "pcb_current_20261027.json"
REVIEW = DC / "pcb_borderline_review_20261027.json"
OUT = DC / "pcb_validation_20261027.json"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stock_hunter"))
from modules.data_loader import WatchlistLoader

formal = json.loads(SRC.read_text(encoding="utf-8"))
backup = json.loads(BAK.read_text(encoding="utf-8"))
preview = json.loads(PREVIEW.read_text(encoding="utf-8"))
audit = json.loads(AUDIT.read_text(encoding="utf-8"))
current = json.loads(CURRENT.read_text(encoding="utf-8"))
review = json.loads(REVIEW.read_text(encoding="utf-8"))
target = {x["code"] for x in audit}
assert formal == preview and len(formal) == len(backup) == 5400
assert all(formal[c] == backup[c] for c in formal if c not in target)
assert len(audit) == len(target) == len(current) == 89
assert target == {x["code"] for x in current}
assert all(len(x["reason"]) >= 150 for x in audit)
assert all(x["evidence_title"] and x["evidence_date"] and x["evidence_url"] for x in audit)
assert all(x["pairs"] == [] for x in audit if x["verdict"] == "摘除")
assert all(x["pairs"] and all(p["category"] == "PCB" for p in x["pairs"]) for x in audit if x["verdict"] != "摘除")


def toks(value):
    if isinstance(value, list):
        return [str(x) for x in value if str(x)]
    return [x for x in str(value or "").split("|") if x]


def active_pairs(rec):
    out = []
    for i in list(range(1, 10)) + [0]:
        suffix = "" if i == 0 else str(i)
        for cat in toks(rec.get("jiuyan_category" + suffix)):
            for con in toks(rec.get("jiuyan_concept" + suffix)):
                if (cat, con) not in out:
                    out.append((cat, con))
    return out


commercial_rows = [x for x in audit if any(p["category"] == "PCB" for p in x["pairs"])]
verdicts = Counter(x["verdict"] for x in commercial_rows)
actions = Counter(x["action"] for x in audit)
concepts = sorted({c for x in commercial_rows for p in x["pairs"] if p["category"] == "PCB" for c in p["concepts"]})
concept_verdicts = Counter((p["category"], c, x["verdict"]) for x in audit for p in x["pairs"] for c in p["concepts"])
core_occurrences = sum(v for (_, _, verdict), v in concept_verdicts.items() if verdict == "核心")
related_occurrences = sum(v for (_, _, verdict), v in concept_verdicts.items() if verdict == "相关")
metrics = {
    "复核股票": len(audit),
    "保留PCB": len(commercial_rows),
    "核心": verdicts["核心"],
    "相关": verdicts["相关"],
    "纠错": actions["纠错"],
    "完全摘除": actions["摘除"],
    "PCB二级概念": len(concepts),
    "概念出现次数": core_occurrences + related_occurrences,
    "最短理由字符数": min(len(x["reason"]) for x in audit),
}
assert list(metrics.values())[:8] == [89, 76, 52, 24, 70, 13, 23, 144], metrics
old_concepts = {p["concept"] for x in current for p in x["pairs"] if p["category"] == "PCB"}
removed_expected = {"000636", "000733", "001212", "001216", "002859", "300408", "301013", "301566", "301678", "603267", "603678", "605376", "688260"}
assert {x["code"] for x in audit if x["verdict"] == "摘除"} == removed_expected

for row in audit:
    code = row["code"]
    rec = formal[code]
    pairs = active_pairs(rec)
    expected = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
    got = [pair for pair in pairs if pair[0] == "PCB"]
    assert got == expected, (code, got, expected)
    # “PCB设备-封装设备”本身是标准化精确概念，允许保留；其余旧粗标签不得残留。
    assert not old_concepts.intersection(c for _, c in got)
    for _, concept in expected:
        assert rec["jiuyan_concept_reason"].get(concept) == row["reason"]
        assert rec["jiuyan_concept_desc"].get(concept)
    before_other = [pair for pair in active_pairs(backup[code]) if pair[0] != "PCB"]
    after_other = [pair for pair in pairs if pair[0] != "PCB"]
    assert before_other == after_other, (code, before_other, after_other)
    for key in backup[code]:
        if key.endswith("_verdicts") and key != "jiuyan_pcb_verdicts":
            assert rec.get(key) == backup[code].get(key), (code, key)
    audit_mark = rec["jiuyan_pcb_audit_20261027"]
    assert rec["jiuyan_pcb_audited_at"] == "2026-10-27"
    assert audit_mark["reason"] == row["reason"] and audit_mark["verdict"] == row["verdict"]
    assert rec["jiuyan_pcb_evidence"]["url"] == row["evidence_url"]
    if row["verdict"] == "摘除":
        assert "jiuyan_pcb_verdicts" not in rec
    else:
        assert rec["jiuyan_pcb_verdicts"] == {"PCB": row["verdict"]}

row = next(x for x in audit if x["code"] == "301511")
assert row["name"] == "德福科技" and row["verdict"] == "核心"
assert row.get("borderline_review") == "2026-10-27二次复核修正"
assert review["summary"]["field_change_count"] == 17
assert review["summary"]["verdict_changes"] == {"301511": "相关→核心"}
assert Counter(x["action"] for x in audit) == {"纠错": 70, "保留": 6, "摘除": 13}

loader = WatchlistLoader()
after_df = loader.load(str(SRC))
before_df = loader.load(str(BAK))
after_counts = Counter(after_df["韭研分类"])
before_counts = Counter(before_df["韭研分类"])
category_differences = {c: [before_counts[c], after_counts[c]] for c in set(before_counts) | set(after_counts) if before_counts[c] != after_counts[c]}
# 旧交叉槽位令Loader多加载8只：直接标签审计为161只，旧Loader口径为169只。
assert category_differences["PCB"][1] == 76 and category_differences["PCB"][0] >= 89
assert set(category_differences) == {"PCB", ""}, category_differences
for category in ["半导体", "光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:
    assert before_counts[category] == after_counts[category]
assert after_counts["半导体"] == 153 and after_counts["光通信"] == 42 and after_counts["商业航天"] == 74 and after_counts["军工"] == 89 and after_counts["云计算"] == 37 and after_counts["网络安全"] == 38
assert after_counts["AI应用"] == 84 and after_counts["物理AI"] == 54 and after_counts["数据要素"] == 77
assert len(after_df.loc[after_df["韭研分类"] == "PCB", "代码"]) == 76
loader_counts = {c: after_counts[c] for c in ["PCB", "半导体", "光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]}

wb = load_workbook(XLSX, data_only=False)
assert wb.sheetnames == ["封面", "清洗总表", "PCB", "摘除清单", "概念展开", "概念统计"]
assert wb["概念展开"].sheet_state == "hidden"
assert wb["清洗总表"].max_row == 93 and wb["PCB"].max_row == 80 and wb["摘除清单"].max_row == 17
assert wb["概念展开"].max_row == 145 and wb["概念统计"].max_row == 28
flat_rows = [tuple(cell.value for cell in row) for row in wb["概念展开"].iter_rows(min_row=2, max_col=5)]
expected_flat = [(p["category"], c, x["verdict"], x["code"], x["name"]) for x in audit for p in x["pairs"] for c in p["concepts"]]
assert flat_rows == expected_flat
stats = {}
for row in wb["概念统计"].iter_rows(min_row=5, max_row=27, min_col=2, max_col=3, values_only=True):
    stats[row] = (concept_verdicts[(row[0], row[1], "核心")], concept_verdicts[(row[0], row[1], "相关")])
assert len(stats) == 23 and {con for _, con in stats} == set(concepts)
assert sum(v[0] for v in stats.values()) == core_occurrences
assert sum(v[1] for v in stats.values()) == related_occurrences
formula_count = sum(1 for ws in wb for row in ws.iter_rows() for cell in row if cell.data_type == "f")
assert formula_count == 78
assert wb.calculation.calcMode == "auto" and wb.calculation.fullCalcOnLoad and wb.calculation.forceFullCalc
wb.close()

result = {
    "status": "pass_independent_qa",
    "metrics": metrics,
    "concept_occurrences_by_verdict": {"核心": core_occurrences, "相关": related_occurrences, "合计": core_occurrences + related_occurrences},
    "formal_equals_preview": True,
    "backup_records": len(backup),
    "non_target_records_unchanged": len(formal) - len(target),
    "other_category_pairs_and_verdicts_preserved": True,
    "category_differences_loader": category_differences,
    "loader_counts": loader_counts,
    "borderline_second_review": {"verdict_changes": {"301511": "相关→核心"}, "field_change_count": 17},
    "workbook_formula_count": formula_count,
    "workbook_formula_gate": "deferred_calcMode_auto; 78 formula caches unavailable; all reported values independently recomputed from JSON",
    "workbook_independent_stats": {f"{cat}/{con}": {"核心": counts[0], "相关": counts[1]} for (cat, con), counts in stats.items()},
}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
