# -*- coding: utf-8 -*-
"""个股→板块指数映射单测 —— core/board_index.py（2026-11-13 补）。

A-1(2026-09-07) 把 analysis/index_resonance.py 里的 resolve_index 提升成公共函数，
多侧（买卖信号/共振/timing_gate）共用同一份前缀规则。要钉死三件事：
① 板块前缀路由（60→上证 / 68·588→科创50 / 30→创业板 / 00·001·002·003→深成）；
② `_A/_B` 后缀剥离后再判；③ 通达信 880003(平均股价) 映射只在 GUI_INDEX_BOARD 单点。
这些函数全部无 IO，直接单测。

运行：python tests/phase3/test_board_index.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.board_index import (  # noqa: E402
    GUI_INDEX_BOARD, gui_board, gui_board_by_symbol, index_gm_symbol,
    resolve_index, tdx_code_for,
)


class TestResolveIndex(unittest.TestCase):
    def test_shanghai_main(self):
        self.assertEqual(resolve_index("600000"), ("sh000001", "上证指数"))
        self.assertEqual(resolve_index("601318"), ("sh000001", "上证指数"))

    def test_star_market(self):
        self.assertEqual(resolve_index("688981"), ("sh000688", "科创50"))
        self.assertEqual(resolve_index("588000"), ("sh000688", "科创50"))

    def test_chinext(self):
        self.assertEqual(resolve_index("300750"), ("sz399006", "创业板指"))

    def test_shenzhen_main(self):
        for c in ("000001", "001979", "002451", "003816"):
            self.assertEqual(resolve_index(c), ("sz399001", "深证成指"), c)

    def test_suffix_stripped(self):
        """代码带 `_A`/`_B`（做T仓位标识）时按前缀段判，不能漏掉。"""
        self.assertEqual(resolve_index("300750_A"), ("sz399006", "创业板指"))
        self.assertEqual(resolve_index("600000_B"), ("sh000001", "上证指数"))

    def test_unknown_falls_back_shanghai(self):
        self.assertEqual(resolve_index("999999"), ("sh000001", "上证指数"))
        self.assertEqual(resolve_index(""), ("sh000001", "上证指数"))


class TestIndexGmSymbol(unittest.TestCase):
    def test_shanghai(self):
        self.assertEqual(index_gm_symbol("sh000001"), "SHSE.000001")
        self.assertEqual(index_gm_symbol("SH000905"), "SHSE.000905")

    def test_shenzhen(self):
        self.assertEqual(index_gm_symbol("sz399006"), "SZSE.399006")

    def test_already_gm_format_normalized(self):
        self.assertEqual(index_gm_symbol("SHSE.000001"), "SHSE.000001")
        self.assertEqual(index_gm_symbol("szse.399006"), "SZSE.399006")

    def test_unrecognized_unchanged(self):
        self.assertEqual(index_gm_symbol("880003"), "880003")


class TestGuiBoard(unittest.TestCase):
    def test_all_seven(self):
        self.assertEqual(len(gui_board()), 7)

    def test_filter_gm(self):
        gms = gui_board("gm")
        self.assertEqual(len(gms), 6)
        self.assertTrue(all(i["source"] == "gm" for i in gms))

    def test_filter_em(self):
        ems = gui_board("em")
        self.assertEqual(len(ems), 1)
        self.assertEqual(ems[0]["name"], "平均股价")

    def test_by_symbol(self):
        it = gui_board_by_symbol("sh000001")
        self.assertEqual(it["name"], "上证指数")
        self.assertIsNone(gui_board_by_symbol("no-such-symbol"))


class TestTdxCodeFor(unittest.TestCase):
    def test_avg_price_maps_880003(self):
        self.assertEqual(tdx_code_for("em47.800005"), "880003")

    def test_gm_index_has_no_tdx(self):
        self.assertIsNone(tdx_code_for("sh000001"))

    def test_unknown_returns_none(self):
        self.assertIsNone(tdx_code_for("xyz"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
