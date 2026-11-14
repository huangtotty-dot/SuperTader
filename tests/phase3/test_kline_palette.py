# -*- coding: utf-8 -*-
"""K线配色对齐参考图（2026-11-14，owner：K线形态/比例/颜色按参考图调）。

深蓝黑底 + 高饱和红涨/深绿跌（仍是 A 股惯例）。这是**纯视觉**改动，无纯函数，
只做静态钉色测试，防止以后被无意识地改回旧 GitHub 配色。

口径（钉住）：
  · 蜡烛体：涨 #ef3b3a、跌 #168d4f（旧 #f85149 / #3fb950 不再作为蜡烛体色）
  · 图表底色：#000020（三处图表 + 两处九宫格空图 init + dayModal 容器）
"""
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


class TestKlinePalette(unittest.TestCase):
    def test_三处蜡烛都用新配色(self):
        # 主图 / dayModal 分钟图 / 九宫格 三处 candlestick 的 itemStyle
        self.assertEqual(_JS.count('color: "#ef3b3a", color0: "#168d4f"'), 3)

    def test_旧蜡烛体色已清除(self):
        self.assertNotIn('color: "#f85149", color0:', _JS)
        self.assertNotIn('color0: "#3fb950"', _JS)

    def test_图表底色改深蓝黑(self):
        self.assertIn('backgroundColor: "#000020"', _JS)
        # 5 处 option 级 backgroundColor + dayModal 容器 div
        self.assertGreaterEqual(_JS.count('backgroundColor: "#000020"'), 5)
        self.assertIn('background:#000020', _JS)  # dayModal 容器 div

    def test_涨红跌绿_边框仍在(self):
        self.assertIn('borderColor: "#ff6b62", borderColor0: "#3fbf72"', _JS)

    def test_蜡烛加粗(self):
        # 主图 / dayModal 分钟图 / 九宫格 三处蜡烛更粗 + 缩小类目间隙
        self.assertIn('barMaxWidth: 18, barCategoryGap: "8%"', _JS)   # 主图
        self.assertIn('barMaxWidth: 14, barCategoryGap: "8%"', _JS)   # dayModal
        self.assertIn('barMaxWidth: 12, barCategoryGap: "10%"', _JS)  # 九宫格

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
