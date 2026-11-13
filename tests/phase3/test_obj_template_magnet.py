# -*- coding: utf-8 -*-
"""对象模板 + 磁吸（2026-11-14，TradingView「对象模板 + 磁吸」落地）。

两个纯函数，node 跑 app.js 真函数：
  · applyBoxTemplate(base, tpl)：按现价 ±N% 或直接给上下沿，算画框上下沿（保留 2 位小数）
  · magnetSnap(price, bar, tolPct)：把价位吸到该根 K 线 开/高/低/收 最近的一个（容差内）

口径（钉住）：
  · applyBoxTemplate：base≤0 / tpl 非对象 / 无任何上下沿 ⇒ null；upperPct/lowerPct 可只给一边
  · magnetSnap：price 非有限数 / bar 非数组 ⇒ 原样返回；容差默认 0.2%（现价相对）
  · 只吸「最近的」OHLC 值；超容差不动
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
    i = _JS.index("function applyBoxTemplate")
    j = _JS.index("let _magnetOn", i)
    return _JS[i:j]


def _run(expr, *args):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify({expr}({', '.join(json.dumps(a) for a in args)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestApplyBoxTemplate(unittest.TestCase):
    def test_百分比模板(self):
        out = _run("applyBoxTemplate", 10, {"upperPct": 2, "lowerPct": 2})
        self.assertEqual(out, {"upper": 10.2, "lower": 9.8})

    def test_只给上边(self):
        out = _run("applyBoxTemplate", 10, {"upperPct": 5})
        self.assertEqual(out["upper"], 10.5)
        self.assertIsNone(out["lower"])

    def test_绝对价模板(self):
        out = _run("applyBoxTemplate", 10, {"upper": 11, "lower": 9})
        self.assertEqual(out, {"upper": 11, "lower": 9})

    def test_坏输入为_null(self):
        self.assertIsNone(_run("applyBoxTemplate", 0, {"upperPct": 2, "lowerPct": 2}))
        self.assertIsNone(_run("applyBoxTemplate", 10, None))
        self.assertIsNone(_run("applyBoxTemplate", 10, {}))


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestMagnetSnap(unittest.TestCase):
    def test_容差内吸到最近OHLC(self):
        # 10.01 → 最近的 10，距离 0.01 ≤ 10.01*0.2% ≈ 0.02
        self.assertEqual(_run("magnetSnap", 10.01, [10, 10.5, 9.95, 10.6], 0.2), 10)

    def test_正好命中不漂(self):
        self.assertEqual(_run("magnetSnap", 10.5, [10, 10.5, 9.95, 10.6], 0.2), 10.5)

    def test_超容差不吸(self):
        # 10.8 距最近 10.6 有 0.2，> eps≈0.0216 ⇒ 原样
        self.assertEqual(_run("magnetSnap", 10.8, [10, 10.5, 9.95, 10.6], 0.2), 10.8)

    def test_缺省容差用默认0_2(self):
        self.assertEqual(_run("magnetSnap", 10.01, [10, 10.5, 9.95, 10.6]), 10)

    def test_坏输入原样返回(self):
        self.assertEqual(_run("magnetSnap", 10, None, 0.2), 10)
        self.assertEqual(_run("magnetSnap", 10, [10, "x", 9, 12], 0.2), 10)   # 非数候选被过滤


class TestTemplateMagnetWiring(unittest.TestCase):
    def test_模板下拉接_onBoxTemplateSel(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="boxTplSel"' in ln:
                found = True
                self.assertIn("onBoxTemplateSel(this)", ln)
        self.assertTrue(found, "index.html 缺 id=boxTplSel 模板下拉")

    def test_磁吸开关接_onMagnetToggle(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="tgMagnet"' in ln:
                found = True
                self.assertIn("onMagnetToggle()", ln)
        self.assertTrue(found, "index.html 缺 id=tgMagnet 磁吸开关")

    def test_函数存在(self):
        for fn in ("function onBoxTemplateSel(sel)", "function applyBoxFromTemplate(pct)",
                   "function onMagnetToggle()"):
            self.assertIn(fn, _JS)

    def test_拖框接入磁吸(self):
        self.assertIn("up = magnetSnap(up, _bar, MAGNET_TOL_PCT);", _JS)
        self.assertIn("dn = magnetSnap(dn, _bar, MAGNET_TOL_PCT);", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
