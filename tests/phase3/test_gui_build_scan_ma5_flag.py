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
        # 2026-11-08 owner：猎手表的 20 日线列（maStateCellHtml）已随整列删除，
        # 建仓表仍用 ma20FlagCellHtml（站上=绿、破位=红）。
        self.assertIn("function ma20FlagCellHtml(", _JS)
        self.assertIn('>站上</span>', _JS)
        self.assertIn('<b class="down">破位</b>', _JS)
        self.assertNotIn("function maStateCellHtml(", _JS, "猎手专用渲染器应已删除")

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


class TestHunterRemovedColumns(unittest.TestCase):
    """2026-11-08 owner：「选股猎手中我画出来的这些列…取消并且删除相关的代码」。

    从猎手个股表移除 5 列：**得分 / 20日线 / 30m MA5 / 均线突破 / 时机条件**
    （其它表 —— 建仓扫描、风向标、监控池 —— 仍保留各自的同名列）。
    行底色仍按 5 日线（与建仓表同一套），只是不再有 20 日线这一列。
    """

    def _hunter_seg(self):
        i = _JS.index("function renderHunter(h)")
        nxt = _JS.index("\nfunction ", i + 10)
        return _JS[i:nxt]

    def test_hunter_columns_removed(self):
        seg = self._hunter_seg()
        for bad in ('>得分</th>', '>20日线<br>', '>30m MA5<br>',
                    '>均线突破<br>', '>时机条件</th>'):
            self.assertNotIn(bad, seg, f"猎手表不应再有这一列：{bad}")
        # 只查**调用**（注释里还提到这些名字，不能按字符串一刀切）
        self.assertNotIn("${maStateCellHtml(", seg)
        self.assertNotIn("${condCellHtml(", seg)
        self.assertNotIn('class="h-mr"', seg)
        self.assertNotIn("${maReclaimInnerHtml(", seg)

    def test_hunter_row_bg_still_ma5(self):
        seg = self._hunter_seg()
        self.assertIn("const _ab5 = s.build_above_ma5;", seg)
        self.assertIn("background:rgba(248,81,73,.12);", seg)

    def test_其它表仍保留这些列(self):
        # 建仓扫描 / 监控池 / 风向标 仍用共享渲染器
        self.assertIn("function condCellHtml(", _JS)
        self.assertIn("function maReclaimInnerHtml(", _JS)
        self.assertIn("function ma5CellHtml(", _JS)


if __name__ == "__main__":
    unittest.main()
