# -*- coding: utf-8 -*-
"""「站上5日线天数」盘中实时口径单测（2026-11-13，全离线）。

背景（owner）：「站上5日线的数字没有实时进行更新」——该列原来只读**已落盘**日线：
盘中今日那根要么还没有（数字停在昨天），要么是 15min TTL 的 forming 快照（旧价）。

覆盖：
  A. 历史日不看实时价（`date != 今天` ⇒ 一律按日线缓存算）；
  B. 今天 + 实时价 ⇒ 追加当日临时根（今天还没落盘）；
  C. 今天 + 实时价跌破 ⇒ 归零；
  D. 缓存里今日是 **forming 根** ⇒ 用实时价顶替；
  E. 缓存里今日是 **已确认收盘根**（today 不在 forming）⇒ 不许顶替；
  F. 09:30 前（集合竞价）不许顶替 —— 竞价快照会造假当日根；
  G. `quote_bus.peek_quotes` 只读共享档、永不触发网络。

运行：python -m unittest tests.phase3.test_ma5_streak_live -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime as _dt

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402
import core.chart_cache as _cc  # noqa: E402

TODAY = "2026-11-13"


def _fake_now(hh, mm):
    class _FakeDT(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 11, 13, hh, mm, 0)
    return _FakeDT


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self._old_dir = t_gui._DAILY_KLINE_DIR
        self._old_dt = t_gui.datetime
        self._old_live = t_gui.Api._live_price_for
        t_gui._DAILY_KLINE_DIR = type(self._old_dir)(self.td)
        t_gui.datetime = _fake_now(10, 30)
        t_gui.Api._MA5_STREAK_CACHE.clear()
        _cc.invalidate_daily_close_cache()

    def tearDown(self):
        t_gui._DAILY_KLINE_DIR = self._old_dir
        t_gui.datetime = self._old_dt
        t_gui.Api._live_price_for = self._old_live
        t_gui.Api._MA5_STREAK_CACHE.clear()
        _cc.invalidate_daily_close_cache()

    def _mk(self, code, closes, dates=None, forming=()):
        dates = dates or ["2026-01-%02d" % (i + 1) for i in range(len(closes))]
        rec = {"date": TODAY, "saved_at": "2026-01-01 16:00:00", "forming": list(forming),
               "rows": [{"date": d, "open": c, "close": c, "high": c, "low": c, "volume": 1.0}
                        for d, c in zip(dates, closes)]}
        with open(os.path.join(self.td, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def _live(self, v):
        t_gui.Api._live_price_for = classmethod(lambda cls, b, d: v)


class TestMa5StreakLive(_Base):
    def test_rising_series_baseline_and_history(self):
        self._mk("000001", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self._live(None)
        self.assertEqual(t_gui.Api._ma5_streak("000001", "2026-01-09"), 5)
        t_gui.Api._MA5_STREAK_CACHE.clear()
        self.assertEqual(t_gui.Api._ma5_streak("000001"), 6)

    def test_live_price_appended_when_today_missing(self):
        self._mk("000001", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self._live(11.0)
        self.assertEqual(t_gui.Api._ma5_streak("000001"), 7)

    def test_live_price_breaks_below_ma5(self):
        self._mk("000001", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self._live(8.0)                       # 8 < MA5=(7+8+9+10+8)/5=8.4
        self.assertEqual(t_gui.Api._ma5_streak("000001"), 0)

    def test_history_date_never_uses_live(self):
        self._mk("000001", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self._live(11.0)
        self.assertEqual(t_gui.Api._ma5_streak("000001", "2026-01-10"), 6)

    def test_forming_bar_replaced_by_live(self):
        self._mk("000002", [1, 2, 3, 4, 5, 6, 7, 8, 9, 9],
                 dates=["2026-01-%02d" % (i + 1) for i in range(9)] + [TODAY],
                 forming=[TODAY])
        self._live(6.0)                       # 顶替掉旧价 9 ⇒ 6 < MA5=(6+7+8+9+6)/5=7.2
        self.assertEqual(t_gui.Api._ma5_streak("000002"), 0)

    def test_settled_bar_not_replaced(self):
        self._mk("000003", [1, 2, 3, 4, 5, 6, 7, 8, 9, 9],
                 dates=["2026-01-%02d" % (i + 1) for i in range(9)] + [TODAY],
                 forming=[])
        self._live(8.0)
        self.assertEqual(t_gui.Api._ma5_streak("000003"), 6)

    def test_preopen_auction_never_uses_live(self):
        self._mk("000001", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self._live(11.0)
        t_gui.datetime = _fake_now(9, 20)
        self.assertEqual(t_gui.Api._ma5_streak("000001"), 6)

    def test_stale_forming_tail_dropped(self):
        """过去某天仍挂 forming ⇒ 那根是盘中快照，不许当收盘（11-12 全池 2050 只）。"""
        dates = ["2026-11-%02d" % (i + 2) for i in range(9)]      # 11-02 … 11-10
        self._mk("000004", [1, 2, 3, 4, 5, 6, 7, 8, 9] + [100],
                 dates=dates + ["2026-11-12"], forming=["2026-11-12"])
        self._live(None)
        self.assertEqual(t_gui.Api._ma5_streak("000004"), 5)      # 丢掉 100 后按 1..9 算
        t_gui.Api._MA5_STREAK_CACHE.clear()
        self._live(11.0)
        self.assertEqual(t_gui.Api._ma5_streak("000004"), 6)      # 前一日 + 今日实时价

    def test_missing_cache_is_none(self):
        self._live(9.0)
        self.assertIsNone(t_gui.Api._ma5_streak("000999"))


class TestPeekQuotesNeverFetches(unittest.TestCase):
    """`peek_quotes` 只读共享档：档里没有的码返回缺省，**不许**去取数。"""

    def test_reads_store_only(self):
        import time as _t
        import core.market_data.quote_bus as qb
        old, old_load = qb._fetch, qb._load_store
        calls = []
        qb._fetch = lambda codes: calls.append(list(codes)) or {}
        qb._load_store = lambda: {}                     # 不读真档
        try:
            qb._MEMO["ts"], qb._MEMO["store"] = _t.time(), {"600001": {"ts": 1e18, "q": {"price": 9.0}}}
            self.assertEqual(qb.peek_quotes(["600001"], max_age_sec=None),
                             {"600001": {"price": 9.0}})
            self.assertEqual(qb.peek_quotes(["600002"], max_age_sec=None), {})
            self.assertEqual(calls, [])       # 一次网络都没打
        finally:
            qb._fetch, qb._load_store = old, old_load
            qb._MEMO["ts"], qb._MEMO["store"] = 0.0, None


if __name__ == "__main__":
    unittest.main(verbosity=2)
