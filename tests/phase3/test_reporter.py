# -*- coding: utf-8 -*-
"""报告组装纯函数单测 —— stock_hunter/modules/reporter.py（2026-11-13 补）。

只测两个可离线确定的纯函数：
① `_fmt_stat`：None 显示为 '-'（Excel 里别把空当 0）；
② `ExcelReporter._sanitize_sheet_title`：openpyxl Sheet 标题非法字符
   `[ ] : * ? / \\` 替换为 '-'，空标题回退 "Sheet"，并截断到 31 字符上限。
   后者是真实 Bug 面——Sheet 名一旦含非法字符，整份 Excel 报告写入即抛异常。

运行：python tests/phase3/test_reporter.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stock_hunter.modules.reporter import ExcelReporter, _fmt_stat  # noqa: E402


class TestFmtStat(unittest.TestCase):
    def test_none_is_dash(self):
        self.assertEqual(_fmt_stat(None), "-")

    def test_value_passthrough(self):
        self.assertEqual(_fmt_stat(3.14), 3.14)
        self.assertEqual(_fmt_stat("abc"), "abc")


class TestSanitizeSheetTitle(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(
            ExcelReporter._sanitize_sheet_title("a[b]:c*d?e/f\\g"),
            "a-b--c-d-e-f-g",
        )

    def test_empty_title_falls_back_sheet(self):
        self.assertEqual(ExcelReporter._sanitize_sheet_title(""), "Sheet")
        self.assertEqual(ExcelReporter._sanitize_sheet_title(None), "Sheet")

    def test_truncated_to_31_chars(self):
        self.assertEqual(len(ExcelReporter._sanitize_sheet_title("x" * 40)), 31)

    def test_clean_title_unchanged(self):
        self.assertEqual(ExcelReporter._sanitize_sheet_title("半导体"), "半导体")


if __name__ == "__main__":
    unittest.main(verbosity=2)
