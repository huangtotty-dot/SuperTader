# -*- coding: utf-8 -*-
"""网络安全清洗独立QA：不依赖Excel公式缓存计算交付指标。"""
import json
import sys
from collections import Counter
from pathlib import Path
from openpyxl import load_workbook

ROOT = Path(r"E:\superTrader")
DC = ROOT / "data_cleaning"
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_cybersecurity_deep"
PREVIEW = DC / "watchlist_jiuyan_cybersecurity_cleaned_20261027.json"
XLSX = DC / "网络安全概念深度清洗_20261027.xlsx"
AUDIT = DC / "cybersecurity_audit_20261027.json"
OUT = DC / "cybersecurity_validation_20261027.json"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stock_hunter"))
from modules.data_loader import WatchlistLoader

formal = json.loads(SRC.read_text(encoding="utf-8"))
backup = json.loads(BAK.read_text(encoding="utf-8"))
preview = json.loads(PREVIEW.read_text(encoding="utf-8"))
audit = json.loads(AUDIT.read_text(encoding="utf-8"))
target = {x["code"] for x in audit}
assert formal == preview and len(formal) == len(backup) == 5400
assert all(formal[c] == backup[c] for c in formal if c not in target)
assert len(audit) == len(target) == 45
assert all(len(x["reason"]) >= 80 for x in audit)

verdicts = Counter(x["verdict"] for x in audit)
actions = Counter(x["action"] for x in audit)
concepts = sorted({c for x in audit for p in x["pairs"] for c in p["concepts"]})
concept_occurrences = sum(len(p["concepts"]) for x in audit for p in x["pairs"])
concept_verdicts = Counter((p["category"], c, x["verdict"]) for x in audit for p in x["pairs"] for c in p["concepts"])
metrics = {
    "复核股票": len(audit),
    "保留网络安全": len(audit) - verdicts["摘除"],
    "核心": verdicts["核心"],
    "相关": verdicts["相关"],
    "摘除": verdicts["摘除"],
    "纠错": actions["纠错"],
    "新增": actions["新增"],
    "二级概念": len(concepts),
    "概念出现次数": concept_occurrences,
    "最短理由字符数": min(len(x["reason"]) for x in audit),
}
assert list(metrics.values())[:9] == [45, 38, 30, 8, 7, 36, 2, 47, 67]
assert sum(v for (_, _, verdict), v in concept_verdicts.items() if verdict == "核心") == 57
assert sum(v for (_, _, verdict), v in concept_verdicts.items() if verdict == "相关") == 10

for row in audit:
    rec = formal[row["code"]]
    expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "网络安全" for c in p["concepts"]]
    got = []
    for i in list(range(1, 10)) + [0]:
        suffix = "" if i == 0 else str(i)
        if rec.get("jiuyan_category" + suffix) == "网络安全":
            value = rec.get("jiuyan_concept" + suffix, "")
            values = value if isinstance(value, list) else [x for x in str(value).split("|") if x]
            got.extend(("网络安全", c) for c in values)
    assert got == expected, (row["code"], got, expected)
    for _, concept in expected:
        assert rec["jiuyan_concept_reason"].get(concept) == row["reason"]
        assert rec["jiuyan_concept_desc"].get(concept)
    old_forbidden = {"其他", "国家队", "字节生态", "AI应用", "SaaS软件与服务"}
    assert not old_forbidden.intersection(c for _, c in got)

loader = WatchlistLoader()
after_df = loader.load(str(SRC))
before_df = loader.load(str(BAK))
after_counts = Counter(after_df["韭研分类"])
before_counts = Counter(before_df["韭研分类"])
category_differences = {c: [before_counts[c], after_counts[c]] for c in set(before_counts) | set(after_counts) if before_counts[c] != after_counts[c]}
assert category_differences == {"": [3079, 3085], "网络安全": [45, 38]}, category_differences
network_codes = set(after_df.loc[after_df["韭研分类"] == "网络安全", "代码"])
assert len(network_codes) == 38
loader_counts = {c: after_counts[c] for c in ["网络安全", "AI应用", "物理AI", "数据要素"]}

wb = load_workbook(XLSX, data_only=False)
assert wb.sheetnames == ["封面", "清洗总表", "网络安全", "摘除清单", "概念展开", "概念统计"]
assert wb["概念展开"].sheet_state == "hidden"
assert wb["清洗总表"].max_row == 49 and wb["网络安全"].max_row == 42 and wb["摘除清单"].max_row == 11
assert wb["概念展开"].max_row == 68 and wb["概念统计"].max_row == 52
formula_count = sum(1 for ws in wb for row in ws.iter_rows() for cell in row if cell.data_type == "f")
assert formula_count == 151
assert wb.calculation.calcMode == "auto" and wb.calculation.fullCalcOnLoad and wb.calculation.forceFullCalc
wb.close()

result = {
    "status": "pass_independent_qa",
    "metrics": metrics,
    "concept_occurrences_by_verdict": {"核心": 57, "相关": 10, "合计": 67},
    "formal_equals_preview": True,
    "backup_records": len(backup),
    "non_target_records_unchanged": len(formal) - len(target),
    "category_differences_loader": category_differences,
    "loader_counts": loader_counts,
    "workbook_formula_count": formula_count,
    "workbook_formula_gate": "deferred_calcMode_auto; independent QA used for all reported values",
}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
