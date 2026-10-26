# -*- coding: utf-8 -*-
"""建仓信号扫描：行底色 5 日线 / 列显示 20 日线（2026-10-19~20 owner）。

2026-10-19：建仓表整行底色按 5 日线——站上=绿、其它=红（与 −70 悬崖同口径）。
2026-10-20：owner 澄清「图中的列应该是是否站上 20 日线」⇒
  · **列**恢复为「20日线」观察列（`above_ma20`）；
  · **行底色**仍是 5 日线口径。
本测试只做源码契约，不启动 GUI。

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
        # 2026-10-20：渲染器泛化成「哪条均线由调用方传 n」⇒ ma5FlagCellHtml → maStateCellHtml
        self.assertIn("function maStateCellHtml(", _JS)
        self.assertIn('color:#3fb950">站上', _JS)
        # 建仓表默认红「破位」；猎手表通过参数改成黄「未站上」
        self.assertIn('const _bc = belowColor || "#f85149";', _JS)
        self.assertIn('const _bt = belowText || "破位";', _JS)

    def test_build_scan_column_is_ma20_row_color_is_ma5(self):
        """列 = 20 日线观察；行底色 = 5 日线（两者刻意分开）。"""
        self.assertIn("${ma20FlagCellHtml(_tf.above_ma20)}", _JS)
        self.assertIn("const _above5 = _tf.above_ma5;", _JS)

    def test_row_background_is_ma5_based(self):
        self.assertIn("const _above5 = _tf.above_ma5;", _JS)
        self.assertIn('(_above5 === true) ? "background:rgba(63,185,80,.10);"', _JS)
        self.assertIn('(_above5 === false) ? "background:rgba(248,81,73,.12);"', _JS)

    def test_build_scan_header_is_20day(self):
        self.assertIn(
            '>20日线<br><span style="font-size:9px;opacity:.6">观察</span></th>\n'
            '          <th class="num" title="实时价 + 实时涨幅（随行情每 10s 刷新）。取不到显示 —',
            _JS, "建仓表表头应为 20日线（行底色才是 5 日线口径）")


class TestHunterMa5Flag(unittest.TestCase):
    """选股猎手个股：站上=绿、其它=黄（`belowColor` 黄「未站上」）。

    2026-10-20 owner：「选股猎手图中的该列换成 20 日均线」⇒
      · **列**改为 20 日线状态（`build_above_ma20`），5 日线状态降级进 tooltip；
      · **行底色**仍是 5 日线口径（与建仓表同一套：列看 20 日线、底色看 5 日线）。
    """

    def test_hunter_row_uses_ma20_green_yellow(self):
        self.assertIn(
            '${maStateCellHtml(s.build_above_ma20, 20, s.build_above_ma5, 5, '
            'undefined, "#d29922", "未站上")}', _JS)
        # 2026-10-26 owner：「破5日线应该和建仓信号扫描一样显示红色」——行底色从
        # 琥珀 rgba(210,153,34,.14) 改成与 pb 表 `_scoreBg` / 监控池 `rowBg` 同值的红。
        # ⚠️ 上面那条 `"#d29922"` 是**20 日线列**的颜色（未站上 20 日线用琥珀提示），
        # 与行底色是两回事，别一起改。
        self.assertIn("background:rgba(248,81,73,.12);", _JS)
        # 行底色仍按 5 日线
        self.assertIn('const _ab5 = s.build_above_ma5;', _JS)

    def test_hunter_header_is_20day(self):
        self.assertIn('>20日线<br><span style="font-size:9px;opacity:.6">规则口径</span></th>', _JS)


if __name__ == "__main__":
    unittest.main()
