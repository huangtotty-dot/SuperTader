# -*- coding: utf-8 -*-
"""建仓信号扫描：5 日线状态着色（2026-10-19 owner）。

规则：**站上 5 日均线=绿色、其它=红色**；与得分里 −70 悬崖同一口径。
本测试只做源码契约（前端渲染），不启动 GUI：
  · `ma5FlagCellHtml` 存在，且 true/false 分别用绿/红；
  · 建仓表行用 `above_ma5` 渲染该列，**不再**用 `above_ma20`；
  · 整行底色按 `above_ma5`，无数据不着色；
  · 表头是「5日线」。

运行：python -m unittest tests.phase3.test_gui_build_scan_ma5_flag -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


class TestBuildScanMa5Flag(unittest.TestCase):
    def test_renderer_exists_with_green_red(self):
        self.assertIn("function ma5FlagCellHtml(", _JS)
        self.assertIn('color:#3fb950">站上', _JS)
        self.assertIn('color:#f85149">破位', _JS)

    def test_build_scan_row_uses_above_ma5(self):
        self.assertIn("${ma5FlagCellHtml(_tf.above_ma5, _tf.above_ma20)}", _JS)
        self.assertNotIn(
            "${ma20FlagCellHtml((r.timing_features || {}).above_ma20)}", _JS,
            "建仓表该列已改为 5 日线，不应再渲染 20 日线")

    def test_row_background_is_ma5_based(self):
        self.assertIn("const _above5 = _tf.above_ma5;", _JS)
        self.assertIn('(_above5 === true) ? "background:rgba(63,185,80,.10);"', _JS)
        self.assertIn('(_above5 === false) ? "background:rgba(248,81,73,.12);"', _JS)

    def test_header_is_5day(self):
        self.assertIn(">5日线<br>", _JS)
        self.assertNotIn(
            '>20日线<br><span style="font-size:9px;opacity:.6">观察</span></th>\n'
            '          <th class="num" title="实时价 + 实时涨幅（随行情每 10s 刷新）。取不到显示 —',
            _JS, "建仓表表头应已改为 5日线")


if __name__ == "__main__":
    unittest.main()
