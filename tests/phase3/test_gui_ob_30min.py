# -*- coding: utf-8 -*-
"""持仓体检 `load_ob_analysis` 改用 30 分钟口径的离线单测（2026-10-09）。**全离线**。

背景：体检表趋势/背离/风险提醒三列改 30min（趋势=30min 状态机；背离=30min MACD 背离；
风险=30min 顶部特征 T1–T4）。数据走 `_m30_bars_cache_only`（只读内存/磁盘，**零网络**）。

断言：
  T1 30min 可得时：trend_src=="30min"、divergence 新结构、top_features.ok=True；
     且 **网络层 `_fetch_30min` 绝不被调用**（打成抛异常不会触发）—— 证明热路径零网络。
  T2 30min 缺席时：trend_src=="daily"、top_features 无（ok 缺失）；仍零网络。
  T3 趋势列与行内「上行/下行/震荡」标签**同源一致**（单一 t30 口径）。

铁律：全离线。补桩 `_load_json` 给固定持仓、补桩磁盘分钟层，不读真实 cache、不打网络。
运行：python -m unittest tests.phase3.test_gui_ob_30min
"""
import os
import sys
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402,F401
import pandas  # noqa: E402,F401
import t_gui  # noqa: E402
from analysis.trend30 import adapter as _adapter  # noqa: E402


def _empty_df():
    return pandas.DataFrame()


def _m30_uptrend(days=45, base=10.0, step=0.01):
    """构造一段稳定上行的 30min bars（8 根/日），跨多日 ⇒ 30min 状态机可判 BULL=up。"""
    rows, px, t = [], base, 0
    times = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    for d in range(days):
        day = f"2026-01-{ (d % 28) + 1 :02d}"
        for hm in times:
            px += step
            rows.append({"time": f"{day} {hm}:00", "open": px - 0.005, "high": px + 0.02,
                         "low": px - 0.02, "close": px, "volume": 1000.0})
            t += 1
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
        # 网络层：一旦被调用即失败（证明 OB 热路径零网络）
        def _boom(*a, **k):
            raise AssertionError("OB 热路径不应调用网络 _fetch_30min")
        _adapter._fetch_30min = _boom
        t_gui._MIN_BARS_CACHE.clear()
        t_gui._M30_SNAP_CACHE.clear()

        self._today = datetime.now().strftime("%Y-%m-%d")
        self.api._stock_chart_cache = {f"{self._today}_000001": (datetime.now(), _fake_chart())}

    def tearDown(self):
        t_gui._load_json = self._orig_load_json
        t_gui._fetch_min_bars_disk = self._orig_min_disk
        _adapter._fetch_30min = self._orig_fetch30
        t_gui._MIN_BARS_CACHE.clear(); t_gui._MIN_BARS_CACHE.update(self._orig_mins_cache)
        t_gui._M30_SNAP_CACHE.clear(); t_gui._M30_SNAP_CACHE.update(self._saved_m30)
        self.api._stock_chart_cache = self._saved_chart

    def test_01_30min_available(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _m30_uptrend()
        r = self.api.load_ob_analysis()
        stocks = r.get("stocks", [])
        self.assertEqual(len(stocks), 1, "应出 1 行体检（图表缓存已就绪）")
        s = stocks[0]
        self.assertEqual(s.get("trend_src"), "30min", "趋势应来自 30min")
        self.assertIn(s.get("trend"), ("up", "down", "flat"))
        self.assertIn("type", s.get("divergence", {}), "背离应为新结构 {type,...}")
        self.assertTrue(s.get("top_features", {}).get("ok"), "30min 顶部特征应可用")
        self.assertIn(s.get("risk"), ("高", "中", "低"))

    def test_02_30min_absent_falls_back_daily(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _empty_df()
        r = self.api.load_ob_analysis()
        s = r["stocks"][0]
        self.assertEqual(s.get("trend_src"), "daily", "无 30min 数据 → 趋势回退日线")
        self.assertFalse(s.get("top_features", {}).get("ok"), "无 30min → 顶部特征不可用")

    def test_03_trend_agrees_with_tags(self):
        t_gui._fetch_min_bars_disk = lambda ts_code, freq: _m30_uptrend()
        r = self.api.load_ob_analysis()
        s = r["stocks"][0]
        label = s["tags"][0]["label"] if s.get("tags") else None      # 首个标签 = 上行/下行/震荡
        m = {"up": "上行", "down": "下行", "flat": "震荡"}
        self.assertEqual(label, m[s["trend"]], "趋势列与行内趋势标签必须同源一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
