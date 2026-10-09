# -*- coding: utf-8 -*-
"""持仓体检 `load_ob_analysis` 30 分钟口径 + **主线程零计算** 的离线单测（2026-10-09）。**全离线**。

背景：体检判定走 30min（趋势=30min 状态机；判定=30min 顶部/底背离特征）。其中 `evaluate_bars`
单只 ~300ms，7 只 ~2s ⇒ **绝不能在 pywebview 主线程算**（实测 load_ob_analysis 冷启动 5.4s）。
改为：主线程只读 `_M30_SNAP_CACHE`，miss → 判定显示「计算中」+ `m30_pending`（前端 3s 快速重拉），
由后台 `_warm_ob_m30_disk`（磁盘）/`refresh_ob_m30`（在线）填充。

断言：
  T1 冷启动（快照未热）：**立即返回**、`m30_pending` 置位、判定=pending —— 证主线程不算 30min。
  T1b 快照就绪后：trend_src=30min、top_features.ok、verdict 合法，且 `_fetch_30min`（网络）**从未被调用**。
  T2 无 30min 数据：trend 回退日线、top_features 不可用；仍零网络。
  T3 趋势列与行内趋势标签**同源一致**。

铁律：全离线。补桩 `_load_json`/磁盘分钟层；`analysis.trend30.adapter._fetch_30min` 打成抛异常。
运行：python -m unittest tests.phase3.test_gui_ob_30min
"""
import os
import sys
import time as _time
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402,F401
import pandas  # noqa: E402,F401
import analysis.indicators  # noqa: E402,F401  预热 `_stock_tags_from_df` 内的懒加载(~1.4s 一次性)
import t_gui  # noqa: E402
from analysis.trend30 import adapter as _adapter  # noqa: E402

_CODE = "000001"


def _empty_df():
    return pandas.DataFrame()


def _m30_uptrend(days=45, base=10.0, step=0.01):
    """构造一段稳定上行的 30min bars（8 根/日），跨多日 ⇒ 30min 状态机可判 BULL=up。"""
    rows, px = [], base
    times = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    for d in range(days):
        day = f"2026-01-{(d % 28) + 1:02d}"
        for hm in times:
            px += step
            rows.append({"time": f"{day} {hm}:00", "open": px - 0.005, "high": px + 0.02,
                         "low": px - 0.02, "close": px, "volume": 1000.0})
    return pandas.DataFrame(rows)


def _fake_chart(n=400, close=10.0):
    ohlc = [[close, close, close - 0.1, close + 0.1] for _ in range(n)]  # [open, close, low, high]
    dates = [(datetime(2026, 1, 1) + pandas.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    return {
        "available": True,
        "period_data": {"daily": {
            "dates": dates,
            "ohlc": ohlc,
            "volume": [1000.0] * n,
            "rsi": [50.0] * n,
            "macd": {"dif": [0.0] * n},
            "boll": {"up": [close + 1.0] * n},
        }},
        "channel": {"direction": "flat"},
    }


class TestObM30(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._orig_load_json = t_gui._load_json
        self._orig_min_disk = t_gui._fetch_min_bars_disk
        self._orig_fetch30 = _adapter._fetch_30min
        self._orig_mins_cache = dict(t_gui._MIN_BARS_CACHE)
        self._saved_m30 = dict(t_gui._M30_SNAP_CACHE)
        self._saved_chart = dict(getattr(self.api, "_stock_chart_cache", {}))

        t_gui._load_json = lambda p, d=None: (
            {"000001_A": {"name": "甲", "qty": 100}} if str(p) == str(t_gui.HOLDINGS_MANUAL)
            else (d if d is not None else {}))

        def _boom(*a, **k):           # 网络层：一旦被调用即失败（证明 OB 热路径零网络）
            raise AssertionError("OB 热路径不应调用网络 _fetch_30min")
        _adapter._fetch_30min = _boom
        t_gui._MIN_BARS_CACHE.clear()
        t_gui._M30_SNAP_CACHE.clear()

        self._today = datetime.now().strftime("%Y-%m-%d")
        self.api._stock_chart_cache = {f"{self._today}_{_CODE}": (datetime.now(), _fake_chart())}

    def tearDown(self):
        t_gui._load_json = self._orig_load_json
        t_gui._fetch_min_bars_disk = self._orig_min_disk
        _adapter._fetch_30min = self._orig_fetch30
        t_gui._MIN_BARS_CACHE.clear(); t_gui._MIN_BARS_CACHE.update(self._orig_mins_cache)
        t_gui._M30_SNAP_CACHE.clear(); t_gui._M30_SNAP_CACHE.update(self._saved_m30)
        self.api._stock_chart_cache = self._saved_chart

    def _warm(self, df=None):
        """模拟后台磁盘预热：把持仓的 30min 快照填进 `_M30_SNAP_CACHE`（df 缺省=上行帧）。"""
        t_gui._M30_SNAP_CACHE[f"{_CODE}_{t_gui._min_bars_slot()}"] = \
            t_gui._build_m30_snapshot(_m30_uptrend() if df is None else df)

    def test_01_cold_is_fast_and_pending(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _m30_uptrend()
        t0 = _time.perf_counter()
        r = self.api.load_ob_analysis()
        dt = _time.perf_counter() - t0
        self.assertLess(dt, 0.5, f"冷启动阻塞 {dt:.2f}s：主线程不应算 30min（应只读缓存）")
        self.assertTrue(r.get("m30_pending"), "未热时应置 m30_pending 供前端快速重拉")
        self.assertEqual(r["stocks"][0]["verdict"]["level"], "pending")

    def test_01b_warm_computes_verdict(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _m30_uptrend()
        self._warm()
        r = self.api.load_ob_analysis()
        s = r["stocks"][0]
        self.assertIsNone(r.get("m30_pending"), "已热不应再有 m30_pending")
        self.assertEqual(s.get("trend_src"), "30min", "趋势应来自 30min")
        self.assertTrue(s.get("top_features", {}).get("ok"), "30min 顶部特征应可用")
        self.assertIn(s["verdict"]["level"], ("high", "watch", "bull", "none", "na"))
        self.assertTrue(s["verdict"].get("label") and s["verdict"].get("reason"))

    def test_02_30min_absent_falls_back_daily(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _empty_df()
        self._warm(_empty_df())                       # 无数据 → 快照为 {}（已热但空）
        r = self.api.load_ob_analysis()
        s = r["stocks"][0]
        self.assertEqual(s.get("trend_src"), "daily", "无 30min 数据 → 趋势回退日线")
        self.assertFalse(s.get("top_features", {}).get("ok"), "无 30min → 顶部特征不可用")

    def test_03_trend_agrees_with_tags(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _m30_uptrend()
        self._warm()
        s = self.api.load_ob_analysis()["stocks"][0]
        label = s["tags"][0]["label"] if s.get("tags") else None      # 首个标签 = 上行/下行/震荡
        m = {"up": "上行", "down": "下行", "flat": "震荡"}
        self.assertEqual(label, m[s["trend"]], "趋势列与行内趋势标签必须同源一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
