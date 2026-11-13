# -*- coding: utf-8 -*-
"""数据窗口 Data Window（2026-11-14，TradingView「数据窗口」落地）。

核心纯函数 `buildDataWindow(period, i)`：十字光标停在哪根，就把那根的
OHLC/涨跌/量额/均线/BOLL/MACD/RSI 整理成结构化数据，供停靠面板陈列。node 跑真函数。

口径（钉住）：
  · index 越界自动夹紧到 [0, n-1]
  · 涨跌% = 相对**前一根**收盘；第 0 根无前一根 ⇒ chgPct/up 都是 null
  · 字段缺失 ⇒ null（面板显示「—」），不抛错
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
    i = _JS.index("function buildDataWindow")
    j = _JS.index("let _dwOpen", i)
    return _JS[i:j]


def _run(period, i):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify(buildDataWindow({json.dumps(period)}, {json.dumps(i)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _fixture():
    return {
        "dates": ["d0", "d1", "d2"],
        "ohlc": [[10, 10.5, 9.8, 10.6], [10.5, 11, 10.4, 11.2], [11, 10.8, 10.7, 11.3]],
        "volume": [100, 200, 300],
        "amount": [1e7, 2e7, 3e7],
        "ma": [[10, 10.5, 10.8], [9.8, 10.2, 10.5], [9.5, 10, 10.3]],
        "boll": {"mid": [10, 10.5, 10.8], "up": [10.5, 11, 11.3], "dn": [9.5, 10, 10.3]},
        "macd": {"dif": [0.1, 0.2, 0.15], "dea": [0.05, 0.1, 0.12], "hist": [0.1, 0.2, 0.06]},
        "rsi": [50, 55, 52],
    }


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestBuildDataWindow(unittest.TestCase):
    def test_空与坏输入(self):
        self.assertIsNone(_run(None, 0))
        self.assertIsNone(_run({"dates": []}, 0))

    def test_最后一根全字段(self):
        dw = _run(_fixture(), 2)
        self.assertEqual(dw["index"], 2)
        self.assertEqual(dw["date"], "d2")
        self.assertEqual(dw["open"], 11)
        self.assertEqual(dw["high"], 11.3)
        self.assertEqual(dw["low"], 10.7)
        self.assertEqual(dw["close"], 10.8)
        self.assertEqual(dw["volume"], 300)
        self.assertEqual(dw["amount"], 3e7)
        self.assertEqual(dw["ma5"], 10.8)
        self.assertEqual(dw["ma10"], 10.5)
        self.assertEqual(dw["ma20"], 10.3)
        self.assertEqual(dw["bollMid"], 10.8)
        self.assertEqual(dw["bollUp"], 11.3)
        self.assertEqual(dw["bollDn"], 10.3)
        self.assertEqual(dw["macdDif"], 0.15)
        self.assertEqual(dw["macdDea"], 0.12)
        self.assertEqual(dw["macdHist"], 0.06)
        self.assertEqual(dw["rsi"], 52)

    def test_涨跌相对前一根(self):
        dw = _run(_fixture(), 2)   # 收 10.8，前收 11 ⇒ -1.818…%
        self.assertAlmostEqual(dw["chgPct"], (10.8 - 11) / 11 * 100, places=4)
        self.assertFalse(dw["up"])
        dw1 = _run(_fixture(), 1)  # 收 11，前收 10.5 ⇒ 涨
        self.assertTrue(dw1["up"])
        self.assertGreater(dw1["chgPct"], 0)

    def test_第0根无前收_涨跌为_null(self):
        dw = _run(_fixture(), 0)
        self.assertIsNone(dw["chgPct"])
        self.assertIsNone(dw["up"])

    def test_越界夹紧(self):
        self.assertEqual(_run(_fixture(), 99)["index"], 2)
        self.assertEqual(_run(_fixture(), -5)["index"], 0)

    def test_字段缺失给_null(self):
        dw = _run({"dates": ["a"], "ohlc": [[1, 2, 0.5, 2.5]]}, 0)
        self.assertEqual(dw["close"], 2)
        self.assertIsNone(dw["volume"])
        self.assertIsNone(dw["ma5"])
        self.assertIsNone(dw["rsi"])


class TestDataWindowWiring(unittest.TestCase):
    def test_按钮接_toggleDataWindow(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="dataWinBtn"' in ln:
                found = True
                self.assertIn("toggleDataWindow()", ln)
        self.assertTrue(found, "index.html 缺 id=dataWinBtn 按钮")

    def test_函数存在(self):
        for fn in ("function toggleDataWindow()", "function _renderDataWindow()"):
            self.assertIn(fn, _JS)

    def test_十字光标接_updateAxisPointer(self):
        self.assertIn('stockChartInst.on("updateAxisPointer", (ev) => {', _JS)
        self.assertIn("_dwHoverIdx = idx;", _JS)

    def test_渲染后刷新面板(self):
        self.assertIn("if (_dwOpen) _renderDataWindow();", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
