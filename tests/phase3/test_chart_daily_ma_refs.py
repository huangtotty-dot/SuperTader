# -*- coding: utf-8 -*-
"""非日线周期上的「日线 MA 水平参考线」（2026-10-22 owner：其它周期也画 30/60 日均线）。

⚠️ 两条 MA 别混淆：
  · `maSeries`（每周期自己的均线）= 该周期 30/60 根 K 线的均线，一直都有；
  · 本文件说的「日线 MAx 参考线」= 取**日线序列**的 MA 末值画一条水平虚线（30/60 个交易日），
    只在 `stockChartPeriod !== "daily"` 时出现 —— 与已有的 日线MA5/10/20 同口径。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _block(name):
    m = re.search(rf"const {name} = .*?\}} : null;", _JS, re.S)
    assert m, f"找不到 {name}"
    return m.group(0)


class TestDailyMa30And60:
    def test_both_lines_defined(self):
        for name, idx in (("dailyMa30Line", 3), ("dailyMa60Line", 4)):
            blk = _block(name)
            assert "stockChartPeriod !== \"daily\"" in blk, f"{name} 只在非日线周期显示"
            assert "type: \"dashed\"" in blk
            # 取值那行在对象字面量之前（_dma30 = _lastMa(3)），单独查
            assert f"const _dma{30 if idx == 3 else 60} = _lastMa({idx});" in _JS, \
                f"{name} 取的应是**日线** ma[{idx}]"

    def test_labels_are_offsets(self):
        """四条水平线共用左右两端标签位 ⇒ 必须用 distance 错开，否则价位接近就叠字。"""
        assert "日线MA30" in _block("dailyMa30Line")
        assert "日线MA60" in _block("dailyMa60Line")
        assert "distance: 17" in _block("dailyMa30Line")
        assert "distance: 34" in _block("dailyMa60Line")

    def test_wired_into_markline(self):
        assert "...(dailyMa30Line ? [dailyMa30Line] : [])" in _JS
        assert "...(dailyMa60Line ? [dailyMa60Line] : [])" in _JS

    def test_last_ma_helper_bounds(self):
        """`_lastMa` 对空数组/缺失索引要返回 null（月线只有 40 根 ⇒ MA60 全 null）。"""
        assert "const _lastMa = (i) => {" in _JS
        assert "return (a && a.length) ? a[a.length - 1] : null;" in _JS
