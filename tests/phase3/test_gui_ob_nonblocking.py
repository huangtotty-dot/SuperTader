# -*- coding: utf-8 -*-
"""持仓体检 `load_ob_analysis` 的**冷启动不阻塞**防回退单测（2026-10-04，2026-10-10 改口径）。

背景：`load_ob_analysis` 逐只算 KDJ/CCI/标签/30min 判定（纯 Python 重活）⇒ 图表预热后同步跑
实测 **9~11s** 冻 pywebview 主线程。2026-10-10 改 **SWR**：`load_ob_analysis` 只读缓存/起后台算，
冷启动立即返回 `pending` 占位（前端重拉补）；计算体抽到 `_load_ob_analysis_impl`。

断言：
  T1 冷启动（无缓存）**立即**返回 `pending` 占位 —— 证明未同步算。
  T2 图表缓存就绪后，`_load_ob_analysis_impl` 读缓存出结果、不再 `pending`（wrapper 仍非阻塞）。
  T3 已清仓(qty=0)不计入 `pending`。

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


def _empty_df():
    return pandas.DataFrame()


class TestObNonBlocking(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._orig_load_json = t_gui._load_json
        # 2026-10-09：OB 现读 30min 磁盘缓存（`_m30_bars_cache_only` → `_fetch_min_bars_disk`）。
        # 测试桩成空 → 30min 列整体缺席、趋势回退日线；保证**全离线**且不依赖真实 cache。
        self._orig_min_disk = t_gui._fetch_min_bars_disk
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _empty_df()
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
        self._saved_m30 = dict(t_gui._M30_SNAP_CACHE)
        t_gui._M30_SNAP_CACHE.clear()
        self._today = datetime.now().strftime("%Y-%m-%d")

    def tearDown(self):
        t_gui._load_json = self._orig_load_json
        t_gui._fetch_min_bars_disk = self._orig_min_disk
        t_gui._M30_SNAP_CACHE.clear()
        t_gui._M30_SNAP_CACHE.update(self._saved_m30)
        self.api._stock_chart_cache = self._saved_cache

    def test_01_冷启动立即返回不阻塞(self):
        # 2026-10-10：load_ob_analysis 改 SWR——冷启动**起后台算 + 立即返回 pending 占位**，
        # 不再在主线程同步算（逐只 KDJ/CCI/标签 + 竞争预热线程，实测 9~11s）。
        t = time.perf_counter()
        r = self.api.load_ob_analysis()
        dt = time.perf_counter() - t
        self.assertLess(dt, 0.5, f"冷启动阻塞了 {dt:.2f}s，疑似仍同步算")
        self.assertEqual(len(r.get("stocks", [])), 0, "冷启动占位不应有体检行")
        self.assertTrue(r.get("pending"), "冷启动应返回 pending（后台算、前端重拉）")

    def test_02_缓存就绪后出结果(self):
        # 纯函数 `_load_ob_analysis_impl` 校验「读缓存出结果」；wrapper 只保证非阻塞。
        for c in ("000001", "600000"):
            self.api._stock_chart_cache[f"{self._today}_{c}"] = (datetime.now(), _fake_chart())
        t = time.perf_counter()
        self.api.load_ob_analysis()          # wrapper：非阻塞
        self.assertLess(time.perf_counter() - t, 0.5, "缓存就绪后 wrapper 仍阻塞")
        r = self.api._load_ob_analysis_impl()
        self.assertEqual(r.get("pending"), None, "全就绪时不应再有 pending")
        codes = {s["code"] for s in r.get("stocks", [])}
        self.assertEqual(codes, {"000001_A", "600000_B"})

    def test_03_清仓不计入pending(self):
        r = self.api._load_ob_analysis_impl()
        self.assertEqual(r.get("pending"), 2, "已清仓(qty=0)不应计入 pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
