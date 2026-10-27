# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
source = (ROOT / "validate_commercial_space_cleaning.py").read_text(encoding="utf-8")
text = source.replace("commercial_space", "optical_comm").replace("商业航天", "光通信")
text = text.replace("== 90", "== 45")
text = text.replace("[90, 74, 14, 60, 74, 16, 23, 111]", "[45, 42, 30, 12, 42, 3, 19, 98]")
text = text.replace('removed_expected = {"001379", "002088", "002131", "002245", "002796", "002985", "300118", "300316", "300604", "300751", "300757", "301071", "600456", "600855", "688516", "688610"}', 'removed_expected = {"002199", "600345", "603330"}')
start = text.index('for code, verdict in [("600879", "相关"), ("603809", "相关")]:')
end = text.index("\nloader = WatchlistLoader()", start)
review_block = '''for code in ("600487", "601869", "300570"):
    row = next(x for x in audit if x["code"] == code)
    assert row.get("borderline_review") == "2026-10-27二次复核数字修正"
assert "668.5485亿元" in next(x for x in audit if x["code"] == "600487")["reason"]
assert "31.4386亿元" in next(x for x in audit if x["code"] == "601869")["reason"]
assert "15.4676亿元" in next(x for x in audit if x["code"] == "300570")["reason"]
assert review["summary"]["verdict_changes"] == 0
assert review["summary"]["reason_replacements"] == 3
'''
text = text[:start] + review_block + text[end:]
text = text.replace('category_differences["光通信"] == [90, 74]', 'category_differences["光通信"] == [45, 42]')
text = text.replace('for category in ["军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:', 'for category in ["商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:')
text = text.replace('assert after_counts["军工"] == 89', 'assert after_counts["商业航天"] == 74 and after_counts["军工"] == 89')
text = text.replace('== "光通信", "代码"]) == 74', '== "光通信", "代码"]) == 42')
text = text.replace('["光通信", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]', '["光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]')
text = text.replace('max_row == 94', 'max_row == 49').replace('max_row == 78', 'max_row == 46').replace('max_row == 20', 'max_row == 7')
text = text.replace('max_row == 112', 'max_row == 99').replace('max_row == 28', 'max_row == 24')
text = text.replace('max_row=27', 'max_row=23').replace('len(stats) == 23', 'len(stats) == 19')
text = text.replace('formula_count == 78', 'formula_count == 66').replace('78 formula caches unavailable', '66 formula caches unavailable')
text = text.replace('"borderline_second_review": {"600879": "核心→相关", "603809": "摘除→相关"}', '"borderline_second_review": {"verdict_changes": 0, "numeric_corrections": ["600487", "601869", "300570"]}')
out = ROOT / "validate_optical_comm_cleaning.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
