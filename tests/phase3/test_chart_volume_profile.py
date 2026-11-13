# -*- coding: utf-8 -*-
"""K 线「密集成交区」（Volume Profile）实验（2026-11-02 owner）。

owner：「在K线图中计算密集成交区。给出一个按钮，如果点击后，可以显示密集成交区」。

口径（被本测试钉住）：
  · 每根 K 的量按 [low, high] 与价格分箱的**重叠比例**均摊，一字板全量给所在箱；
  · POC = 量最大的箱；密集区 = 连续 ≥1.5×平均箱量 的箱段（POC 必可见）；
  · 窗口默认最近 250 根；权重优先成交额，没有则成交量；
  · **按钮开关、默认关闭**；开/关都走 `renderStockChart` 重绘（关掉要能清掉色带）。

两条腿：
  ① 静态：按钮 / 开关函数 / markArea / POC 线都接上了；
  ② 行为：把 `buildVolumeProfile` 抽出来丢给 node 跑（没有 node 就跳过），
     用合成数据验证 POC 落在真实密集带、边界与份额合理。

运行：python -m unittest tests.phase3.test_chart_volume_profile -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _extract_fn(name: str) -> str:
    """从 app.js 里按花括号配平抽出某个顶层函数源码（无需加载整个 app.js）。"""
    i = JS.index(f"function {name}(")
    depth = 0
    for j in range(i, len(JS)):
        ch = JS[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return JS[i:j + 1]
    raise AssertionError(f"{name} 花括号不配平")


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestVolumeProfileBehavior(unittest.TestCase):
    def _run(self, ohlc, vol, lb=250, bins=60):
        fn = _extract_fn("buildVolumeProfile")
        js = (fn + "\nconst r = buildVolumeProfile("
              + json.dumps(ohlc) + "," + json.dumps(vol) + f",{lb},{bins});"
              + "\nprocess.stdout.write(JSON.stringify(r));")
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "vp.js"
            fp.write_text(js, encoding="utf-8")
            out = subprocess.check_output([NODE, str(fp)], timeout=30)
        return json.loads(out.decode("utf-8"))

    def test_密集带与POC落在真实密集区(self):
        # 60 根在 10~11（量摊得薄）+ 40 根挤在 15~15.2（同样每根 1，但密度 5 倍）
        ohlc, vol = [], []
        for _ in range(60):
            ohlc.append([10.5, 10.6, 10.0, 11.0])
            vol.append(1.0)
        for _ in range(40):
            ohlc.append([15.1, 15.15, 15.0, 15.2])
            vol.append(1.0)
        r = self._run(ohlc, vol)
        self.assertIsNotNone(r)
        self.assertTrue(14.9 <= r["poc"] <= 15.3, f"POC 应落在密集带，实际 {r['poc']}")
        self.assertTrue(any(z["poc"] for z in r["zones"]), "至少一个色带要标成 POC")
        for z in r["zones"]:
            self.assertLess(z["lo"], z["hi"])
            self.assertGreater(z["share"], 0)
        self.assertLessEqual(len(r["zones"]), 3)
        self.assertTrue(10.0 <= r["lo"] and r["hi"] <= 15.2)

    def test_一字板不炸且量给所在箱(self):
        ohlc, vol = [], []
        for _ in range(30):
            ohlc.append([10.0, 10.0, 10.0, 10.0])
            vol.append(2.0)
        for _ in range(30):
            ohlc.append([12.0, 12.0, 12.0, 12.0])
            vol.append(1.0)
        r = self._run(ohlc, vol)
        self.assertIsNotNone(r)
        self.assertTrue(10.0 <= r["poc"] <= 12.0)

    def test_数据不足返回null(self):
        self.assertIsNone(self._run([[10, 11, 9, 12]] * 10, [1.0] * 10))

    def test_零量返回null(self):
        self.assertIsNone(self._run([[10, 11, 9, 12]] * 30, [0.0] * 30))


class TestVolumeProfileWiring(unittest.TestCase):
    def test_按钮存在且默认关(self):
        self.assertIn('id="vpBtn"', HTML)
        self.assertIn('id="mwVpBtn"', HTML, "多窗口标题栏也要有开关")
        self.assertIn("onclick=\"toggleVolumeProfile()\"", HTML)
        # 2026-11-02 owner：「多窗口默认显示」⇒ 默认开，只有显式关过才为 false
        self.assertIn("let _vpOn = (() => {", JS)
        self.assertIn("return v === null ? true : v === \"1\";", JS)
        self.assertIn("function toggleVolumeProfile()", JS)
        self.assertIn("_vpOn ? buildVolumeProfile(period.ohlc, _voltSeries, 250, 60) : null", JS)

    def test_只在多窗口打开时也能切(self):
        i = JS.index("function toggleVolumeProfile")
        seg = JS[i:i + 1200]
        self.assertIn("_mwWindowOpen()", seg, "没有 K 线弹窗、只开多窗口时也要能开关")

    def test_右侧直方图而非常宽色带(self):
        """owner：「还是和缺口、画框冲突」⇒ 不再画满宽色带，改成网格右留白里的横向直方图。"""
        self.assertIn("function _vpSeriesOption(profile, marginPx, maxBarPx, xAxisIndex)", JS)
        seg = JS[JS.index("function _vpSeriesOption"):JS.index("function _vpSeriesOption") + 1800]
        self.assertIn('type: "custom"', seg)
        self.assertIn("clip: false", seg)
        self.assertIn("api.getWidth() - marginPx", seg)
        self.assertIn("rgba(190,160,255,.88)", seg)     # POC 最亮
        self.assertIn("rgba(163,113,247,.45)", seg)
        # 不再往 markArea 里塞密集区；改成独立序列
        self.assertNotIn("vpAreas", JS)
        self.assertIn("...(vpSeries ? [vpSeries] : [])", JS)

    def test_直方图不参与dataZoom过滤(self):
        """挂在主 x 轴上会被 dataZoom 的 filter 模式过滤掉（缩放到别处整条消失）。"""
        self.assertIn("_vpSeriesOption(_vp.profile, _vpRight, _vpRight - 62, 4)", JS)
        self.assertIn("_vpSeriesOption(_vp.profile, _mwVpRight, 42, 3)", JS)
        self.assertIn("密集区直方图专用的隐藏 x 轴", JS)
        self.assertIn("xAxisIndex: (xAxisIndex == null ? 0 : xAxisIndex)", JS)

    def test_右侧留白随开关加宽(self):
        self.assertIn("const _vpRight = _vp ? 112 : 58;", JS)
        self.assertIn("const _mwVpRight = _vp ? 52 : 6;", JS)
        self.assertIn("right: _vpRight, top: 50", JS)
        self.assertIn("left: 40, right: _mwVpRight", JS)

    def test_POC线(self):
        self.assertIn("formatter: `POC ${fmt(_vp.poc, 2)}`", JS)
        self.assertIn('color: "#c9a7ff"', JS)

    def test_多窗口每格也画(self):
        """owner：「在多窗口显示中也要显示密集成交区」。"""
        self.assertIn("_vpOn ? buildVolumeProfile(ohlc, vals, MW_TAIL, 48)", JS)
        self.assertIn("_vpSeries ? [_vpSeries] : []", JS)
        self.assertIn("_lines.concat(_vpLine ? [_vpLine] : [], _ma5Line ? [_ma5Line] : [])", JS)
        # 开关要立刻同步到已打开的多窗口
        i = JS.index("function toggleVolumeProfile")
        self.assertIn("_mwKickTick()", JS[i:i + 1200])

    def test_开关跨窗口同步(self):
        """owner 2026-11-02 实报：主窗口开了密集区，多窗口（独立原生窗口）里没有。

        多窗口通常是**独立原生窗口 = 另一个 JS 上下文** ⇒ 光靠内存变量传不过去。守三条：
          ① 状态落 localStorage，刷新/同源窗口都认；
          ② 开窗时把状态随参数传给 `mwEnterWindowMode(codes, opts)`；
          ③ 后端 `open_multi_window(codes, opts)` 原样透传（evaluate_js）。
        """
        self.assertIn('const VP_LS_KEY = "mwVolumeProfileOn2";', JS)
        self.assertIn("localStorage.getItem(VP_LS_KEY)", JS)
        self.assertIn("localStorage.setItem(VP_LS_KEY", JS)
        self.assertIn("mwEnterWindowMode(codes, opts)", JS)
        self.assertIn("typeof opts.vp === \"boolean\"", JS)
        self.assertIn('apiCall("open_multi_window", _codes, { vp: !!_vpOn })', JS)
        tg = (Path(_ROOT) / "gui.py").read_text(encoding="utf-8")
        self.assertIn("def open_multi_window(self, codes=None, opts=None)", tg)
        self.assertIn("mwEnterWindowMode(%s, %s)", tg)

    def test_与缺口画框结构上分开(self):
        """owner：「还是和缺口、画框冲突」——三种东西都画成"矩形"就必然互相盖。

        新口径：缺口/画框留在**主图坐标区**（markArea），密集区挪到**网格右侧留白**
        （custom series，clip:false）⇒ 结构上就不在同一个区域，颜色只是辅助区分。
        """
        # 缺口/画框侧保持原样：红/绿 + dashed，且 markArea 里不再有密集区
        j = JS.index("function buildGapAreas")
        self.assertIn('borderType: "dashed"', JS[j:j + 2600])
        self.assertIn("rgba(248,81,73,.17)", JS[j:j + 2600])
        self.assertNotIn("vpAreas", JS)
        # 密集区：自定义序列 + 网格外绘制
        i = JS.index("function _vpSeriesOption")
        seg = JS[i:i + 1800]
        self.assertIn('type: "custom"', seg)
        self.assertIn("clip: false", seg)
        self.assertIn("api.getWidth() - marginPx", seg)


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestMultiWindowOptionBehavior(unittest.TestCase):
    """把 `_mwOption` 整个跑起来（stub 掉画图无关的依赖），确认多窗口的 option 里
    **真的有**密集成交区序列 / POC 线 —— 单元测试只查字符串会漏掉运行时错误。"""

    def _run(self, vp_on, hist=None):
        import datetime
        import random
        random.seed(11)
        t = datetime.datetime(2026, 11, 2, 9, 35)
        p = 10.0
        dates, ohlc, vol, amt = [], [], [], []
        for _ in range(96):
            o = p
            c = o * (1 + random.uniform(-0.004, 0.004))
            h = max(o, c) * (1 + random.uniform(0, 0.002))
            l = min(o, c) * (1 - random.uniform(0, 0.002))
            v = random.randint(200, 4000)
            dates.append(t.strftime("%Y-%m-%d %H:%M"))
            ohlc.append([round(o, 3), round(c, 3), round(l, 3), round(h, 3)])
            vol.append(v)
            amt.append(round(((h + l + c) / 3.0) * v * 100.0))
            p = c
            t += datetime.timedelta(minutes=5)
        s = {"dates": dates, "ohlc": ohlc, "volume": vol, "amount": amt,
             "ma5": [None] * 96,
             "macd": {"dif": [], "dea": [], "hist": (hist if hist is not None else [])}}
        js = (
            "global.buildGapAreas=()=>[]; global._mwBoxMarkAreas=()=>[];\n"
            "global.buildIntradayVwapZero=()=>({vwap:[],zero:[]});\n"
            "global.VOL_MA_BASE=60; global.MW_TAIL=96; let mwPeriod='min5';\n"
            f"let _vpOn={'true' if vp_on else 'false'};\n"
            + _extract_fn("buildVolumeProfile") + "\n"
            + _extract_fn("_vpSeriesOption") + "\n"
            + _extract_fn("_mwOption") + "\n"
            + f"const s={json.dumps(s)};\n"
            "const o=_mwOption(s,null,null,[],null,'600000');\n"
            "const vp=o.series.find(x=>x&&x.name==='密集区');\n"
            "const mb=o.series.find(x=>x&&x.name==='MACD柱')||{};\n"
            "process.stdout.write(JSON.stringify({"
            "vp: vp?{type:vp.type,n:(vp.data||[]).length,clip:vp.clip,xAxisIndex:vp.xAxisIndex}:null,"
            "gridRight:o.grid[0].right,"
            "line: o.series[0].markLine?o.series[0].markLine.data:null,"
            "areas: o.series[0].markArea?o.series[0].markArea.data:null,"
            "macdBars: mb.data||null,"
            "macdMp: mb.markPoint?mb.markPoint.data.map(d=>d.coord):null,"
            "macdMl: mb.markLine?mb.markLine.data:null}));"
        )
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "t.js"
            fp.write_text(js, encoding="utf-8")
            out = subprocess.check_output([NODE, str(fp)], timeout=30)
        return json.loads(out.decode("utf-8"))

    def test_开时真的有直方图序列与POC(self):
        r = self._run(True)
        self.assertIsNotNone(r.get("vp"), f"多窗口 option 里没有密集区序列：{json.dumps(r)[:400]}")
        self.assertEqual(r["vp"]["type"], "custom")
        self.assertGreater(r["vp"]["n"], 10, "直方图应有全部分箱")
        self.assertTrue(r["vp"]["clip"] is False)
        self.assertEqual(r["vp"]["xAxisIndex"], 3, "挂在隐藏轴上，避免被 dataZoom 过滤")
        self.assertEqual(r["gridRight"], 52, "开着时右侧留白要加宽")
        self.assertTrue(any((x or {}).get("label", {}).get("formatter") == "POC"
                            for x in (r.get("line") or [])), "POC 线没进 markLine")
        # 缺口/画框的 markArea 里不该混进密集区
        self.assertNotIn("163,113,247", json.dumps(r.get("areas") or []))


    def test_翻红首根高亮且三角标出(self):
        """owner：「第一根 MACD 的红线要清晰，这信号十分重要」——绿转红的首根要亮红+白边+三角。"""
        hist = [-1.0, -0.5, 0.2, 0.4, -0.3, 0.1, 0.5]
        r = self._run(False, hist=hist)
        bars = r.get("macdBars") or []
        self.assertEqual(bars[2]["itemStyle"]["color"], "#ff3b30", "翻红首根要亮红")
        self.assertEqual(bars[2]["itemStyle"]["borderColor"], "#ffffff", "白边提高辨识度")
        self.assertNotEqual(bars[3]["itemStyle"].get("color"), "#ff3b30", "同一段的后续红柱不重复高亮")
        mp = r.get("macdMp") or []
        self.assertIn([2, 0.2], mp)
        self.assertIn([5, 0.1], mp)
        ml = r.get("macdMl") or []
        self.assertTrue(any(x.get("xAxis") == 5
                            and (x.get("label") or {}).get("formatter") == "翻红" for x in ml),
                        "最近一次翻红要有竖线+文字")

    def test_没有翻红就不加标记(self):
        r = self._run(False, hist=[0.1, 0.2, 0.3])
        self.assertIsNone(r.get("macdMp"))
        self.assertFalse(any("xAxis" in (x or {}) for x in (r.get("macdMl") or [])))

    def test_关时一条都不画(self):
        r = self._run(False)
        self.assertIsNone(r.get("vp"), "关掉后不该有密集区序列")
        self.assertEqual(r["gridRight"], 6, "关掉后右侧留白恢复")
        self.assertFalse(any((x or {}).get("label", {}).get("formatter") == "POC"
                             for x in (r.get("line") or [])), "关掉后不该有 POC 线")


if __name__ == "__main__":
    unittest.main(verbosity=2)
