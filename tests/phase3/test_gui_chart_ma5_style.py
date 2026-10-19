# -*- coding: utf-8 -*-
"""K 线图 MA5 特殊线型（2026-10-20 owner：「任意周期用特殊线型标记 5 日均线」）。

只做前端源码契约，不启动 GUI：
  · MA5 使用独立金色 `#ffd166`、2.6px 加粗 + 微光；
  · z 抬高（压在蜡烛/其它均线之上）；
  · itemStyle 与 lineStyle 同色（图例颜色不漂移）。

运行：python -m unittest tests.phase3.test_gui_chart_ma5_style -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


class TestChartMA5Style(unittest.TestCase):
    def test_ma5_special_color_and_width(self):
        self.assertIn('const MA5_COLOR = "#ffd166";', _JS)
        self.assertIn("const _isMA5 = i === 0;", _JS)
        self.assertIn("width: 2.6", _JS)
        self.assertIn("shadowBlur: 6", _JS)

    def test_ma5_above_other_series(self):
        self.assertIn("z: _isMA5 ? 5 : 2", _JS)

    def test_legend_color_matches_line(self):
        self.assertIn("const _color = _isMA5 ? MA5_COLOR : MA_COLORS[i];", _JS)
        self.assertIn("itemStyle: { color: _color }", _JS)


if __name__ == "__main__":
    unittest.main()
