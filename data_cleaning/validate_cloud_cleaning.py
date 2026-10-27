# -*- coding: utf-8 -*-
"""云计算清洗独立QA：所有交付数字直接由JSON重算，不依赖Excel公式缓存。"""
import json
import sys
from collections import Counter
from pathlib import Path
from openpyxl import load_workbook

ROOT = Path(r"E:\superTrader")
DC = ROOT / "data_cleaning"
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_cloud_deep"
PREVIEW = DC / "watchlist_jiuyan_cloud_cleaned_20261027.json"
XLSX = DC / "云计算概念深度清洗_20261027.xlsx"
AUDIT = DC / "cloud_audit_20261027.json"
OUT = DC / "cloud_validation_20261027.json"
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
assert all(x["evidence_title"] and x["evidence_date"] and x["evidence_url"] for x in audit)

cloud_rows = [x for x in audit if any(p["category"] == "云计算" for p in x["pairs"])]
verdicts = Counter(x["verdict"] for x in cloud_rows)
actions = Counter(x["action"] for x in audit)
concepts = sorted({c for x in cloud_rows for p in x["pairs"] if p["category"] == "云计算" for c in p["concepts"]})
all_concepts = sorted({(p["category"], c) for x in audit for p in x["pairs"] for c in p["concepts"]})
concept_verdicts = Counter((p["category"], c, x["verdict"]) for x in audit for p in x["pairs"] for c in p["concepts"])
core_occurrences = sum(v for (_, _, verdict), v in concept_verdicts.items() if verdict == "核心")
related_occurrences = sum(v for (_, _, verdict), v in concept_verdicts.items() if verdict == "相关")
metrics = {
    "复核股票": len(audit),
    "保留云计算": len(cloud_rows),
    "核心": verdicts["核心"],
    "相关": verdicts["相关"],
    "纠错": actions["纠错"],
    "迁移": actions["迁移"],
    "完全摘除": actions["摘除"],
    "云计算二级概念": len(concepts),
    "概念出现次数_含迁移": core_occurrences + related_occurrences,
    "最短理由字符数": min(len(x["reason"]) for x in audit),
}
assert list(metrics.values())[:9] == [45, 37, 28, 9, 37, 1, 7, 23, 59], metrics
assert core_occurrences == 47 and related_occurrences == 12

for row in audit:
    rec = formal[row["code"]]
    got_all = []
    for i in list(range(1, 10)) + [0]:
        suffix = "" if i == 0 else str(i)
        categories = rec.get("jiuyan_category" + suffix, "")
        categories = categories if isinstance(categories, list) else [categories]
        concepts_value = rec.get("jiuyan_concept" + suffix, "")
        concepts_value = concepts_value if isinstance(concepts_value, list) else str(concepts_value).split("|")
        for category in categories:
            got_all.extend((category, c) for c in concepts_value if c)
    expected_all = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
    for pair in expected_all:
        assert pair in got_all, (row["code"], pair, got_all)
        assert rec["jiuyan_concept_reason"].get(pair[1]) == row["reason"]
        assert rec["jiuyan_concept_desc"].get(pair[1])
    got_cloud = [pair for pair in got_all if pair[0] == "云计算"]
    expected_cloud = [pair for pair in expected_all if pair[0] == "云计算"]
    assert got_cloud == expected_cloud, (row["code"], got_cloud, expected_cloud)
    forbidden = {"SaaS软件与服务", "IaaS基础设施服务", "国资云", "PaaS平台即服务", "DeepSeek", "DeepSeek生态"}
    assert not forbidden.intersection(c for _, c in got_cloud)

star = formal["002439"]
assert ("网络安全", "云安全与SaaS安全服务") in [
    (p["category"], c) for p in next(x for x in audit if x["code"] == "002439")["pairs"] for c in p["concepts"]
]
assert star["jiuyan_cybersecurity_verdicts"]["网络安全"] == "核心"
assert star["jiuyan_cybersecurity_verdicts"] == backup["002439"]["jiuyan_cybersecurity_verdicts"]
assert star["jiuyan_tag_verdict"] == backup["002439"]["jiuyan_tag_verdict"] == "核心"
assert "云计算" not in star.get("jiuyan_cloud_verdicts", {})

loader = WatchlistLoader()
after_df = loader.load(str(SRC))
before_df = loader.load(str(BAK))
after_counts = Counter(after_df["韭研分类"])
before_counts = Counter(before_df["韭研分类"])
category_differences = {c: [before_counts[c], after_counts[c]] for c in set(before_counts) | set(after_counts) if before_counts[c] != after_counts[c]}
assert category_differences["云计算"] == [45, 37]
# 启明星辰的一级分类原本就是网络安全，本次只调整二级分类，因此一级计数不变。
assert set(category_differences) == {"云计算", ""}, category_differences
for category in ["网络安全", "AI应用", "物理AI", "数据要素"]:
    assert before_counts[category] == after_counts[category]
assert after_counts["网络安全"] == 38 and after_counts["AI应用"] == 84
assert after_counts["物理AI"] == 54 and after_counts["数据要素"] == 77
assert len(after_df.loc[after_df["韭研分类"] == "云计算", "代码"]) == 37
loader_counts = {c: after_counts[c] for c in ["云计算", "网络安全", "AI应用", "物理AI", "数据要素"]}

wb = load_workbook(XLSX, data_only=False)
assert wb.sheetnames == ["封面", "清洗总表", "云计算", "跨分类迁移", "摘除清单", "概念展开", "概念统计"]
assert wb["概念展开"].sheet_state == "hidden"
assert wb["清洗总表"].max_row == 49 and wb["云计算"].max_row == 41
assert wb["跨分类迁移"].max_row == 5 and wb["摘除清单"].max_row == 11
assert wb["概念展开"].max_row == 60 and wb["概念统计"].max_row == 29
flat_rows = [tuple(cell.value for cell in row) for row in wb["概念展开"].iter_rows(min_row=2, max_col=5)]
expected_flat = [(p["category"], c, x["verdict"], x["code"], x["name"]) for x in audit for p in x["pairs"] for c in p["concepts"]]
assert flat_rows == expected_flat
stats = {}
for row in wb["概念统计"].iter_rows(min_row=5, max_row=28, min_col=2, max_col=3, values_only=True):
    stats[row] = (concept_verdicts[(row[0], row[1], "核心")], concept_verdicts[(row[0], row[1], "相关")])
assert len(stats) == 24 and set(stats) == set(all_concepts)
assert sum(v[0] for v in stats.values()) == 47 and sum(v[1] for v in stats.values()) == 12
formula_count = sum(1 for ws in wb for row in ws.iter_rows() for cell in row if cell.data_type == "f")
assert formula_count == 82
assert wb.calculation.calcMode == "auto" and wb.calculation.fullCalcOnLoad and wb.calculation.forceFullCalc
wb.close()

result = {
    "status": "pass_independent_qa",
    "metrics": metrics,
    "concept_occurrences_by_verdict": {"核心": core_occurrences, "相关": related_occurrences, "合计": core_occurrences + related_occurrences},
    "formal_equals_preview": True,
    "backup_records": len(backup),
    "non_target_records_unchanged": len(formal) - len(target),
    "category_differences_loader": category_differences,
    "loader_counts": loader_counts,
    "venustech_original_core_verdict_preserved": True,
    "workbook_formula_count": formula_count,
    "workbook_formula_gate": "deferred_calcMode_auto; 82 formula caches unavailable; all reported values independently recomputed from JSON",
    "workbook_independent_stats": {f"{cat}/{con}": {"核心": counts[0], "相关": counts[1]} for (cat, con), counts in stats.items()},
}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
