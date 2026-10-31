# -*- coding: utf-8 -*-
"""重点关注标记（2026-10-31 owner）：五张表每行一颗星，点击变紫。

守四条：
  ① 五个表（持仓30min体检/建仓信号扫描/异常监控池/避风港湾/选股猎手）的**行模板**里
     都插入了 `focusStarHtml(...)`；
  ② 紫色不与既有颜色冲突：`.focus-star.on` 用 #8250df（红/绿/橙/黄都不是它），
     且行首紫条只加 box-shadow，不覆盖行背景；
  ③ 后端 `toggle_focus` 幂等、落盘 `t_io/state/focus_stocks.json`，`load_focus_codes` 回读一致；
  ④ 坏文件不炸（读失败 → 空表）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
CSS = (Path(_ROOT) / "web" / "style.css").read_text(encoding="utf-8")
PY = (Path(_ROOT) / "t_gui.py").read_text(encoding="utf-8")


class TestFrontend(unittest.TestCase):
    def test_五张表都插了星标(self):
        # 定义 1 处 + 行模板 5 处（异常池/避风港/建仓/猎手/体检）
        self.assertGreaterEqual(JS.count("focusStarHtml("), 6,
                                f"focusStarHtml 出现 {JS.count('focusStarHtml(')} 次，应有 5 表 + 定义")
        self.assertIn('data-focus-code="${esc(c)}"', JS)
        self.assertIn("function toggleFocus(", JS)
        self.assertIn("loadFocusCodes();", JS)

    def test_紫色且不撞色(self):
        self.assertIn(".focus-star.on", CSS)
        self.assertIn("#8250df", CSS)
        # 行首紫条用 box-shadow（不覆盖既有行背景）
        self.assertIn("tr.focus-row td:first-child", CSS)
        self.assertIn("box-shadow: inset 3px 0 0 #8250df", CSS)

    def test_点击不冒泡到双击开图(self):
        self.assertIn("event.stopPropagation();toggleFocus(this)", JS)
        self.assertIn('ondblclick="event.stopPropagation()"', JS)


class TestBackend(unittest.TestCase):
    def setUp(self):
        import t_gui
        self.tg = t_gui
        self.td = tempfile.TemporaryDirectory()
        self._orig = t_gui._FOCUS_FP
        t_gui._FOCUS_FP = Path(self.td.name) / "focus_stocks.json"
        self.api = t_gui.Api()

    def tearDown(self):
        self.tg._FOCUS_FP = self._orig
        self.td.cleanup()

    def test_切换与回读(self):
        r = self.api.toggle_focus("600362")
        self.assertTrue(r["on"])
        self.assertIn("600362", r["codes"])
        self.assertEqual(self.api.load_focus_codes()["codes"], ["600362"])
        saved = json.loads(self.tg._FOCUS_FP.read_text(encoding="utf-8"))
        self.assertIn("600362", saved["codes"])

        r = self.api.toggle_focus("600362_SH")     # 带后缀也能归一
        self.assertFalse(r["on"])
        self.assertEqual(self.api.load_focus_codes()["codes"], [])

    def test_多只并存(self):
        for c in ("000001", "600519", "301079"):
            self.api.toggle_focus(c)
        self.assertEqual(self.api.load_focus_codes()["codes"], ["000001", "301079", "600519"])

    def test_坏文件不炸(self):
        self.tg._FOCUS_FP.parent.mkdir(parents=True, exist_ok=True)
        self.tg._FOCUS_FP.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.api.load_focus_codes()["codes"], [])
        self.assertTrue(self.api.toggle_focus("600000")["on"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
