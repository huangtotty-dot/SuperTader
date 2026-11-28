# -*- coding: utf-8 -*-
"""H1~H5 假说脚本 as-of 硬边界（全离线）。"""
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.experiment import h_hypothesis_daily as H  # noqa: E402


class TestHypothesisAsOf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "t_io" / "cache" / "daily_kline").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _bars(self, code, rows, forming=()):
        fp = H.cache_path(code, self.base)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"forming": list(forming), "rows": rows}), encoding="utf-8")

    def test_cache_excludes_future_and_forming_rows(self):
        self._bars("600000", [
            {"date": "2026-11-25", "close": 10},
            {"date": "2026-11-26", "close": 11},
            {"date": "2026-11-27", "close": 12},
            {"date": "2026-11-30", "close": 99},
        ], forming=["2026-11-27"])
        rows = H.cache_bars("600000", "2026-11-28", self.base)
        self.assertEqual([r["date"] for r in rows], ["2026-11-25", "2026-11-26"])

    def test_session_return_uses_target_close_and_previous_cached_close(self):
        self._bars("600000", [
            {"date": "2026-11-25", "close": 10},
            {"date": "2026-11-26", "close": 11},
            {"date": "2026-11-27", "close": 13.2},
            {"date": "2026-11-30", "close": 100},
        ])
        self.assertAlmostEqual(H.session_return("600000", "2026-11-27", self.base), 0.2)
        self.assertIsNone(H.session_return("600000", "2026-11-28", self.base))

    def test_h1_uses_cached_historical_ohlc_not_latest_snapshot(self):
        self._bars("300972", [
            {"date": "2026-11-23", "open": 10, "high": 10.2, "low": 9.8, "close": 10.1},
            {"date": "2026-11-24", "open": 10.1, "high": 10.5, "low": 10.0, "close": 10.3},
            {"date": "2026-11-27", "open": 20, "high": 30, "low": 19, "close": 25},
        ])
        self._bars("sh000300", [
            {"date": "2026-11-23", "close": 100},
            {"date": "2026-11-24", "close": 101},
        ])
        with mock.patch.object(H, "H1_SAMPLE", {"300972": "样本"}), \
                mock.patch("sys.stdout", new=io.StringIO()):
            result = H.h1_settlement("2026-11-27", self.base)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["max_gain"], 0.05)
        self.assertAlmostEqual(result[0]["close_gain"], 0.03)
        self.assertTrue(result[0]["pop"])

    def test_h1_before_settlement_is_pending(self):
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(H.h1_settlement("2026-11-23", self.base), [])

    def test_h2_kline_window_cannot_read_after_as_of(self):
        fills = self.base / "t_io" / "state" / "weak_strong_book"
        fills.mkdir(parents=True)
        (fills / "fills_2026-11-12.json").write_text(json.dumps([
            {"code": "600629", "side": "BUY", "price": 10}
        ]), encoding="utf-8")
        self._bars("600629", [
            {"date": "2026-11-12", "open": 10, "high": 10.2, "low": 9.8, "close": 10},
            {"date": "2026-11-13", "open": 10, "high": 10.4, "low": 9.7, "close": 10.2},
            {"date": "2026-11-20", "open": 20, "high": 30, "low": 5, "close": 25},
        ])
        with mock.patch.object(H, "H2_CODES", ("600629",)), \
                mock.patch.object(H, "H2_ENTRY_DATES", ("2026-11-12",)), \
                mock.patch("sys.stdout", new=io.StringIO()):
            result = H.h2_samples("2026-11-13", self.base)
        self.assertEqual(result[0]["last"], "2026-11-13")
        self.assertAlmostEqual(result[0]["mfe"], 0.04)
        self.assertAlmostEqual(result[0]["mae"], -0.03)

    def test_ma_uses_only_rows_up_to_as_of(self):
        start = datetime(2026, 9, 1)
        rows = []
        for i in range(90):
            day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append({"date": day, "close": float(i + 1)})
        self._bars("300508", rows)
        cutoff = rows[69]["date"]
        ma, close, day = H.mas("300508", cutoff, self.base)
        self.assertEqual(day, cutoff)
        self.assertEqual(close, 70)
        self.assertEqual(ma[5], 68.0)
        self.assertEqual(ma[21], 60.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
