# -*- coding: utf-8 -*-
"""`load_stock_chart` cache-first 路径离线单测（2026-10-04）。

断言盘后预下载的缓存能让图表**绕开 GM 优先路径**（`fetch_daily_kline`）：
  T1 日线历史缓存命中 ⇒ 返回 available 且**未调用** fetch_daily_kline；miss ⇒ 回退调用（保 GM-first 回归）。
  T2 payload 缓存命中 ⇒ 直接返回（瞬开），不碰 fetch_daily_kline。
  T3 `_fetch_min_bars_disk`：新鲜的 plain 档优先于陈旧 `_d540`。

铁律：全离线。patch 掉缓存读取与所有网络入口（fetch_daily_kline / _fetch_min_bars）。

运行：python tests/phase3/test_chart_cache_first.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import t_gui  # noqa: E402
import core.chart_cache as cc  # noqa: E402


def _daily_df(n=400, start=10.0):
    days = [datetime.now() - timedelta(days=n - 1 - i) for i in range(n)]
    return pd.DataFrame({
        "date": pd.to_datetime([d.strftime("%Y-%m-%d") for d in days]),
        "open": [start + i * 0.01 for i in range(n)],
        "high": [start + i * 0.01 + 0.1 for i in range(n)],
        "low": [start + i * 0.01 - 0.1 for i in range(n)],
        "close": [start + i * 0.01 for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
    })


class TestCacheFirst(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()

    def test_01_日线缓存命中_绕开网络(self):
        calls = {"fetch": 0}

        def _fake_fetch(code):
            calls["fetch"] += 1
            raise AssertionError("命中了日线缓存却仍调用 fetch_daily_kline")
        with mock.patch.object(cc, "load_payload", return_value=None), \
             mock.patch.object(cc, "load_daily_display", return_value=_daily_df()), \
             mock.patch("t_gui._fetch_min_bars", return_value=pd.DataFrame()), \
             mock.patch("core.position_builder.fetch_daily_kline", _fake_fetch):
            r = self.api.load_stock_chart("600000")
        self.assertTrue(r.get("available"))
        self.assertEqual(calls["fetch"], 0)

    def test_02_缓存miss则回退fetch(self):
        calls = {"fetch": 0}

        def _fake_fetch(code):
            calls["fetch"] += 1
            return _daily_df()
        with mock.patch.object(cc, "load_payload", return_value=None), \
             mock.patch.object(cc, "load_daily_display", return_value=None), \
             mock.patch("t_gui._fetch_min_bars", return_value=pd.DataFrame()), \
             mock.patch("core.position_builder.fetch_daily_kline", _fake_fetch):
            r = self.api.load_stock_chart("600000")
        self.assertTrue(r.get("available"))
        self.assertEqual(calls["fetch"], 1, "miss 时必须回退 fetch_daily_kline（GM-first 语义不变）")

    def test_03_payload命中_瞬开(self):
        today = datetime.now().strftime("%Y-%m-%d")
        payload = {"available": True, "version": "vX", "code": "600000",
                   "period_data": {"daily": {"dates": [today], "ohlc": [[1, 1, 1, 1]]},
                                   "weekly": {"dates": []}, "monthly": {"dates": []}},
                   "current_price": 1.0}
        hit = {"payload": payload, "daily_rows": [{"date": today, "open": 1, "high": 1, "low": 1,
                                                   "close": 1, "volume": 1}],
               "last_daily_date": today, "version": "vX"}
        calls = {"fetch": 0}

        def _fake_fetch(code):
            calls["fetch"] += 1
            raise AssertionError("payload 命中却仍调用 fetch_daily_kline")
        with mock.patch.object(cc, "load_payload", return_value=hit), \
             mock.patch("core.position_builder.fetch_daily_kline", _fake_fetch):
            r = self.api.load_stock_chart("600000")
        self.assertTrue(r.get("available"))
        self.assertEqual(calls["fetch"], 0)

    def test_05_默认不取分时_按需才取(self):
        calls = {"min": 0}

        def _fake_min(code, freq="30min", days=None):
            calls["min"] += 1
            return pd.DataFrame()
        with mock.patch.object(cc, "load_payload", return_value=None), \
             mock.patch.object(cc, "load_daily_display", return_value=_daily_df()), \
             mock.patch("t_gui._fetch_min_bars", _fake_min), \
             mock.patch("core.position_builder.fetch_daily_kline", lambda c: _daily_df()):
            r1 = self.api.load_stock_chart("600000")
            self.assertEqual(calls["min"], 0, "日线视图默认不应取分时（want_minutes=False）")
            self.assertNotIn("min30", r1.get("period_data", {}))
            r2 = self.api.load_stock_chart("600000", None, True)   # 切到分钟 Tab
            self.assertGreater(calls["min"], 0, "显式要分时时必须取数")
            self.assertTrue(r2.get("available"))

    def test_06_要分时但payload无分时_回落重建(self):
        today = datetime.now().strftime("%Y-%m-%d")
        payload = {"available": True, "version": "vN", "code": "600000",
                   "period_data": {"daily": {"dates": [today], "ohlc": [[1, 1, 1, 1]]}}}
        hit = {"payload": payload,
               "daily_rows": [{"date": today, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
               "last_daily_date": today, "version": "vN"}
        calls = {"min": 0}

        def _fake_min(code, freq="30min", days=None):
            calls["min"] += 1
            return pd.DataFrame()
        with mock.patch.object(cc, "load_payload", return_value=hit), \
             mock.patch.object(cc, "load_daily_display", return_value=_daily_df()), \
             mock.patch("t_gui._fetch_min_bars", _fake_min), \
             mock.patch("core.position_builder.fetch_daily_kline", lambda c: _daily_df()):
            r = self.api.load_stock_chart("600000", None, True)
        self.assertTrue(r.get("available"))
        self.assertGreater(calls["min"], 0, "payload 无分时但前端要分时 ⇒ 必须回落重建、取分时")

    def test_07_北交所无缓存_立即降级不阻塞(self):
        """北交所日线源全不通 ⇒ 无缓存时必须**立即**优雅降级，不得走 fetch（实测 18s 冻主线程）。"""
        import time as _t
        with mock.patch.object(cc, "load_payload", return_value=None), \
             mock.patch.object(cc, "load_daily_display", return_value=None), \
             mock.patch("core.position_builder.fetch_daily_kline",
                        side_effect=AssertionError("北交所不应走 fetch_daily_kline")):
            t0 = _t.time()
            r = self.api.load_stock_chart("830799")
        self.assertFalse(r.get("available"))
        self.assertLess(_t.time() - t0, 1.0, "北交所无缓存必须立即降级")

    def test_04_分钟磁盘取新者(self):
        tmp = tempfile.mkdtemp(prefix="min_disk_")
        from pathlib import Path as _P
        old_rows = [{"time": "2026-09-01 14:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        new_rows = [{"time": "2026-10-01 14:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}]
        _P(tmp, "600000.SH_30min_d540.json").write_text(
            __import__("json").dumps({"rows": old_rows}), encoding="utf-8")
        _P(tmp, "600000.SH_30min.json").write_text(
            __import__("json").dumps({"date": "2026-10-01", "rows": new_rows}), encoding="utf-8")
        with mock.patch.object(t_gui, "_MIN_BARS_DIR", _P(tmp)), \
             mock.patch.object(cc, "_MINUTE_CACHE_DIR", _P(tmp, "empty_none")), \
             mock.patch.object(cc, "load_minute_history", return_value=None):
            df = t_gui._fetch_min_bars_disk("600000.SH", "30min")
        self.assertFalse(df.empty)
        self.assertEqual(str(df["time"].iloc[-1])[:10], "2026-10-01", "应取更晚的 plain 档而非陈旧 _d540")


if __name__ == "__main__":
    unittest.main(verbosity=2)
