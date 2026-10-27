# -*- coding: utf-8 -*-
from pathlib import Path
ROOT = Path(r"E:\superTrader\data_cleaning")
text = (ROOT / "validate_semiconductor_cleaning.py").read_text(encoding="utf-8")
text = text.replace("semiconductor", "pcb").replace("半导体", "PCB")
text = text.replace("== 161", "== 89")
text = text.replace("[161, 153, 111, 42, 153, 8, 43, 229]", "[89, 76, 52, 24, 70, 13, 23, 144]")
text = text.replace('removed_expected = {"002881", "300293", "300457", "300638", "301226", "600619", "603236", "688503"}', 'removed_expected = {"000636", "000733", "001212", "001216", "002859", "300408", "301013", "301566", "301678", "603267", "603678", "605376", "688260"}')
text = text.replace('assert old_concepts.intersection(c for _, c in got) <= {"PCB设备-封装设备"}', 'assert not old_concepts.intersection(c for _, c in got)')
start = text.index('for code in ("002338", "301486", "603260"):')
end = text.index("\nloader = WatchlistLoader()", start)
review_block = '''row = next(x for x in audit if x["code"] == "301511")
assert row["name"] == "德福科技" and row["verdict"] == "核心"
assert row.get("borderline_review") == "2026-10-27二次复核修正"
assert review["summary"]["field_change_count"] == 17
assert review["summary"]["verdict_changes"] == {"301511": "相关→核心"}
assert Counter(x["action"] for x in audit) == {"纠错": 70, "保留": 6, "摘除": 13}
'''
text = text[:start] + review_block + text[end:]
start = text.index('assert category_differences["PCB"] == [169, 153]') if 'assert category_differences["PCB"] == [169, 153]' in text else text.index('assert category_differences["PCB"] == [161, 153]')
end = text.index("\n", start)
text = text[:start] + 'assert category_differences["PCB"][1] == 76 and category_differences["PCB"][0] >= 89' + text[end:]
text = text.replace('for category in ["光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:', 'for category in ["半导体", "光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]:')
text = text.replace('assert after_counts["光通信"] == 42', 'assert after_counts["半导体"] == 153 and after_counts["光通信"] == 42')
text = text.replace('== "PCB", "代码"]) == 153', '== "PCB", "代码"]) == 76')
text = text.replace('["PCB", "光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]', '["PCB", "半导体", "光通信", "商业航天", "军工", "云计算", "网络安全", "AI应用", "物理AI", "数据要素"]')
text = text.replace('max_row == 165', 'max_row == 93').replace('max_row == 157', 'max_row == 80').replace('max_row == 12', 'max_row == 17')
text = text.replace('max_row == 230', 'max_row == 145').replace('max_row == 48', 'max_row == 28')
text = text.replace('max_row=47', 'max_row=27').replace('len(stats) == 43', 'len(stats) == 23')
text = text.replace('formula_count == 138', 'formula_count == 78').replace('138 formula caches unavailable', '78 formula caches unavailable')
old = '"borderline_second_review": {"verdict_changes": {"002338": "摘除→相关", "301486": "摘除→相关", "603260": "摘除→相关"}, "field_change_count": 29}'
text = text.replace(old, '"borderline_second_review": {"verdict_changes": {"301511": "相关→核心"}, "field_change_count": 17}')
out = ROOT / "validate_pcb_cleaning.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
