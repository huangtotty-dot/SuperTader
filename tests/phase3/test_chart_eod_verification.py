# -*- coding: utf-8 -*-
"""W48：日线当日行必须持显式 EOD 证明，盘后时间不再是证明。"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
from core import chart_cache as cc  # noqa: E402
from core import daily_repair as dr  # noqa: E402
from core.market_data import tencent_provider as tp  # noqa: E402

_DAY = "2026-11-27"
_NOW = datetime(2026, 11, 27, 15, 30, 0)


def _df(close=10.2, volume=1000.0):
    return pd.DataFrame([{"date": _DAY, "open": 10.0, "high": 10.3,
                          "low": 9.9, "close": close, "volume": volume}])


def _bar(close=10.2, volume=1000.0):
    return {"date": _DAY, "open": 10.0, "high": 10.3, "low": 9.9,
            "close": close, "volume": volume, "amount": 10200.0}


class TestDailyEodProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_cc_dir = cc._DAILY_CACHE_DIR
        self.old_tp_dir = tp._DAILY_CACHE_DIR
        cc._DAILY_CACHE_DIR = self.root
        tp._DAILY_CACHE_DIR = str(self.root)

    def tearDown(self):
        cc._DAILY_CACHE_DIR = self.old_cc_dir
        tp._DAILY_CACHE_DIR = self.old_tp_dir
        self.tmp.cleanup()

    def _cache(self):
        return json.loads((self.root / "600000.json").read_text(encoding="utf-8"))

    def test_after_close_without_proof_stays_forming(self):
        self.assertTrue(cc.write_daily_history("600000", _df(), now=_NOW))
        got = self._cache()
        self.assertEqual(got["forming"], [_DAY])
        self.assertNotIn(_DAY, got.get("eod_verified") or [])

    def test_controlled_write_records_proof_and_source(self):
        self.assertTrue(cc.write_daily_history(
            "600000", _df(), now=_NOW, eod_verified=True,
            source="today_bar_repair"))
        got = self._cache()
        self.assertEqual(got["forming"], [])
        self.assertEqual(got["eod_verified"], [_DAY])
        self.assertEqual(got["eod_source"], {_DAY: "today_bar_repair"})

    def test_unverified_same_day_cache_is_not_skipped_after_close(self):
        (self.root / "600000.json").write_text(json.dumps({
            "date": _DAY, "saved_at": f"{_DAY} 15:30:00", "forming": [],
            "rows": [_bar()]}), encoding="utf-8")
        provider = mock.Mock()
        provider.daily_many.return_value = {}
        with mock.patch("core.market_data.get_provider", return_value=provider):
            stat = cc.prefetch_daily(["600000"], now=_NOW, retries=0, sleep_s=0)
        self.assertEqual(stat["skipped"], 0)
        self.assertEqual(stat["failed"], 1)
        provider.daily_many.assert_called_once()

    def test_verified_same_day_cache_can_be_skipped(self):
        (self.root / "600000.json").write_text(json.dumps({
            "date": _DAY, "saved_at": f"{_DAY} 15:30:00", "forming": [],
            "eod_verified": [_DAY], "rows": [_bar()]}), encoding="utf-8")
        with mock.patch("core.market_data.get_provider") as factory:
            stat = cc.prefetch_daily(["600000"], now=_NOW, retries=0, sleep_s=0)
        self.assertEqual(stat["skipped"], 1)
        factory.assert_not_called()

    def test_run_prefetch_propagates_only_explicit_proof(self):
        with mock.patch.object(cc, "_acquire_lock", return_value=True), \
             mock.patch.object(cc, "_release_lock"), \
             mock.patch.object(cc, "small_pool_codes", return_value=["600000"]), \
             mock.patch.object(cc, "mark_run_today"), \
             mock.patch.object(cc, "prefetch_daily", return_value={"got": 1}) as pf:
            cc.run_prefetch(full=False, minutes=False, force=True, now=_NOW,
                            eod_verified=True, eod_source="today_bar_repair")
        self.assertTrue(pf.call_args.kwargs["eod_verified"])
        self.assertEqual(pf.call_args.kwargs["eod_source"], "today_bar_repair")


class TestTodayRepairProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fp = Path(self.tmp.name) / "600000.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, close=10.2, volume=1000.0):
        self.fp.write_text(json.dumps({
            "date": _DAY, "saved_at": f"{_DAY} 15:20:00", "forming": [_DAY],
            "rows": [_bar(close, volume)]}), encoding="utf-8")

    def test_consistent_row_is_stamped(self):
        self._write()
        with mock.patch.object(tp, "quote_daily_bar", return_value=_bar()):
            code, status, _, _ = dr._fix_today_row_one(
                ("600000", self.fp, 10.2, 1000.0), _DAY, False, None)
        self.assertEqual((code, status), ("600000", "ok"))
        got = json.loads(self.fp.read_text(encoding="utf-8"))
        self.assertEqual(got["eod_verified"], [_DAY])
        self.assertEqual(got["forming"], [])
        self.assertEqual(got["eod_source"][_DAY], "tencent_quote_consistent")

    def test_patched_row_is_stamped(self):
        self._write(close=9.8, volume=800.0)
        with mock.patch.object(tp, "quote_daily_bar", return_value=_bar()):
            _, status, _, _ = dr._fix_today_row_one(
                ("600000", self.fp, 9.8, 800.0), _DAY, False, None)
        self.assertEqual(status, "patched")
        got = json.loads(self.fp.read_text(encoding="utf-8"))
        self.assertEqual(got["rows"][-1]["close"], 10.2)
        self.assertEqual(got["eod_source"][_DAY], "tencent_quote_patch")

    def test_dry_run_never_stamps(self):
        self._write(close=9.8, volume=800.0)
        before = self.fp.read_text(encoding="utf-8")
        with mock.patch.object(tp, "quote_daily_bar", return_value=_bar()):
            _, status, _, _ = dr._fix_today_row_one(
                ("600000", self.fp, 9.8, 800.0), _DAY, True, None)
        self.assertEqual(status, "would_patch")
        self.assertEqual(self.fp.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
