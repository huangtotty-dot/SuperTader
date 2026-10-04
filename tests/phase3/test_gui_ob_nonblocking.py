# -*- coding: utf-8 -*-
"""持仓体检 `load_ob_analysis` 的**冷启动不阻塞**防回退单测（2026-10-04）。

背景：`load_ob_analysis` 逐只调 `load_stock_chart`（冷取数）⇒ 冷启动实测 **14.3s** 同步阻塞在
pywebview 主线程。本次改为**只用已预热的图表缓存**：未就绪的持仓本轮跳过、计入 `pending`，
前端稍后重拉（启动时 `prewarm_holdings_charts` 在后台填缓存）。

断言：
  T1 冷启动（图表缓存空）**立即**返回，且 `pending` == 有仓持仓数 —— 证明未同步 `load_stock_chart`。
  T2 图表缓存就绪后，持仓进入 `stocks` 且不再 `pending` —— 证明确实读缓存。

铁律：全离线。补桩 `_load_json` 提供固定持仓，不读真实 holdings 文件、不打网络。

运行：python tests/phase3/test_gui_ob_nonblocking.py
"""
import os
import sys
import time
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas  # noqa: E402,F401  预导入：真机由 load_day 早已载入，避免把首次 import 计进断言
import numpy  # noqa: E402,F401
import t_gui  # noqa: E402


def _fake_chart(n=40, close=10.0):
    """最小可用图表 payload（够 load_ob_analysis 跑完：ohlc/volume/rsi/macd/boll/channel）。"""
    ohlc = [[close, close, close - 0.1, close + 0.1] for _ in range(n)]  # [open, close, low, high]
    return {
        "available": True,
        "period_data": {"daily": {
            "ohlc": ohlc,
            "volume": [1000.0] * n,
            "rsi": [50.0] * n,
            "macd": {"dif": [0.0] * n},
            "boll": {"up": [close + 1.0] * n},
        }},
        "channel": {"direction": "flat"},
    }


class TestObNonBlocking(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._orig_load_json = t_gui._load_json
        self._holdings = {
            "000001_A": {"name": "甲", "qty": 100},
            "600000_B": {"name": "乙", "qty": 200},
            "300000_C": {"name": "丙", "qty": 0},   # 已清仓：不计
        }
        t_gui._load_json = lambda p, d=None: (
            self._holdings if str(p) == str(t_gui.HOLDINGS_MANUAL)
            else (d if d is not None else {}))
        self._saved_cache = dict(getattr(self.api, "_stock_chart_cache", {}))
        self.api._stock_chart_cache = {}
        self._today = datetime.now().strftime("%Y-%m-%d")

    def tearDown(self):
        t_gui._load_json = self._orig_load_json
        self.api._stock_chart_cache = self._saved_cache

    def test_01_冷启动立即返回且标记pending(self):
        t = time.perf_counter()
        r = self.api.load_ob_analysis()
        dt = time.perf_counter() - t
        self.assertLess(dt, 0.5, f"冷启动阻塞了 {dt:.2f}s，疑似仍同步调 load_stock_chart")
        self.assertEqual(len(r.get("stocks", [])), 0, "未预热时不应有体检行")
        self.assertEqual(r.get("pending"), 2, "pending 应等于有仓持仓数（甲/乙）")

    def test_02_缓存就绪后读缓存出结果(self):
        for c in ("000001", "600000"):
            self.api._stock_chart_cache[f"{self._today}_{c}"] = (datetime.now(), _fake_chart())
        t = time.perf_counter()
        r = self.api.load_ob_analysis()
        self.assertLess(time.perf_counter() - t, 0.5, "缓存就绪后仍阻塞")
        self.assertEqual(r.get("pending"), None, "全就绪时不应再有 pending")
        codes = {s["code"] for s in r.get("stocks", [])}
        self.assertEqual(codes, {"000001_A", "600000_B"})

    def test_03_清仓不计入pending(self):
        r = self.api.load_ob_analysis()
        self.assertEqual(r.get("pending"), 2, "已清仓(qty=0)不应计入 pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
