# -*- coding: utf-8 -*-
"""K 线「Shift + 拖动 = 测幅度」（2026-11-02 owner）。

owner：「在K线图中增加一个功能，按shift键再点击鼠标，然后移动可以测量幅度是多少」。

守五条（都是**会静默坏掉**的点，所以钉死）：
  ① 只在**按住 Shift + 左键**、且点在图表区域里才起手 —— 否则会抢掉正常的平移/缩放；
  ② 捕获阶段拦 pointerdown 并 preventDefault + stopPropagation（早于 ECharts）；
  ③ 幅度口径 = (终点价 − 起点价) / 起点价 × 100%，并显示价差、跨多少根、起止时间；
  ④ 盘中每几秒整图重绘 ⇒ 测量框必须按**数据坐标**重新投影（只记像素会飘）；
  ⑤ 换票 / 换周期 / 关窗 / Esc 都要清掉覆盖层（覆盖层挂在 body 上，不清会一直留着）。

运行：python -m unittest tests.phase3.test_chart_measure_shift_drag -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
HTML = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")


def _seg(name, size=2600):
    i = JS.index(f"function {name}(")
    return JS[i:i + size]


class TestMeasureGuards(unittest.TestCase):
    def test_只在shift左键且图表内起手(self):
        seg = _seg("_msrOnDown")
        self.assertIn("ev.shiftKey", seg)
        self.assertIn("ev.button", seg, "只认左键")
        self.assertIn("host.contains(ev.target)", seg, "只认图表区域")
        self.assertIn("_paDrawMode", seg, "画框模式下不抢（画框优先）")

    def test_拦在ECharts之前(self):
        seg = _seg("_msrOnDown")
        self.assertIn("ev.preventDefault()", seg)
        self.assertIn("ev.stopPropagation()", seg)
        self.assertIn('document.addEventListener("pointerdown", _msrOnDown, true)', JS,
                      "必须捕获阶段")

    def test_误触单击不画框(self):
        self.assertIn("Math.abs(_msr.b.px.x - _msr.a.px.x) < 3", _seg("_msrOnUp"))


class TestMeasureMath(unittest.TestCase):
    def test_幅度口径(self):
        seg = _seg("_msrPaint", 4200)
        self.assertIn("const chg = bP - aP", seg)
        self.assertIn("chg / aP * 100", seg)
        self.assertIn("pct.toFixed(2)", seg)
        self.assertIn("价差", JS, "标签里要能看到价差")
        self.assertIn("根", seg, "要显示跨多少根")
        self.assertIn("#f85149", seg, "涨红")
        self.assertIn("#3fb950", seg, "跌绿")

    def test_像素换算复用画框那套(self):
        self.assertIn("_paPixToData(p.x, p.y)", _seg("_msrOnDown"))
        self.assertIn("_paPixToData(p.x, p.y)", _seg("_msrOnMove"))


class TestMeasureReprojection(unittest.TestCase):
    def test_整图重绘后重投影(self):
        i = JS.index("function renderStockChart")
        j = JS.index("if (_msr) {", i)          # 只要求出现在 renderStockChart 之后
        seg = JS[j:j + 400]
        self.assertIn("_msr.code !== stockChartCode", seg)
        self.assertIn("_msr.period !== stockChartPeriod", seg)
        self.assertIn("setTimeout(_msrRepaint, 0)", seg)

    def test_datazoom后重投影(self):
        i = JS.index('stockChartInst.on("dataZoom"')
        self.assertIn("setTimeout(_msrRepaint, 0)", JS[i:i + 1400])

    def test_换算失败时用grid矩形兜底(self):
        seg = _seg("_msrDataToPix", 2200)
        self.assertIn("convertToPixel", seg)
        self.assertIn("coordinateSystem.getRect()", seg)
        self.assertIn("axis.scale.getExtent()", seg)

    def test_兜底换算认dataZoom窗口(self):
        """默认视窗只显示最近 N 根，按全量日期线性映射会整体偏移。"""
        seg = _seg("_msrZoomIdx", 700)
        self.assertIn("stockChartAppliedZoom", seg)
        self.assertIn("(n - 1)", seg)
        # 反向（像素→索引，画框/测幅共用）也要用
        i = JS.index("function _paPixToData")
        self.assertIn("_msrZoomIdx(dates.length)", JS[i:i + 2600])


class TestMeasureCleanup(unittest.TestCase):
    def test_换票换周期关窗Esc都清(self):
        self.assertIn("_msrClear();", _seg("closeStockChart", 800))
        i = JS.index("async function openStockChart")
        self.assertIn("_msrClear();", JS[i:i + 900])
        self.assertIn("if (e.key === \"Escape\") _msrClear();", JS)
        # 换周期：renderStockChart 的守卫会在 period 不同时清掉
        self.assertIn("_msr.period !== stockChartPeriod", JS)

    def test_覆盖层挂body且不挡鼠标(self):
        seg = _seg("_msrEls", 1200)
        self.assertIn("document.body.appendChild(box)", seg)
        self.assertIn("pointer-events:none", seg)

    def test_工具栏有提示(self):
        self.assertIn("测幅度", HTML)
        self.assertIn("Shift", HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
