# -*- coding: utf-8 -*-
"""K 线图「各周期顶/底背离标注」测试（2026-10-19 owner 需求）。

覆盖：
  A. 后端 `_attach_divergence`：period_data（ECharts [open,close,low,high] 口径）→
     每周期的事件 [{index,type,price,consec,dif,bars_ago}]；短序列(<40)不产键。
  B. 前端契约：app.js 画 divByIndex 散点、index.html 有「背离」开关。

⚠️ 与 test_scan_stage_timing 同款：**不 import t_gui**（GUI 主程序），按标记切出函数源码 exec。
运行：python -m unittest tests.phase3.test_chart_divergence_annotation -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SRC = (_ROOT / "t_gui.py").read_text(encoding="utf-8")


def _load_attach():
    a = _SRC.index("def _attach_divergence(")
    b = _SRC.index("def _to_series(", a)
    ns = {}
    exec(compile(_SRC[a:b], "t_gui.py:_attach_divergence", "exec"), ns)
    return ns["_attach_divergence"]


def _series(closes):
    closes = np.asarray(closes, dtype=float)
    return {
        "dates": [f"2026-01-{i + 1:02d}" for i in range(len(closes))],
        # ECharts candlestick 口径：[open, close, low, high]
        "ohlc": [[round(float(x), 3), round(float(x), 3),
                  round(float(x) - 0.05, 3), round(float(x) + 0.05, 3)]
                 for x in closes],
    }


def _top_pattern():
    c = []
    c += list(np.linspace(9.0, 10.0, 40))     # 前置上升趋势（给第一个峰足够 DIF）
    c += list(np.linspace(10.0, 10.4, 5))     # 快速冲高 → 峰 1
    c += list(np.linspace(10.4, 9.8, 10))
    c += list(np.linspace(9.8, 10.8, 40))     # 慢速爬升 → 峰 2（价格更高、DIF 更低）
    c += list(np.linspace(10.8, 10.1, 8))
    return c


def _bottom_pattern():
    c = []
    c += list(np.linspace(10.0, 9.5, 10))
    c += list(np.linspace(9.5, 10.2, 10))
    c += list(np.linspace(10.2, 9.3, 10))     # 谷 1
    c += list(np.linspace(9.3, 10.6, 20))
    c += list(np.linspace(10.6, 10.1, 10))
    return c


class TestAttachDivergence(unittest.TestCase):
    def setUp(self):
        self.attach = _load_attach()

    def test_top_divergence_event(self):
        payload = {"period_data": {"daily": _series(_top_pattern())}}
        out = self.attach(payload)
        ev = (out.get("divergence") or {}).get("daily") or []
        tops = [e for e in ev if e["type"] == "顶"]
        self.assertEqual(len(tops), 1, ev)
        e = tops[0]
        self.assertEqual(e["index"], 94)
        self.assertAlmostEqual(e["price"], 10.85)
        for k in ("time", "consec", "bars_ago", "dif"):
            self.assertIn(k, e)
        self.assertGreaterEqual(e["bars_ago"], 0)

    def test_bottom_divergence_event(self):
        payload = {"period_data": {"min30": _series(_bottom_pattern())}}
        out = self.attach(payload)
        ev = (out.get("divergence") or {}).get("min30") or []
        self.assertTrue(any(e["type"] == "底" for e in ev), ev)

    def test_short_series_is_skipped(self):
        payload = {"period_data": {"monthly": _series(np.linspace(10, 12, 20))}}
        out = self.attach(payload)
        self.assertNotIn("monthly", out.get("divergence") or {})

    def test_bad_payload_never_raises(self):
        out = self.attach({"period_data": {"daily": {"dates": [], "ohlc": []}}})
        self.assertEqual(out.get("divergence"), {})


class TestFrontendContract(unittest.TestCase):
    def test_app_js_draws_divergence(self):
        js = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("divByIndex", js)
        self.assertIn('name: "背离"', js)
        self.assertIn("顶背离", js)

    def test_index_html_has_toggle(self):
        html = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="tgDiv"', html)


if __name__ == "__main__":
    unittest.main()
