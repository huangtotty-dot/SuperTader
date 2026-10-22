# -*- coding: utf-8 -*-
"""猎手「刚站上5日线」**实时 watcher** 单测（2026-10-20，全离线）。

覆盖：
  A. `build_live_context`：完成日线 MA5 预计算（排除今日 forming bar；陈旧帧丢弃）；
  B. `scan_live_once`：隔夜回站 / 盘内 V 反转 / 快照日期闸 / 空快照 ok=False；
  C. `run_live_watch`：独立线程一轮命中即推（不依赖主循环）；
  D. `_in_live_session` 时段闸；`push_events` 去重。

运行：python -m unittest tests.phase3.test_hunter_ma5_live_watch -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.hunter_ma5_alert as h  # noqa: E402

CTX = {"date": "2026-10-20", "stocks": {"600001": "电力股"}, "codes": ["600001"],
       "hist": {"600001": {"prev_close": 9.0, "prev_ma5": 10.0, "last4_sum": 39.0}}}


class _FakeProvider:
    def __init__(self, prices, dates=None):
        self.prices = prices
        self.dates = dates or {}

    def snapshot_auction_many(self, codes):
        return {c: {"price": p, "ts_date": self.dates.get(c, "2026-10-20")}
                for c, p in self.prices.items() if c in codes}


class TestBuildLiveContext(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP, h._BELOW_FP)
        h._DEDUP_FP = os.path.join(self._td.name, "dup.json")
        h._BELOW_FP = os.path.join(self._td.name, "below.json")
        self._o_hbs = h.hot_board_stocks

    def tearDown(self):
        h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP, h._BELOW_FP = self._o
        h.hot_board_stocks = self._o_hbs
        self._td.cleanup()

    def _patch(self, dates, closes):
        h.hot_board_stocks = lambda top_n=None, date=None: {"600001": "电力股"}
        df = pd.DataFrame({"date": dates, "open": closes, "high": closes,
                           "low": closes, "close": closes, "volume": [1e6] * len(closes)})
        from core import chart_cache as _cc
        self._o_cc = _cc.load_daily_display
        _cc.load_daily_display = lambda code, now=None: df.copy()

    def tearDown_fd(self):
        from core import chart_cache as _cc
        if hasattr(self, "_o_cc"):
            _cc.load_daily_display = self._o_cc

    def test_context_excludes_today_and_computes_ma5(self):
        dates = ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14",
                 "2026-10-15", "2026-10-16", "2026-10-20"]
        closes = [12.0, 11.0, 10.5, 10.0, 9.5, 9.0, 9.9]   # 末根=今日 forming bar
        self._patch(dates, closes)
        try:
            ctx = h.build_live_context(date="2026-10-20", universe="hunter")
        finally:
            self.tearDown_fd()
        self.assertEqual(ctx["codes"], ["600001"])
        hi = ctx["hist"]["600001"]
        self.assertAlmostEqual(hi["prev_close"], 9.0)
        self.assertAlmostEqual(hi["prev_ma5"], 10.0)        # (11+10.5+10+9.5+9)/5
        self.assertAlmostEqual(hi["last4_sum"], 39.0)       # 10.5+10+9.5+9

    def test_stale_frame_is_dropped(self):
        dates = ["2026-08-01", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
        self._patch(dates, [11, 10.5, 10, 9.5, 9])
        try:
            ctx = h.build_live_context(date="2026-10-20", universe="hunter")
        finally:
            self.tearDown_fd()
        self.assertEqual(ctx["codes"], [], "超过 15 天的陈旧帧不得参与实时判定")


class TestScanLiveOnce(unittest.TestCase):
    def test_overnight_reclaim(self):
        hits, ok = h.scan_live_once(CTX, set(), provider=_FakeProvider({"600001": 11.0}))
        self.assertTrue(ok)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "overnight")
        self.assertAlmostEqual(hits[0]["ma5"], 10.0)        # (39+11)/5

    def test_v_reversal(self):
        # 昨收已在 MA5 上方（overnight=False），今日先跌破、再拉回 ⇒ vrev
        ctx = {"date": "2026-10-20", "stocks": {"600001": "电力股"}, "codes": ["600001"],
               "hist": {"600001": {"prev_close": 10.5, "prev_ma5": 10.0,
                                   "last4_sum": 39.0}}}
        below = set()
        hits, _ = h.scan_live_once(ctx, below, provider=_FakeProvider({"600001": 9.5}))
        self.assertEqual(hits, [])
        self.assertIn("600001", below)
        hits, _ = h.scan_live_once(ctx, below, provider=_FakeProvider({"600001": 10.5}))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "vrev")

    def test_stale_snapshot_ignored(self):
        p = _FakeProvider({"600001": 11.0}, dates={"600001": "2026-10-19"})
        hits, ok = h.scan_live_once(CTX, set(), provider=p)
        self.assertEqual(hits, [])
        self.assertTrue(ok, "有快照但日期不是今天 ⇒ ok=True、只是跳过")

    def test_empty_snapshot_not_ok(self):
        hits, ok = h.scan_live_once(CTX, set(), provider=_FakeProvider({}))
        self.assertEqual(hits, [])
        self.assertFalse(ok, "空快照必须 ok=False（供失败告警）")


class TestSessionAndWatcher(unittest.TestCase):
    def test_session_gate(self):
        self.assertTrue(h._in_live_session(datetime(2026, 10, 20, 10, 0)))
        self.assertTrue(h._in_live_session(datetime(2026, 10, 20, 13, 30)))
        self.assertFalse(h._in_live_session(datetime(2026, 10, 20, 12, 0)))
        self.assertFalse(h._in_live_session(datetime(2026, 10, 20, 15, 10)))
        self.assertFalse(h._in_live_session(datetime(2026, 10, 24, 10, 0)))   # 周六

    def test_watcher_pushes_within_one_poll(self):
        import threading
        self._td = tempfile.TemporaryDirectory()
        _o = (h._BELOW_FP, h._LIVE_CTX)
        h._BELOW_FP = os.path.join(self._td.name, "below.json")
        h._LIVE_CTX = {"date": None, "ts": 0.0, "sig": None, "ctx": None}
        _o_bl = h.build_live_context
        _o_hb = h.hot_boards
        h.build_live_context = lambda top_n=None, date=None, universe="pool": dict(CTX)
        h.hot_boards = lambda top_n=None, date=None: ["电力"]
        ev = threading.Event()
        got = []

        def _push(events, **kw):
            got.extend(events)
            return events

        def _sleep(sec):
            ev.set()          # 一轮后立即退出
        try:
            h.run_live_watch(poll_sec=0.01, stop_event=ev,
                             provider=_FakeProvider({"600001": 11.0}),
                             sleep_fn=_sleep,
                             now_fn=lambda: datetime(2026, 10, 20, 10, 0),
                             push_fn=_push)
        finally:
            h._BELOW_FP = _o[0]
            h._LIVE_CTX = _o[1]
            h.build_live_context = _o_bl
            h.hot_boards = _o_hb
            self._td.cleanup()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["code"], "600001")


if __name__ == "__main__":
    unittest.main()
