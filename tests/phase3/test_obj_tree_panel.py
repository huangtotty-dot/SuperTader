# -*- coding: utf-8 -*-
"""对象树面板（2026-11-14，TradingView「对象树」落地）。

核心是一个纯函数 `buildBoxTree(priceAlerts)`：把当前票的画框提醒（kind==="box"）拍平成
可逐条管理的清单（👁 隐藏 / 🗑 删除 / 点标签改上下沿）。用 node 跑 app.js 里的真函数。

口径（钉住）：
  · 只收 kind==="box"；坏价（upper/lower 非正有限数）跳过
  · triggered = 上破次数>0 或 下破次数>0
  · 排序：未触发在前，同状态按下沿升序
  · id 缺省用 `_auto_<原数组下标>`（仅展示用，不影响后端）
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
    i = _JS.index("function buildBoxTree")
    j = _JS.index("/* 「🗂 对象」面板", i)
    return _JS[i:j]


def _run(price_alerts):
    code = _extract() + f"\nconsole.log(JSON.stringify(buildBoxTree({json.dumps(price_alerts)})));"
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestBuildBoxTree(unittest.TestCase):
    def test_空与坏输入(self):
        self.assertEqual(_run([]), [])
        self.assertEqual(_run(None), [])

    def test_只收_box_跳过坏价与非_box(self):
        out = _run([
            {"kind": "box", "id": "b1", "upper": 30.5, "lower": 29.5, "up_n": 0, "down_n": 0},
            {"kind": "line", "id": "L", "price": 25},
            {"kind": "box", "id": "bad", "upper": -1, "lower": 0},
            None,
        ])
        self.assertEqual([b["id"] for b in out], ["b1"])

    def test_字段与触发口径(self):
        out = _run([
            {"kind": "box", "id": "b1", "upper": 30.5, "lower": 29.5, "up_n": 0, "down_n": 0},
            {"kind": "box", "id": "b2", "upper": 20, "lower": 18, "up_n": 2, "down_n": 0},
            {"kind": "box", "id": "b3", "upper": 50, "lower": 48, "up_n": 0, "down_n": 1},
        ])
        # 排序：未触发(b1)在前；已触发按 lower 升序(b2=18, b3=48)
        self.assertEqual([b["id"] for b in out], ["b1", "b2", "b3"])
        self.assertEqual(out[0]["triggered"], False)
        self.assertEqual(out[0]["tag"], "")
        self.assertEqual(out[1]["triggered"], True)
        self.assertEqual(out[1]["tag"], "上破×2 ")
        self.assertEqual(out[2]["tag"], "下破×1 ")
        self.assertEqual(out[0]["upper"], 30.5)
        self.assertEqual(out[0]["lower"], 29.5)

    def test_上破下破都触发时两边都计数(self):
        out = _run([{"kind": "box", "id": "x", "upper": 12, "lower": 10, "up_n": 3, "down_n": 2}])
        self.assertEqual(out[0]["triggered"], True)
        self.assertEqual(out[0]["tag"], "上破×3 下破×2 ")

    def test_缺_id_用_auto_下标(self):
        out = _run([{"kind": "box", "upper": 10, "lower": 9, "up_n": 0, "down_n": 0}])
        self.assertEqual(out[0]["id"], "_auto_0")


class TestObjTreeWiring(unittest.TestCase):
    def test_工具栏按钮接_toggleObjTree(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="objTreeBtn"' in ln:
                found = True
                self.assertIn("toggleObjTree()", ln, "对象树按钮要接 toggleObjTree")
        self.assertTrue(found, "index.html 缺 id=objTreeBtn 按钮")

    def test_面板函数存在(self):
        self.assertIn("function toggleObjTree()", _JS)
        self.assertIn("function _renderObjTree()", _JS)
        self.assertIn("function _paToggleBoxHide(id)", _JS)

    def test_隐藏态接入画框渲染(self):
        # _paBoxAreas 必须跳过被隐藏的框，否则「👁 隐藏」不生效
        self.assertIn("if (_paHiddenBoxes.has(String(a.id))) return;", _JS)

    def test_换票清隐藏态(self):
        self.assertIn("if (priceAlertsCode !== c) _paHiddenBoxes.clear();", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
