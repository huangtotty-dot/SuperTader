# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
source = (ROOT / "validate_optical_comm_cleaning.py").read_text(encoding="utf-8")
text = source.replace("optical_comm", "semiconductor").replace("光通信", "半导体")
text = text.replace("== 45", "== 161")
text = text.replace("[45, 42, 30, 12, 42, 3, 19, 98]", "[161, 153, 111, 42, 153, 8, 43, 229]")
text = text.replace('removed_expected = {"002199", "600345", "603330"}', 'removed_expected = {"002881", "300293", "300457", "300638", "301226", "600619", "603236", "688503"}')
start = text.index('for code in ("600487", "601869", "300570"):')
end = text.index("\nloader = WatchlistLoader()", start)
review_block = '''for code in ("002338", "301486", "603260"):
    row = next(x for x in audit if x["code"] == code)
    assert row["verdict"] == "相关" and row["action"] == "纠错"
    assert row.get("borderline_review") == "2026-10-27二次复核修正"
assert next(x for x in audit if x["code"] == "002338")["pairs"][0]["concepts"] == ["半导体股权与产业投资"]
assert next(x for x in audit if x["code"] == "301486")["pairs"][0]["concepts"] == ["半导体设备-CMP"]
assert next(x for x in audit if x["code"] == "603260")["pairs"][0]["concepts"] == ["半导体材料-其他关键材料"]
assert review["summary"]["field_change_count"] == 29
'''
text = text[:start] + review_block + text[end:]
text = text.replace('category_differences["半导体"] == [45, 42]', 'category_differences["半导体"] == [161, 153]')
text = text.replace('for category in ["商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:', 'for category in ["光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:')
text = text.replace('assert after_counts["商业航天"] == 74', 'assert after_counts["光通信"] == 42 and after_counts["商业航天"] == 74')
text = text.replace('== "半导体", "代码"]) == 42', '== "半导体", "代码"]) == 153')
text = text.replace('["半导体", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]', '["半导体", "光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]')
text = text.replace('max_row == 49', 'max_row == 165').replace('max_row == 46', 'max_row == 157').replace('max_row == 7', 'max_row == 12')
text = text.replace('max_row == 99', 'max_row == 230').replace('max_row == 24', 'max_row == 48')
text = text.replace('max_row=23', 'max_row=47').replace('len(stats) == 19', 'len(stats) == 43')
text = text.replace('formula_count == 66', 'formula_count == 138').replace('66 formula caches unavailable', '138 formula caches unavailable')
text = text.replace('"borderline_second_review": {"verdict_changes": 0, "numeric_corrections": ["600487", "601869", "300570"]}', '"borderline_second_review": {"verdict_changes": {"002338": "摘除→相关", "301486": "摘除→相关", "603260": "摘除→相关"}, "field_change_count": 29}')
out = ROOT / "validate_semiconductor_cleaning.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
