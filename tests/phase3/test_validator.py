# -*- coding: utf-8 -*-
"""报告交叉验证单测 —— stock_hunter/modules/validator.py（2026-11-13 补，内存 Workbook 真测）。

五个验证规则各自独立、可离线跑（传入 openpyxl Workbook，不 load 真实文件）：
① TOP5 恰好 5 条数据行（扣除 2 行表头）；② TOP5 代码/名称可解析（6 或 5 位纯数字）；
③ TOP5 覆盖 ≥3 个板块；④ 表头颜色白名单；⑤ 分数一致性（现为简化版恒过）。
ReportValidator 入口（load_workbook + load_config）不在此测——它只是把上面五个规则跑一遍。

运行：python tests/phase3/test_validator.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import PatternFill  # noqa: E402

from stock_hunter.modules.validator import (  # noqa: E402
    HeaderColorValidator, ScoreConsistencyValidator, SectorDiversityValidator,
    Top5CountValidator, Top5ParseableValidator,
)


def _top5_wb(data_rows):
    """构造带「3-TOP5」Sheet 的工作簿：第1行标题、第2行表头、数据从第3行起。
    data_rows: [(code, name, sector), ...]，占列 2/3/4。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "3-TOP5"
    ws.cell(1, 1, "TOP5 榜单")
    ws.cell(2, 2, "代码")
    ws.cell(2, 3, "名称")
    ws.cell(2, 4, "板块")
    for i, (code, name, sector) in enumerate(data_rows):
        r = 3 + i
        ws.cell(r, 2, code)
        ws.cell(r, 3, name)
        ws.cell(r, 4, sector)
    return wb


class TestTop5CountValidator(unittest.TestCase):
    def test_exactly_five(self):
        ok, msg = Top5CountValidator().validate(_top5_wb([("1", "a", "X")] * 5))
        self.assertTrue(ok)

    def test_not_five(self):
        ok, _ = Top5CountValidator().validate(_top5_wb([("1", "a", "X")] * 4))
        self.assertFalse(ok)

    def test_no_top5_sheet(self):
        ok, _ = Top5CountValidator().validate(Workbook())
        self.assertFalse(ok)


class TestTop5ParseableValidator(unittest.TestCase):
    def test_valid_codes(self):
        rows = [("600000", "贵州茅台", "白酒"), ("300750", "宁德时代", "新能源"),
                ("688981", "中芯国际", "半导体"), ("830799", "X", "Y"), ("920000", "Z", "W")]
        ok, _ = Top5ParseableValidator().validate(_top5_wb(rows))
        self.assertTrue(ok)

    def test_empty_name_fails(self):
        ok, _ = Top5ParseableValidator().validate(
            _top5_wb([("600000", None, "X")] + [("1", "a", "Y")] * 4))
        self.assertFalse(ok)

    def test_non_digit_code_fails(self):
        ok, _ = Top5ParseableValidator().validate(
            _top5_wb([("ABC", "X", "Y")] + [("1", "a", "Z")] * 4))
        self.assertFalse(ok)


class TestSectorDiversityValidator(unittest.TestCase):
    def test_three_sectors_ok(self):
        rows = [("1", "a", "X"), ("2", "b", "Y"), ("3", "c", "Z"), ("4", "d", "X"), ("5", "e", "Y")]
        ok, _ = SectorDiversityValidator().validate(_top5_wb(rows))
        self.assertTrue(ok)

    def test_two_sectors_fails(self):
        rows = [("1", "a", "X"), ("2", "b", "Y"), ("3", "c", "X"), ("4", "d", "Y"), ("5", "e", "X")]
        ok, _ = SectorDiversityValidator().validate(_top5_wb(rows))
        self.assertFalse(ok)


class TestHeaderColorValidator(unittest.TestCase):
    def _wb_with_fill(self, color):
        wb = Workbook()
        ws = wb.active
        ws.cell(1, 1, "标题")
        ws.cell(1, 1).fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        return wb

    def test_allowed_color_passes(self):
        ok, _ = HeaderColorValidator().validate(self._wb_with_fill("4472C4"))
        self.assertTrue(ok)

    def test_unlisted_color_fails(self):
        ok, _ = HeaderColorValidator().validate(self._wb_with_fill("FF0000"))
        self.assertFalse(ok)


class TestScoreConsistencyValidator(unittest.TestCase):
    def test_simplified_always_passes(self):
        ok, _ = ScoreConsistencyValidator().validate(Workbook())
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
