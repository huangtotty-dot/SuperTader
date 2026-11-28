# -*- coding: utf-8 -*-
"""W48：缺票估值 fail-closed，合法空仓仍计算。"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
import scripts.equity_daily as ed  # noqa: E402

_DATE = "2026-11-27"


class TestPartialPositionValuation(unittest.TestCase):
    def _compute(self, positions, closes):
        hb = {"cash": 5000.0, "positions": positions, "_ts": 1.0}

        def _refresh(index, date):
            return index, f"{index}:test"

        def _ret(df, date):
            return (0.01 if df == ed._BENCH_MAIN else 0.02), "ok"

        with mock.patch.multiple(
                ed,
                force_refresh_index=mock.DEFAULT,
                index_ret=mock.DEFAULT,
                heartbeat_tail=mock.DEFAULT,
                _reject_unfreeze_cash=mock.DEFAULT,
                _apply_fill_overlay=mock.DEFAULT,
                _eod_close=mock.DEFAULT,
                probe_addon=mock.DEFAULT,
                t0_realized=mock.DEFAULT,
                find_prev_equity=mock.DEFAULT) as m:
            m["force_refresh_index"].side_effect = _refresh
            m["index_ret"].side_effect = _ret
            m["heartbeat_tail"].return_value = hb
            m["_reject_unfreeze_cash"].return_value = (0.0, [])
            m["_apply_fill_overlay"].return_value = ([], None)
            m["_eod_close"].side_effect = lambda code, date: closes.get(code, (None, None))
            m["probe_addon"].return_value = (0.0, [], [])
            m["t0_realized"].return_value = (None, 0)
            m["find_prev_equity"].return_value = (1000.0, "2026-11-26")
            return ed.compute(_DATE)

    def test_missing_and_stale_prices_block_formal_equity(self):
        out = self._compute(
            {"SHSE.600001": {"qty": 100}, "SHSE.600002": {"qty": 200},
             "SHSE.600003": {"qty": 300}},
            {"600001": (10.0, "cache"),
             "600002": (13.96, "stale_bar_warn:cache(saved_at=09:43)"),
             "600003": (None, None)})
        self.assertEqual(out["data_status"], "partial_position_valuation")
        self.assertIsNone(out["market_value"])
        self.assertIsNone(out["equity"])
        self.assertIsNone(out["account_ret"])
        self.assertIsNone(out["alpha"])
        self.assertEqual(out["partial_market_value"], 1000.0)
        self.assertEqual(out["missing_prices"], ["600002", "600003"])
        self.assertIn("仅供诊断", out["source"])

    def test_empty_positions_is_valid_cash_only_account(self):
        out = self._compute({}, {})
        self.assertEqual(out["data_status"], "complete")
        self.assertEqual(out["market_value"], 0.0)
        self.assertEqual(out["equity"], 5000.0)
        self.assertEqual(out["missing_prices"], [])
        self.assertAlmostEqual(out["account_ret"], 4.0)
        self.assertAlmostEqual(out["alpha"], 3.99)


class TestBarEodIntegrity(unittest.TestCase):
    def _row(self):
        return {"date": datetime.now().strftime("%Y-%m-%d"),
                "open": 10.0, "high": 10.4, "low": 9.9,
                "close": 10.2, "volume": 1000.0}

    def test_current_day_requires_explicit_proof(self):
        row = self._row()
        day = row["date"]
        with mock.patch.object(ed, "_observed_day_high", return_value=None):
            self.assertFalse(ed._bar_complete(
                "600000", day, row, f"{day} 15:30:00", {"forming": []}))
            self.assertTrue(ed._bar_complete(
                "600000", day, row, f"{day} 15:30:00",
                {"forming": [], "eod_verified": [day]}))

    def test_forming_and_invalid_ohlc_are_rejected(self):
        row = self._row()
        day = row["date"]
        with mock.patch.object(ed, "_observed_day_high", return_value=None):
            self.assertFalse(ed._bar_complete(
                "600000", day, row, f"{day} 15:30:00",
                {"forming": [day], "eod_verified": [day]}))
            bad = dict(row, high=9.8)
            self.assertFalse(ed._bar_complete(
                "600000", day, bad, f"{day} 15:30:00",
                {"forming": [], "eod_verified": [day]}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
