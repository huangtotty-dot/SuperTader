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


class TestNowPriceTagOutsideGrid(unittest.TestCase):
    """2026-10-20 owner：「价格显示的位置会遮挡 K 线的更新」。

    原来现价胶囊是 markLine 的 `insideEndTop` 标签 —— 画在网格**内部右端**，正压最新那几根
    K 线；而盘中 0.5s 刷一次 ⇒ 一直挡着最该看的地方。改成画在网格右侧留白里。
    任何一条丢了，胶囊就会重新压回数据区：
      · 网格右侧留白（right 58）· markLine 标签关掉 · graphic 标签 + convertToPixel 定位
      · dataZoom / 最大化 后要重新定位（否则停在旧像素高度上）
    """

    def test_grid_reserves_right_margin(self):
        self.assertIn("left: 60, right: 58, top: 50", _JS)

    def test_markline_label_is_off(self):
        # 现价那条线只留虚线，标签交给 graphic
        self.assertIn("label: { show: false } };", _JS)

    def test_graphic_tag_uses_convert_to_pixel(self):
        self.assertIn("function _updateNowTag(inst, price, color)", _JS)
        self.assertIn('id: "nowTag"', _JS)
        self.assertIn("inst.convertToPixel({ yAxisIndex: 0 }, _p)", _JS)
        # 主 setOption 是 lazyUpdate ⇒ 必须等一帧再定位
        self.assertIn("function _scheduleNowTag(inst, price, color)", _JS)
        self.assertIn("requestAnimationFrame(_run)", _JS)

    def test_tag_repositioned_after_zoom_and_resize(self):
        # dataZoom：缩放改了 y 轴范围 ⇒ 像素位置要重算
        self.assertIn("_scheduleNowTag(stockChartInst, _nowTagState.price, _nowTagState.color)", _JS)
        # 最大化：resize 改了画布高度，同理
        self.assertIn("stockChartInst.resize();", _JS)


class TestChartAutoResize(unittest.TestCase):
    """2026-10-20 owner：「点最大化后图形没有自适应填满窗口」。

    根因：`.modal-lg` 的尺寸变化是 **CSS transition .25s**，而旧的实现只等 **60ms** 就
    `resize()` ⇒ ECharts 量到过渡中途的半大尺寸，之后没有任何事件纠正它。
    现在改为容器上挂 ResizeObserver（只认实际尺寸，跟过渡时长/触发原因无关）。
    """

    def test_modal_has_css_transition(self):
        # 前提没变：这条 transition 在，任何"固定延时 resize"都会量错
        self.assertIn("transition: all .25s ease", (_ROOT / "web" / "style.css").read_text(encoding="utf-8"))

    def test_resize_observer_attached_and_detached(self):
        self.assertIn("function _attachChartResize(el)", _JS)
        self.assertIn("new ResizeObserver(", _JS)
        self.assertIn("_chartRO.observe(el);", _JS)
        self.assertIn("_attachChartResize(el);", _JS)          # 在 init 分支里挂上
        self.assertIn("_chartRO.disconnect();", _JS)           # 关窗时断开，别随开关累积
        # rAF 节流：过渡期间逐帧 resize 跟得上动画
        self.assertIn("requestAnimationFrame(() => {", _JS)


class TestChartMA5Style(unittest.TestCase):
    """MA5 的特殊标记已于 2026-10-20 晚按 owner「纯属多余，去掉」**撤销**。

    MA5 这条均线本身保留（标准均线之一），恢复成与 MA10/20/30/60… 同款的 1px 细线；
    金线 / 微光 / z 抬升 / 线尾「MA5(日) xx」胶囊全部移除 —— 那个胶囊不仅多余，
    还挂在右端跟「现价胶囊」抢位置。
    """

    def test_ma5_special_marker_is_gone(self):
        self.assertNotIn("MA5_COLOR", _JS)
        self.assertNotIn("_isMA5", _JS)
        self.assertNotIn("shadowBlur: 6", _JS)
        # 只查 maSeries 那一段 —— 别的图（板块星轨等）本来就在用 endLabel
        _body = _JS.split("const maSeries = period.ma.map", 1)[1].split("});", 1)[0]
        self.assertNotIn("endLabel", _body)

    def test_ma5_still_rendered_as_normal_ma(self):
        # 均线本身还在（别把整条 MA5 也删了）
        self.assertIn("const maSeries = period.ma.map((maArr, i) => {", _JS)
        self.assertIn("const _color = MA_COLORS[i];", _JS)
        self.assertIn("lineStyle: { width: 1, color: _color },", _JS)

    def test_legend_color_matches_line(self):
        self.assertIn("itemStyle: { color: _color }", _JS)

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
        # 金线撤销后，口径说明改成直接讲 MA5 的根数含义（不再有"金线"前缀）
        self.assertIn("MA5(${_pcnHint})=5 根该周期K", _JS)
        self.assertNotIn("金线=MA5", _JS)

    def test_intraday_daily_ma10_reference_line(self):
        """2026-10-20 owner：「10分钟周期内也显示10日均线」——紫色虚线参考线。"""
        self.assertIn("const dailyMa10Line", _JS)
        self.assertIn("日线MA10 ${fmt(_dma10, 2)}", _JS)
        self.assertIn('color: "#bc8cff"', _JS)
        self.assertIn("...(dailyMa10Line ? [dailyMa10Line] : [])", _JS)
        self.assertIn("紫虚线=日线MA10", _JS)


if __name__ == "__main__":
    unittest.main()
