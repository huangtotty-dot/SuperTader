# -*- coding: utf-8 -*-
"""多图同步十字光标（2026-11-14，TradingView「多图同步十字光标」落地）。

九宫格里一格动、其它格跟着动（认「同一根」）。核心纯函数 `mwCrosshairTarget(ev)`：
从 ECharts `updateAxisPointer` 事件里取出 x 轴当前值（category 轴是时间字符串），
供 `_mwBindCrosshair` 原样 `dispatchAction` 推给其它格。node 跑真函数。

口径（钉住）：
  · axesInfo 里 axisDim==="x" 且 value 非空 ⇒ 返回该 value；否则 null
  · ev 为空 / 无 x 轴信息 ⇒ null（跨周期时间轴对不上就跳过，不乱跳）
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _extract():
    i = _JS.index("function mwCrosshairTarget")
    j = _JS.index("function _mwBindCrosshair", i)
    return _JS[i:j]


def _run(expr, arg):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify({expr}({json.dumps(arg, ensure_ascii=False)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestMwCrosshairTarget(unittest.TestCase):
    def test_取_x_轴_value(self):
        self.assertEqual(_run("mwCrosshairTarget",
                              {"axesInfo": [{"axisDim": "x", "axisIndex": 0, "value": "2026-11-14 09:35"}]}),
                         "2026-11-14 09:35")

    def test_多个轴只认_x(self):
        # 混入 y 轴信息，也只要 x 轴那条
        self.assertEqual(_run("mwCrosshairTarget",
                              {"axesInfo": [
                                  {"axisDim": "y", "axisIndex": 0, "value": 12.3},
                                  {"axisDim": "x", "axisIndex": 0, "value": "d1"},
                              ]}),
                         "d1")

    def test_数值轴返回数值(self):
        self.assertEqual(_run("mwCrosshairTarget",
                              {"axesInfo": [{"axisDim": "x", "axisIndex": 0, "value": 42}]}),
                         42)

    def test_坏输入为_null(self):
        self.assertIsNone(_run("mwCrosshairTarget", None))
        self.assertIsNone(_run("mwCrosshairTarget", {}))
        self.assertIsNone(_run("mwCrosshairTarget", {"axesInfo": []}))

    def test_x轴值为空为_null(self):
        self.assertIsNone(_run("mwCrosshairTarget",
                               {"axesInfo": [{"axisDim": "x", "axisIndex": 0, "value": ""}]}))
        self.assertIsNone(_run("mwCrosshairTarget",
                               {"axesInfo": [{"axisDim": "x", "axisIndex": 0, "value": None}]}))


class TestMwCrosshairWiring(unittest.TestCase):
    def test_开关接_tgMwXhair(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="tgMwXhair"' in ln:
                found = True
                self.assertIn("checked", ln, "默认应开启同步光标")
        self.assertTrue(found, "index.html 缺 id=tgMwXhair 光标同步开关")

    def test_函数存在(self):
        self.assertIn("function mwCrosshairTarget(ev)", _JS)
        self.assertIn("function _mwBindCrosshair()", _JS)
        self.assertIn("let _mwXhairBusy = false;", _JS)

    def test_两处_init_都挂_crosshair(self):
        # 首建（_mwBindResize 之后）与懒建（_mwBindZoom 之后）都要补绑定
        self.assertIn("_mwBindResize();   // 浮窗右下角一缩放", _JS)
        self.assertIn("_mwBindCrosshair();   // 跨格同步十字光标", _JS)
        self.assertIn("_mwBindCrosshair();   // 懒建出的格也要补上跨格同步", _JS)

    def test_dispatch_推给其它格(self):
        self.assertIn('o.dispatchAction({ type: "updateAxisPointer"', _JS)
        self.assertIn('axesInfo: [{ axisDim: "x", axisIndex: 0, value: v }]', _JS)

    def test_幂等绑定标记(self):
        self.assertIn("inst.__mwXhairBound", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
