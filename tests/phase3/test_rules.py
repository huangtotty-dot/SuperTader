# -*- coding: utf-8 -*-
"""轻量交易规则单测 —— core/rules.py（2026-11-13 补）。

`limit_pct` 是猎手涨停旗标的**近似阈值**（非交易所精确涨停价）。四档口径：
ST 4.5% / 创业板·科创板 19.5% / 北交所 29.5% / 主板 9.5%。钉死：
① ST 优先级最高；② 板块前缀分流（30/68 → 19.5，8/9 → 29.5）；③ `_` 后缀代码剥离。

运行：python tests/phase3/test_rules.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.rules import limit_pct  # noqa: E402


class TestLimitPct(unittest.TestCase):
    def test_main_board(self):
        self.assertEqual(limit_pct("600000"), 9.5)
        self.assertEqual(limit_pct("000001"), 9.5)

    def test_chinext(self):
        self.assertEqual(limit_pct("300750"), 19.5)

    def test_star_market(self):
        self.assertEqual(limit_pct("688981"), 19.5)

    def test_bse(self):
        self.assertEqual(limit_pct("830799"), 29.5)
        self.assertEqual(limit_pct("920000"), 29.5)

    def test_st_takes_priority(self):
        """ST 优先于板块前缀：ST 创业/主板都按 4.5。"""
        self.assertEqual(limit_pct("300750", is_st=True), 4.5)
        self.assertEqual(limit_pct("600000", is_st=True), 4.5)

    def test_suffix_stripped(self):
        """代码带 `_` 后缀（如 `300750_ST`）时按前缀段判。"""
        self.assertEqual(limit_pct("300750_ST"), 19.5)

    def test_empty_and_none_default_main_board(self):
        self.assertEqual(limit_pct(""), 9.5)
        self.assertEqual(limit_pct(None), 9.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
