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


class TestChartDailyFullRange(unittest.TestCase):
    """日线默认视窗：2026-10-20 上午曾改成「全部」，当晚 owner 改口要**近6个月**。

    教训：这条默认值一天内被改了两遍，所以把"为什么是 120"钉进测试 ——
    120 == 后端黄金分割回看期（`t_gui._FIB_LOOKBACK["daily"]`），锚点必然在视窗内。
    """

    def test_default_daily_is_six_months(self):
        self.assertIn("const DEFAULT_BARS = { daily: 120,", _JS)
        # 「全部」仍必须可显式选择（null 与"未设"要能区分）
        self.assertIn('{ label: "全部", bars: null }', _JS)
        # 那条把用户存的 120 删掉的迁移已作废，别再回来
        self.assertNotIn("stockChartRange.daily === 120", _JS)

    def test_summary_shows_bar_count(self):
        self.assertIn("本周期 ${_pdCur.dates.length} 根", _JS)


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

    def test_ma5_text_label(self):
        """线尾文字标记（owner 2026-10-20：「用文字标记出5日均线」）。"""
        self.assertIn("endLabel: _isMA5 ? {", _JS)
        self.assertIn("const _periodCn =", _JS)
        self.assertIn("`MA5(${_periodCn})`", _JS)
        self.assertIn("backgroundColor: MA5_COLOR", _JS)

    def test_period_vars_declared_before_use(self):
        """防 TDZ：`_isIntradayPeriod` 必须先于 `dailyMa5Line` 声明
        （2026-10-20 实盘报错 Cannot access '_isIntradayPeriod' before initialization）。"""
        body = _JS.split("function renderStockChart(", 1)[1]
        body = body.split("\nfunction ", 1)[0]
        self.assertLess(body.index("const _isIntradayPeriod"),
                        body.index("const dailyMa5Line"),
                        "_isIntradayPeriod 必须声明在 dailyMa5Line 之前")

    def test_intraday_daily_ma5_reference_line(self):
        """分钟图额外画「日线MA5」蓝虚线，避免把 5 根 10min 的 MA5 误当 5 日线。"""
        self.assertIn("const _isIntradayPeriod", _JS)
        self.assertIn("const dailyMa5Line", _JS)
        self.assertIn("日线MA5 ${fmt(_dma5, 2)}", _JS)
        self.assertIn('color: "#58a6ff"', _JS)
        self.assertIn("...(dailyMa5Line ? [dailyMa5Line] : [])", _JS)
        self.assertIn("金线=MA5(${_pcnHint}) · 蓝虚线=日线MA5", _JS)

    def test_intraday_daily_ma10_reference_line(self):
        """2026-10-20 owner：「10分钟周期内也显示10日均线」——紫色虚线参考线。"""
        self.assertIn("const dailyMa10Line", _JS)
        self.assertIn("日线MA10 ${fmt(_dma10, 2)}", _JS)
        self.assertIn('color: "#bc8cff"', _JS)
        self.assertIn("...(dailyMa10Line ? [dailyMa10Line] : [])", _JS)
        self.assertIn("紫虚线=日线MA10", _JS)


if __name__ == "__main__":
    unittest.main()
