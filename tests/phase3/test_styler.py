# -*- coding: utf-8 -*-
"""Excel 样式单测 —— stock_hunter/modules/styler.py（2026-11-13 补，内存 Workbook 真测）。

openpyxl 样式是纯本地对象，不 mock 网络即可真测。钉死：
① V8Style 四种单元格类型的填充/字体/对齐契约（表头深蓝白字、分隔浅绿、数据默认黑）；
② 自定义 header_bg/header_font 覆盖默认色；③ auto_width 的 CJK 双宽度计数（CJK 按 2 宽，
   否则中文列宽会挤爆）+ min/max 封顶；④ ExcelStyler 默认 col_end 用 ws.max_column。

运行：python tests/phase3/test_styler.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openpyxl import Workbook  # noqa: E402

from stock_hunter.modules.styler import ExcelStyler, V8Style  # noqa: E402


def _rgb(cell):
    c = cell.fill.fgColor.rgb
    return c[2:] if c and len(c) == 8 else c


def _font_rgb(cell):
    c = cell.font.color.rgb
    return c[2:] if c and len(c) == 8 else c


class TestV8StyleApply(unittest.TestCase):
    def setUp(self):
        self.ws = Workbook().active
        self.s = V8Style()

    def test_header(self):
        self.s.apply(self.ws, 1, 1, "header")
        c = self.ws.cell(1, 1)
        self.assertEqual(_rgb(c), "4472C4")
        self.assertEqual(_font_rgb(c), "FFFFFF")
        self.assertTrue(c.font.bold)
        self.assertEqual(c.alignment.horizontal, "center")

    def test_title(self):
        self.s.apply(self.ws, 1, 1, "title")
        c = self.ws.cell(1, 1)
        self.assertEqual(_rgb(c), "4472C4")
        self.assertTrue(c.font.bold)

    def test_separator(self):
        self.s.apply(self.ws, 1, 1, "separator")
        c = self.ws.cell(1, 1)
        self.assertEqual(_rgb(c), "E2EFDA")
        self.assertEqual(_font_rgb(c), "000000")
        self.assertTrue(c.font.bold)

    def test_data(self):
        self.s.apply(self.ws, 1, 1, "data")
        c = self.ws.cell(1, 1)
        self.assertEqual(_font_rgb(c), "000000")
        self.assertEqual(c.font.size, 9)
        self.assertFalse(c.font.bold)

    def test_border_always_applied(self):
        self.s.apply(self.ws, 1, 1, "data")
        self.assertEqual(self.ws.cell(1, 1).border.left.style, "thin")

    def test_custom_colors_override(self):
        s = V8Style(header_bg="123456", header_font="ABCDEF")
        s.apply(self.ws, 1, 1, "header")
        c = self.ws.cell(1, 1)
        self.assertEqual(_rgb(c), "123456")
        self.assertEqual(_font_rgb(c), "ABCDEF")


class TestApplyRow(unittest.TestCase):
    def test_applies_full_range(self):
        ws = Workbook().active
        V8Style().apply_row(ws, 1, 2, 4, "header")
        for col in (2, 3, 4):
            self.assertEqual(_rgb(ws.cell(1, col)), "4472C4")
        # 范围外不碰
        self.assertNotEqual(_rgb(ws.cell(1, 1)), "4472C4")


class TestAutoWidth(unittest.TestCase):
    def _ws(self, a1):
        ws = Workbook().active
        ws["A1"] = a1
        return ws

    def test_cjk_counted_double(self):
        ws = self._ws("半导体")  # 3 个 CJK = 6 宽 < min_width 8 → 8+2=10
        V8Style().auto_width(ws)
        self.assertEqual(ws.column_dimensions["A"].width, 10)

    def test_ascii_width(self):
        ws = self._ws("abcdefghijklmnopqrst")  # 20 字符 → 20+2=22
        V8Style().auto_width(ws)
        self.assertEqual(ws.column_dimensions["A"].width, 22)

    def test_capped_at_max_width(self):
        ws = self._ws("半导体" * 30)  # 60 CJK = 120 宽 → 封顶 max_width 50
        V8Style().auto_width(ws)
        self.assertEqual(ws.column_dimensions["A"].width, 50)

    def test_custom_bounds(self):
        ws = self._ws("半")
        V8Style().auto_width(ws, min_width=20, max_width=30)  # cell_len 2 < 20 → 20+2=22
        self.assertEqual(ws.column_dimensions["A"].width, 22)


class TestExcelStyler(unittest.TestCase):
    def test_default_style_is_v8(self):
        self.assertIsInstance(ExcelStyler().style, V8Style)

    def test_default_col_end_uses_max_column(self):
        ws = Workbook().active
        ws["A1"] = "a"
        ws["B1"] = "b"
        ws["C1"] = "c"  # max_column=3
        ExcelStyler().apply_header_style(ws, 1)
        for col in (1, 2, 3):
            self.assertEqual(_rgb(ws.cell(1, col)), "4472C4")

    def test_merge_and_title(self):
        ws = Workbook().active
        ExcelStyler().merge_and_title(ws, 1, 1, 3, "概念总排名")
        self.assertEqual(ws.cell(1, 1).value, "概念总排名")
        self.assertEqual(_rgb(ws.cell(1, 1)), "4472C4")
        # 合并区域 A1:C1
        self.assertIn("A1:C1", [str(r) for r in ws.merged_cells.ranges])


if __name__ == "__main__":
    unittest.main(verbosity=2)
