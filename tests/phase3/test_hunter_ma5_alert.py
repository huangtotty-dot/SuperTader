# -*- coding: utf-8 -*-
"""猎手热门板块「刚站上5日线」飞书告警 单测（2026-10-09）——全离线，合成数据。"""
import json
import os
import sys
import tempfile
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.ma_reclaim as mr  # noqa: E402


class TestReclaim5(unittest.TestCase):
    def test_命中(self):
        # basis=[11,10.5,10,9.5,9] prev_close=9<prevMA5=10；price=11>curMA5=10
        r = mr.reclaim5_from_closes([11, 10.5, 10, 9.5, 9, 11])
        self.assertIsNotNone(r); self.assertAlmostEqual(r["prev_close"], 9.0)

    def test_已在MA5上不命中(self):
        self.assertIsNone(mr.reclaim5_from_closes([9, 9.5, 10, 10.5, 11, 11.5]))

    def test_帧版交易日闸(self):
        dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07", "2026-10-08", "2026-10-09"]
        df = pd.DataFrame({"date": dates, "close": [11, 10.5, 10, 9.5, 9, 11]})
        self.assertIsNotNone(mr.reclaim5_frame(df, "2026-10-09"))
        self.assertIsNone(mr.reclaim5_frame(df, "2026-10-08"))   # 末根!=目标日
        # 帧里只有到 10-08，目标 10-09 → 末根 != 目标 → None
        self.assertIsNone(mr.reclaim5_frame(df[df.date <= "2026-10-08"], "2026-10-09"))


class TestHunterHotPool(unittest.TestCase):
    def setUp(self):
        import core.hunter_ma5_alert as h
        self.h = h
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP)
        h._SUMMARY_FP = os.path.join(td, "summary.json")
        h._WL_FP = os.path.join(td, "wl.json")
        h._DEDUP_FP = os.path.join(td, "dup.json")

    def tearDown(self):
        self.h._SUMMARY_FP, self.h._WL_FP, self.h._DEDUP_FP = self._o
        self._td.cleanup()

    def _write(self):
        json.dump({"20261009": [{"板块": "电力", "平均分": 0.7},
                                {"板块": "医药", "平均分": 0.6},
                                {"板块": "冷门X", "平均分": 0.1}]},
                  open(self.h._SUMMARY_FP, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump({"600001": {"name": "电力股", "sector": "电力"},
                   "600002": {"name": "医药股", "jiuyan_concept1": "医药"},
                   "600003": {"name": "无关股", "sector": "钢铁"}},
                  open(self.h._WL_FP, "w", encoding="utf-8"), ensure_ascii=False)

    def test_topn_板块(self):
        self._write()
        self.assertEqual(self.h.hot_boards(2), ["电力", "医药"])

    def test_成分股并集(self):
        self._write()
        s = self.h.hot_board_stocks(2)
        self.assertIn("600001", s); self.assertIn("600002", s); self.assertNotIn("600003", s)


class TestHunterAlert(unittest.TestCase):
    def setUp(self):
        import core.hunter_ma5_alert as h
        self.h = h
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP)
        h._SUMMARY_FP = os.path.join(td, "summary.json")
        h._WL_FP = os.path.join(td, "wl.json")
        h._DEDUP_FP = os.path.join(td, "dup.json")
        json.dump({"20261009": [{"板块": "电力", "平均分": 0.7}]},
                  open(h._SUMMARY_FP, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump({"600001": {"name": "电力股", "sector": "电力"}},
                  open(h._WL_FP, "w", encoding="utf-8"), ensure_ascii=False)

    def tearDown(self):
        self.h._SUMMARY_FP, self.h._WL_FP, self.h._DEDUP_FP = self._o
        self._td.cleanup()
        if hasattr(self, "_o_gp"):
            from core.market_data import facade as _fd
            _fd.get_provider = self._o_gp

    def _patch(self):
        from core.market_data import facade as _fd
        self._o_gp = _fd.get_provider
        dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07", "2026-10-08", "2026-10-09"]
        close = [11, 10.5, 10, 9.5, 9, 11]
        df = pd.DataFrame({"date": dates, "open": close, "high": close, "low": close,
                           "close": close, "volume": [1e6] * len(close)})

        class _Fake:
            def daily_many(self, codes, days=0):
                return {"600001": df.copy()} if "600001" in codes else {}
        _fd.get_provider = lambda: _Fake()

    def test_扫描命中并去重(self):
        self._patch()
        ev = self.h.run_hunter_ma5_alert(date="2026-10-09", dry_run=True)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["code"], "600001")
        # 写去重后再次 → 空
        self.h.run_hunter_ma5_alert(date="2026-10-09", dry_run=True)   # dry_run 不写
        # 手动写去重
        self.h._save_dedup({"2026-10-09": ["600001"]})
        self.assertEqual(self.h.run_hunter_ma5_alert(date="2026-10-09", dry_run=True), [])

    def test_card(self):
        self._patch()
        ev = self.h.scan_hunter_ma5(date="2026-10-09")
        c = self.h.build_card(ev, date="2026-10-09")
        self.assertEqual(c["msg_type"], "interactive")
        self.assertIn("600001", c["card"]["elements"][0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
