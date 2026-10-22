# -*- coding: utf-8 -*-
"""日线趋势线（2026-10-22 owner：「根据日线尝试将趋势线画出来，按钮控制显隐」）。

口径（见 `t_gui._attach_trendlines`）：
  · 摆动点 = 局部极值（左右各 3 根都不越过它）；
  · 上升趋势线 = 最近两个**依次抬高**的摆动低点连线；下降线 = 两个依次**降低**的摆动高点；
  · 两点至少隔 5 根；线从第一个摆动点**延伸到最新一根**（`y_now` 就是"现在这条线在哪"）；
  · `touches` = 线 ±1.5% 内的同类摆动点数（被反复验证的次数）。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _payload(lows, highs):
    """lows/highs → payload（daily 序列：ohlc = [open, close, low, high]）。"""
    n = len(lows)
    dates = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]
    ohlc = [[lows[i], (lows[i] + highs[i]) / 2, lows[i], highs[i]] for i in range(n)]
    return {"period_data": {"daily": {"dates": dates, "ohlc": ohlc}}}


def _zigzag(n=60, start=10.0, step=1.0):
    """构造一段**逐级抬高**的锯齿：低点 10,11,12…，高点 12,13,14…"""
    lows, highs = [], []
    for i in range(n):
        base = start + step * (i // 6)          # 每 6 根抬一级
        lows.append(base + (0.2 if i % 6 else 0.0))
        highs.append(base + 2.0)
    return lows, highs


class TestTrendLines:
    def test_uptrend_line_found(self):
        lows, highs = _zigzag()
        tl = t_gui._attach_trendlines(_payload(lows, highs))["trendlines"]["daily"]
        up = tl["up"]
        assert up is not None, "逐级抬高的低点必须画得出上升趋势线"
        assert up["y1"] > up["y0"], "上升趋势线的后点必须更高"
        assert up["i_now"] == len(lows) - 1, "线要延伸到最新一根"
        assert up["y_now"] > up["y1"], "抬高趋势下，当前线位应高于第二个摆动点"
        assert up["slope_pct"] > 0
        assert up["touches"] >= 2, "两个端点本身就该算进触点"

    def test_downtrend_line_found(self):
        lows = [20.0 - 0.2 * (i // 6) for i in range(60)]
        highs = [22.0 - 0.5 * (i // 6) for i in range(60)]
        tl = t_gui._attach_trendlines(_payload(lows, highs))["trendlines"]["daily"]
        dn = tl["dn"]
        assert dn is not None
        assert dn["y1"] < dn["y0"] and dn["slope_pct"] < 0
        assert dn["y_now"] < dn["y1"]

    def test_flat_series_safe(self):
        """一条直线：没有"依次抬高/降低"的摆动点 ⇒ 两条线都可以是 None，不能抛。"""
        lows = [10.0] * 60
        highs = [10.0] * 60
        tl = t_gui._attach_trendlines(_payload(lows, highs))["trendlines"]["daily"]
        assert tl["up"] is None and tl["dn"] is None

    def test_short_series_no_crash(self):
        tl = t_gui._attach_trendlines(_payload([1.0] * 5, [2.0] * 5))["trendlines"]["daily"]
        assert tl["up"] is None and tl["dn"] is None

    def test_min_gap_between_points(self):
        """两个摆动点挨得太近（<5 根）不能连线 —— 那只是同一根小反弹的两端。"""
        lows, highs = _zigzag()
        tl = t_gui._attach_trendlines(_payload(lows, highs))["trendlines"]["daily"]
        up = tl["up"]
        assert up["i1"] - up["i0"] >= 5


class TestPerPeriod:
    def test_backend_keys_by_period(self):
        lows, highs = _zigzag()
        p = _payload(lows, highs)
        p["period_data"]["weekly"] = p["period_data"]["daily"]
        p["period_data"]["min5"] = p["period_data"]["daily"]
        tl = t_gui._attach_trendlines(p)["trendlines"]
        assert tl["schema"] == 2
        assert set(p["period_data"]) <= set(tl), "每个有序列的周期都要有一份"
        assert tl["weekly"]["up"] is not None and tl["min5"]["up"] is not None

    def test_missing_periods_skipped(self):
        lows, highs = _zigzag()
        tl = t_gui._attach_trendlines(_payload(lows, highs))["trendlines"]
        assert "daily" in tl and "min30" not in tl, "没有序列的周期不塞空壳"

    def test_version_includes_trend_schema(self):
        """口径一改，版本号要变 —— 否则「unchanged 不刷新」握手会让前端永远拿旧 payload。"""
        src = (_ROOT / "t_gui.py").read_text(encoding="utf-8")
        assert "|tr{_TREND_SCHEMA}" in src


class TestFrontend:
    def test_toggle_exists_and_default_on(self):
        assert 'id="tgTrend"' in _HTML, "工具栏要有「趋势线」勾选框"
        i = _HTML.index('id="tgTrend"')
        assert "checked" in _HTML[i:i + 120], "默认勾选（打开就能看到线）"

    def test_per_period(self):
        """2026-10-22 二轮：**每个周期各算一份**，前端按当前 Tab 取（端点索引是各周期自己的下标）。"""
        assert "_tlAll[stockChartPeriod]" in _JS, "前端必须按当前周期取 trendlines"
        # 开关那段不能再带日线限定（别处仍可能有 daily 专属逻辑，只盯这一段）
        i = _JS.index("const showTrend =")
        seg = _JS[i:i + 200]
        assert 'stockChartPeriod === "daily"' not in seg, "趋势线不再限制只画日线"

    def test_series_wired(self):
        assert "...trendSeries," in _JS, "构造出来的 trendSeries 必须进 series 数组"
        assert "_pushTrend(_tl.up" in _JS and "_pushTrend(_tl.dn" in _JS
