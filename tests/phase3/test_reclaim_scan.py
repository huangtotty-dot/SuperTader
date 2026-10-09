# -*- coding: utf-8 -*-
"""全市场「刚刚站上5日线」扫描 单测（2026-10-09）——全离线，合成帧，不打网络。

口径复用 core/position_builder.check_ma_break 的 reclaim5：
  prev_close < prev_MA5 且 今价 > cur_MA5（basis=截至昨日的收盘）。
运行：python tests/phase3/test_reclaim_scan.py
"""
import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 目标日之前的 5 个交易日 + 目标日
_DATES = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-08", "2026-10-09"]
_TODAY = "2026-10-09"


def _frame(closes):
    ds = _DATES[:len(closes)]
    return pd.DataFrame({"date": ds, "open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [1e6] * len(closes)})


class TestReclaimProbe(unittest.TestCase):
    def setUp(self):
        import t_gui
        self.api = t_gui.Api()

    def test_昨收破线今价站上命中(self):
        # basis=[11,10.5,10,9.5,9]：prev_close=9<prev_MA5=10；今价 11>cur_MA5=10 → 命中
        r = self.api._reclaim_probe_one("600000", _frame([11, 10.5, 10, 9.5, 9, 11]), _TODAY)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["prev_close"], 9.0)
        self.assertGreater(r["price"], 9.9)

    def test_已在MA5之上不重复报(self):
        # basis=[9,9.5,10,10.5,11]：prev_close=11>prev_MA5=10 → 非「刚站上」→ 不命中
        r = self.api._reclaim_probe_one("600000", _frame([9, 9.5, 10, 10.5, 11, 11.5]), _TODAY)
        self.assertIsNone(r)

    def test_无当日bar不命中(self):
        # 末根=10-08（<目标日）→ 交易日闸拦下
        r = self.api._reclaim_probe_one("600000", _frame([11, 10.5, 10, 9.5, 9]), _TODAY)
        self.assertIsNone(r)

    def test_空帧不崩(self):
        self.assertIsNone(self.api._reclaim_probe_one("600000", None, _TODAY))
        self.assertIsNone(self.api._reclaim_probe_one("600000", pd.DataFrame(), _TODAY))


class TestReclaimScan(unittest.TestCase):
    def _patch(self, fake):
        from core.market_data import facade as _fd
        self._o = _fd.get_provider
        _fd.get_provider = lambda: fake

    def tearDown(self):
        if hasattr(self, "_o"):
            from core.market_data import facade as _fd
            _fd.get_provider = self._o

    def test_命中计入且排序(self):
        frames = {"600000": _frame([11, 10.5, 10, 9.5, 9, 11]),
                  "000001": _frame([11, 10.5, 10, 9.5, 9, 10.2])}

        class _Fake:
            def daily_many(self, codes, days=0):
                return {c: frames[c].copy() for c in codes if c in frames}
        self._patch(_Fake())
        import t_gui
        st = {}
        hits = t_gui.Api()._scan_reclaim(["600000", "000001"], st, _TODAY)
        self.assertEqual(len(hits), 2)
        self.assertEqual(st.get("ok"), 2)
        self.assertEqual(st.get("fetch_failed"), False)
        self.assertTrue(all(h["tags"][0]["label"] == "站上5日线" for h in hits))

    def test_整池无帧判取数失败(self):
        class _Fake:
            def daily_many(self, codes, days=0):
                return {}
        self._patch(_Fake())
        import t_gui
        api = t_gui.Api(); api._GM_BATCH_RETRY_WAIT = 0
        st = {}
        hits = api._scan_reclaim(["600000", "000001"], st, _TODAY)
        self.assertEqual(hits, [])
        self.assertTrue(st.get("fetch_failed"))
        self.assertEqual(st.get("rest_no_data"), 2)

    def test_北交所缺帧单独计(self):
        frames = {"600000": _frame([11, 10.5, 10, 9.5, 9, 11])}

        class _Fake:
            def daily_many(self, codes, days=0):
                return {c: frames[c].copy() for c in codes if c in frames}
        self._patch(_Fake())
        import t_gui
        st = {}
        t_gui.Api()._scan_reclaim(["600000", "430047"], st, _TODAY)
        self.assertEqual(st.get("bj_no_data"), 1)
        self.assertEqual(st.get("rest_no_data"), 0)
        self.assertFalse(st.get("fetch_failed"))


class TestReclaimSourceGuards(unittest.TestCase):
    """静态源码闸：池全市场、走批量取数、force 先失效两缓存。"""
    import inspect as _ins

    def _src(self, name):
        import t_gui
        return self._ins.getsource(getattr(t_gui.Api, name))

    def test_扫描走批量取数(self):
        s = self._src("_scan_reclaim")
        self.assertIn("daily_many", s)
        self.assertIn("fetch_failed", s)

    def test_池为全市场(self):
        s = self._src("_breakout_pool_codes")
        self.assertIn("isdigit", s)

    def test_force先失效缓存(self):
        s = self._src("start_reclaim_scan")
        self.assertIn("pop(cache_key", s)
        self.assertIn("unlink", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
