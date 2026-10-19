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
import pandas as pd

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


def _shallow_pullback_top():
    """浅回撤(2%)后创新高：DIF 走低但摆动幅度 < 信号口径 4×中位振幅。

    这正是 owner 2026-10-19 报「漏掉很多」的典型：信号口径要求 4×中位 bar 振幅
    （日线中位 ~1% ⇒ 4%）的逆向摆动，而真实背离常常只有 2% 回撤。
    """
    c = []
    c += list(np.linspace(9.0, 10.0, 40))
    c += list(np.linspace(10.0, 10.4, 5))          # 峰 1
    c += list(np.linspace(10.4, 10.4 * (1 - 0.02), 10))   # 只回撤 2%
    c += list(np.linspace(10.4 * (1 - 0.02), 10.45, 30))   # 慢速创新高 → 峰 2
    c += list(np.linspace(10.45, 10.45 * 0.98, 8))
    return c


def _hist_fade_top():
    """柱线动量衰减：价格新高，但红柱已从近期峰值缩短（历史 06-30/07-01 同型）。"""
    c = []
    c += list(np.linspace(10.0, 11.0, 30))
    c += list(np.linspace(11.0, 11.02, 3))     # 停滞后红柱缩短
    c += [11.08]                               # 价格再创新高
    c += list(np.linspace(11.08, 10.8, 8))
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

    def test_display_profile_catches_shallow_pullback(self):
        """展示口径必须比信号口径宽：同一序列信号口径 0 顶，展示口径 ≥1 顶。"""
        from analysis import divergence as dv
        closes = np.asarray(_shallow_pullback_top(), dtype=float)
        # test helper 的 bar 振幅固定 ±0.5%（中位 1%）
        frame = pd.DataFrame({
            "time": [f"2026-01-{i + 1:02d}" for i in range(len(closes))],
            "open": closes, "close": closes,
            "high": closes * 1.005, "low": closes * 0.995,
        })
        strict = [e for e in dv.detect_divergence_events(frame) if e["type"] == "顶"]
        self.assertEqual(strict, [], "信号口径应过滤掉 2% 浅回撤")
        out = self.attach({"period_data": {"daily": _series(closes)}})
        tops = [e for e in (out["divergence"].get("daily") or []) if e["type"] == "顶"]
        self.assertGreaterEqual(len(tops), 1, out)

    def test_hist_fade_top_is_marked(self):
        """第二类标注：DIF 两峰无背离，但价格新高 + 红柱缩短 ⇒ 必须标出来。"""
        from analysis import divergence as dv
        closes = np.asarray(_hist_fade_top(), dtype=float)
        frame = pd.DataFrame({
            "time": [f"2026-01-{i + 1:02d}" for i in range(len(closes))],
            "open": closes, "close": closes,
            "high": closes * 1.002, "low": closes * 0.998,
        })
        fade = dv.detect_hist_fade_events(frame)
        self.assertTrue(any(e["type"] == "顶" and e.get("hist", 0) > 0 for e in fade), fade)
        out = self.attach({"period_data": {"daily": _series(closes)}})
        ev = out["divergence"].get("daily") or []
        self.assertTrue(any(e.get("source") == "hist_fade" and e["type"] == "顶" for e in ev), ev)

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
