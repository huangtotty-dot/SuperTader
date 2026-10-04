# -*- coding: utf-8 -*-
"""§5 做T/建仓许可映射单测（2026-10-04）。无网络：monkeypatch adapter.get_trend30。

运行：python tests/phase3/test_trend30_permission.py
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import analysis.trend30.adapter as adapter  # noqa: E402


class TestPermission(unittest.TestCase):
    def _stub(self, state, r2=0.7, er=0.5):
        def _g(code, **kw):
            return {"source": "30min", "code": code, "state": state, "trend": None,
                    "confidence": "high", "adx": 30.0, "bar_time": "x", "n_bars": 100,
                    "gate_ok": True, "r2": r2, "er10": er}
        self._orig = adapter.get_trend30
        adapter.get_trend30 = _g

    def tearDown(self):
        if hasattr(self, "_orig"):
            adapter.get_trend30 = self._orig

    def test_mapping_table(self):
        cases = {
            ("BULL", "bull"): (True, False, 1.00),
            ("BULL", "uptrend"): (True, False, 1.00),
            ("BULL", "neutral"): (True, True, 0.70),
            ("RANGE", "uptrend"): (True, False, 0.50),
            ("RANGE", "neutral"): (False, False, 0.00),
            ("RANGE", "downtrend"): (False, False, 0.00),
            ("BEAR", "bull"): (False, True, 0.00),
            ("BEAR", "base"): (False, True, 0.30),
            ("BEAR", "weak_breakdown"): (False, False, 0.00),
        }
        for (state, bg), (az, af, ratio) in cases.items():
            self._stub(state)
            p = adapter.get_trade_permission("000001", bg)
            self.assertEqual((p["allow_zheng_t"], p["allow_fan_t"], p["max_position_ratio"]),
                             (az, af, ratio), f"{state}×{bg}")

    def test_bull_x_daily_bear_needs_r2_er(self):
        self._stub("BULL", r2=0.3, er=0.1)
        p = adapter.get_trade_permission("000001", "downtrend")
        self.assertFalse(p["allow_zheng_t"], "R²/ER 未达标 → 停手")
        self.assertEqual(p["max_position_ratio"], 0.0)
        self._stub("BULL", r2=0.7, er=0.5)
        p2 = adapter.get_trade_permission("000001", "downtrend")
        self.assertTrue(p2["allow_zheng_t"])
        self.assertEqual(p2["max_position_ratio"], 0.30)

    def test_missing_daily_bg_falls_back_to_mid(self):
        self._stub("RANGE")
        p = adapter.get_trade_permission("000001", None)
        self.assertTrue(p["daily_fallback"])
        self.assertEqual(p["bucket"], "中")
        self.assertFalse(p["allow_zheng_t"])

    def test_30min_unavailable(self):
        def _g(code, **kw):
            return {"source": "error", "code": code, "state": None, "trend": None}
        self._orig = adapter.get_trend30
        adapter.get_trend30 = _g
        p = adapter.get_trade_permission("000001", "uptrend")
        self.assertIsNone(p["allow_zheng_t"])
        self.assertEqual(p["source"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
