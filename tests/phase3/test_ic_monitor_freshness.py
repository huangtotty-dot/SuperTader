# -*- coding: utf-8 -*-
"""W48 IC 监控面板新鲜度（临时 CSV/JSON，全离线，不更新生产 panel）。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from t_io.validation.factor_mining import ic_monitor as IM  # noqa: E402


class TestICMonitorFreshness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self._patchers = [
            mock.patch.object(IM, "ROLL_CSV", self.out / "roll60_ic.csv"),
            mock.patch.object(IM, "STATUS_JSON", self.out / "monitor_status.json"),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        self._tmp.cleanup()

    def _store(self, ic_end):
        idx = pd.bdate_range(end=pd.Timestamp(ic_end), periods=70)
        return pd.DataFrame({
            f: ([-0.01] * len(idx) if f == "REV10" else [0.01] * len(idx))
            for f in IM.FACTORS
        }, index=idx)

    def _build(self, panel_end, expected="2026-11-27", ic_end="2026-11-18"):
        return IM.build_monitor(
            self._store(ic_end), pd.Timestamp(ic_end), pd.Timestamp(expected),
            warn_days=10, alarm_days=20,
            panel_end=(pd.Timestamp(panel_end) if panel_end is not None else None),
            expected_panel_end=pd.Timestamp(expected))

    def test_stale_panel_disables_weight_advice_even_with_historical_alarm(self):
        status = self._build("2026-11-17")
        self.assertTrue(status["stale"])
        self.assertEqual(status["data_status"], "stale_panel")
        self.assertEqual(status["panel_end"], "2026-11-17")
        self.assertEqual(status["expected_panel_end"], "2026-11-27")
        self.assertFalse(status["overall"]["production_weight_change_allowed"])
        self.assertFalse(status["overall"]["any_alarm"])
        self.assertEqual(status["overall"]["historical_alarm_factors"], ["REV10"])
        self.assertIn("禁止据此调整生产权重", status["overall"]["action"])
        self.assertTrue(all(s["suggested_weight_mult"] is None
                            for s in status["factors"].values()))
        persisted = json.loads((self.out / "monitor_status.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["data_status"], "stale_panel")

    def test_fresh_panel_allows_advice_and_h5_lag_is_not_stale(self):
        status = self._build("2026-11-27", ic_end="2026-11-18")
        self.assertFalse(status["stale"])
        self.assertEqual(status["data_status"], "ready")
        self.assertEqual(status["ic_end"], "2026-11-18")
        self.assertEqual(status["panel_end"], "2026-11-27")
        self.assertTrue(status["overall"]["production_weight_change_allowed"])
        self.assertTrue(status["overall"]["any_alarm"])
        self.assertEqual(status["factors"]["REV10"]["suggested_weight_mult"], 0.5)
        self.assertEqual(status["factors"]["GAP"]["suggested_weight_mult"], 1.0)

    def test_unknown_panel_end_fails_closed(self):
        status = self._build(None)
        self.assertTrue(status["stale"])
        self.assertEqual(status["data_status"], "panel_end_unknown")
        self.assertIsNone(status["panel_end"])
        self.assertFalse(status["overall"]["production_weight_change_allowed"])
        self.assertTrue(all(s["suggested_weight_mult"] is None
                            for s in status["factors"].values()))

    def test_panel_ahead_of_as_of_fails_closed_without_calling_it_stale(self):
        status = self._build("2026-11-28")
        self.assertFalse(status["stale"])
        self.assertEqual(status["data_status"], "panel_ahead_of_as_of")
        self.assertFalse(status["overall"]["production_weight_change_allowed"])
        self.assertTrue(all(s["suggested_weight_mult"] is None
                            for s in status["factors"].values()))

    def test_calendar_unverified_disables_weight_advice_even_when_panel_matches(self):
        status = IM.build_monitor(
            self._store("2026-11-18"), pd.Timestamp("2026-11-18"),
            pd.Timestamp("2026-11-27"), warn_days=10, alarm_days=20,
            panel_end=pd.Timestamp("2026-11-27"),
            expected_panel_end=pd.Timestamp("2026-11-27"),
            calendar_verified=False, calendar_source="weekday_fallback_unverified",
            calendar_reason="local_index_calendar_unavailable")
        self.assertEqual(status["data_status"], "calendar_unverified")
        self.assertFalse(status["calendar"]["verified"])
        self.assertFalse(status["overall"]["production_weight_change_allowed"])
        self.assertIn("交易日历未核验", status["overall"]["action"])

    def test_default_as_of_uses_cached_sessions_for_weekends_and_holidays(self):
        cache = self.out / "daily"
        cache.mkdir()
        (cache / "index_sh000300.json").write_text(json.dumps({
            "rows": [{"date": "2026-09-30", "close": 4500},
                     {"date": "2026-11-26", "close": 4600},
                     {"date": "2026-11-27", "close": 4700},
                     {"date": "2026-12-01", "close": 4800}]
        }), encoding="utf-8")
        self.assertEqual(str(IM._default_as_of("2026-11-28", cache).date()), "2026-11-27")
        self.assertEqual(str(IM._default_as_of("2026-11-29", cache).date()), "2026-11-27")
        self.assertEqual(str(IM._default_as_of("2026-11-30", cache).date()), "2026-11-27")
        holiday = IM.resolve_as_of("2026-10-08", cache)
        self.assertEqual(str(holiday["as_of"].date()), "2026-09-30")
        self.assertTrue(holiday["calendar_verified"])
        self.assertEqual(holiday["calendar_source"], "local_index_daily:sh000300")

    def test_forming_or_unverified_current_day_is_not_completed_session(self):
        cache = self.out / "forming"
        cache.mkdir()
        (cache / "index_sh000300.json").write_text(json.dumps({
            "forming": ["2026-11-27"],
            "eod_verified": [],
            "rows": [{"date": "2026-11-26", "close": 4600},
                     {"date": "2026-11-27", "close": 4700}]
        }), encoding="utf-8")
        got = IM.resolve_as_of("2026-11-27", cache)
        self.assertEqual(str(got["as_of"].date()), "2026-11-26")

    def test_missing_cache_is_explicitly_unverified(self):
        got = IM.resolve_as_of("2026-11-30", self.out / "missing")
        self.assertEqual(str(got["as_of"].date()), "2026-11-30")
        self.assertFalse(got["calendar_verified"])
        self.assertEqual(got["calendar_source"], "weekday_fallback_unverified")


if __name__ == "__main__":
    unittest.main()
