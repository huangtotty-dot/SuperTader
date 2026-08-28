# -*- coding: utf-8 -*-
"""core/t_decision.py 单元测试（期B 做T引擎同源验收：Renko 触发式决策核分支覆盖 + 参数同步守卫）。
unittest 可运行：python t_io/validation/t_decision/test_t_decision.py"""
import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import t_decision as td  # noqa: E402


def _mk_minute(closes, start="2026-08-28 09:30"):
    """构造 1min df（time/open/high/low/close/volume，无 amount，测 amount 防御）。"""
    n = len(closes)
    times = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame({
        "time": times.strftime("%Y-%m-%d %H:%M"),
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
    })


def _buy_trigger_df():
    """长升(190根 10.0→10.5) + 末端小幅回落(10根 10.5→10.46)：
    造出「向下砖 + 15分MACD金叉(m15>0)」的买入触发（已实测 m15=0.0632>0, brick_dir=down）。"""
    up = [10.0 + (10.5 - 10.0) * i / 189 for i in range(190)]
    down = [10.5 - (10.5 - 10.46) * i / 9 for i in range(10)]
    return _mk_minute(up + down)


class TestResample(unittest.TestCase):
    def test_amount_defense(self):
        """df 无 amount 列时 resample 不 KeyError，且 OHLCV 正确。"""
        df = _mk_minute([10.0] * 20)
        d15 = td._resample_to_15min(df)
        self.assertGreaterEqual(len(d15), 1)
        self.assertNotIn("amount", d15.columns)  # 无 amount → 跳过该聚合

    def test_amount_present(self):
        df = _mk_minute([10.0] * 20).copy()
        df["amount"] = df["close"] * df["volume"]
        d15 = td._resample_to_15min(df)
        self.assertIn("amount", d15.columns)

    def test_insufficient_empty(self):
        self.assertTrue(td._resample_to_15min(_mk_minute([10.0] * 5)).empty)


class TestMacd(unittest.TestCase):
    def test_insufficient_zero(self):
        self.assertEqual(td._macd_hist_15m(td._resample_to_15min(_mk_minute([10.0] * 10))), 0.0)


class TestRenkoBuilder(unittest.TestCase):
    def test_first_brick_no_direction(self):
        b = td.RenkoBuilder(brick_size_pct=0.003)
        self.assertFalse(b.update("09:30", 10.0, 10.01, 9.99, 1000))
        self.assertIsNone(b.brick_direction)
        self.assertEqual(len(b.bricks), 1)

    def test_down_brick(self):
        b = td.RenkoBuilder(brick_size_pct=0.003)
        b.update("09:30", 10.0, 10.01, 9.99, 1000)   # 首砖
        self.assertTrue(b.update("09:31", 9.95, 9.96, 9.94, 1000))  # 跌破砖底
        self.assertEqual(b.brick_direction, "down")

    def test_up_brick(self):
        b = td.RenkoBuilder(brick_size_pct=0.003)
        b.update("09:30", 10.0, 10.01, 9.99, 1000)
        self.assertTrue(b.update("09:31", 10.05, 10.06, 10.04, 1000))
        self.assertEqual(b.brick_direction, "up")


class TestEvaluate(unittest.TestCase):
    def test_buy_trigger(self):
        e = td.TDecisionEngine()
        df = _buy_trigger_df()
        sig, bs, ss, reason, meta = e.evaluate(
            "600000.SH", "浦发", df, price=10.46, t_val=1300, vwap=10.48,
            today_ret=0.01, daily_status="ok", today_str="2026-08-28")
        self.assertEqual(sig.action, "BUY_LOW")
        self.assertEqual(bs, 100.0)
        self.assertEqual(ss, 0.0)
        self.assertEqual(reason, "BUY_LOW")
        self.assertEqual(meta["brick_dir"], "down")
        # 买入后记 entry
        self.assertEqual(e.t_entry_price["600000.SH"]["date"], "2026-08-28")

    def test_sell_target_take_profit(self):
        e = td.TDecisionEngine()
        e.t_entry_price["600000.SH"] = {"date": "2026-08-28", "price": 10.0, "ts": "2026-08-28 09:30"}
        df = _mk_minute([10.0] * 20 + [10.06])  # 10.06 >= 10.0*1.005
        sig, bs, ss, reason, meta = e.evaluate(
            "600000.SH", "浦发", df, price=10.06, t_val=1300, vwap=10.05,
            today_ret=0.0, daily_status="ok", today_str="2026-08-28")
        self.assertEqual(sig.action, "SELL_HIGH")
        self.assertEqual(ss, 100.0)
        self.assertIn("目标止盈", sig.reasons[0])
        self.assertNotIn("600000.SH", e.t_entry_price)  # 卖出后清 entry

    def test_sell_force_exit_tail(self):
        e = td.TDecisionEngine()
        e.t_entry_price["600000.SH"] = {"date": "2026-08-28", "price": 10.0, "ts": "2026-08-28 09:30"}
        df = _mk_minute([10.0] * 20 + [10.01])  # 10.01 < 10.05 未到止盈
        sig, bs, ss, reason, meta = e.evaluate(
            "600000.SH", "浦发", df, price=10.01, t_val=1455, vwap=10.0,
            today_ret=0.0, daily_status="ok", today_str="2026-08-28")
        self.assertEqual(sig.action, "SELL_HIGH")
        self.assertIn("尾盘强平", sig.reasons[0])

    def test_hold_no_entry_no_trigger(self):
        e = td.TDecisionEngine()
        df = _mk_minute([10.0] * 30)
        sig, bs, ss, reason, meta = e.evaluate(
            "600000.SH", "浦发", df, price=10.0, t_val=1300, vwap=10.0,
            today_ret=0.0, daily_status="ok", today_str="2026-08-28")
        self.assertIsNone(sig)
        self.assertEqual(bs, 0.0)
        self.assertEqual(ss, 0.0)
        self.assertEqual(reason, "HOLD_NO_SWING")
        self.assertIn("wait", meta)

    def test_reset_day(self):
        e = td.TDecisionEngine()
        e.t_entry_price["x"] = {"date": "2026-08-28"}
        e._renko_states["x"] = {"date": "2026-08-28"}
        e.reset_day("2026-08-29")
        self.assertEqual(e.t_entry_price, {})
        self.assertEqual(e._renko_states, {})

    def test_trace_called_on_buy(self):
        e = td.TDecisionEngine()
        events = []
        df = _buy_trigger_df()
        e.evaluate("600000.SH", "浦发", df, price=10.46, t_val=1300, vwap=10.48,
                   today_ret=0.01, daily_status="ok", today_str="2026-08-28",
                   trace=events.append)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "BUY_LOW")


class TestParamsSync(unittest.TestCase):
    def test_default_params_match_config(self):
        """DEFAULT_T_PARAMS 是 goldminer 侧的 swing_* 参数来源——必须与 config.PARAMS 同值（防双侧漂移）。"""
        from config import PARAMS
        for k in ("swing_renko_brick_pct", "swing_take_profit_pct",
                  "swing_t_max_hold_min", "swing_force_exit_tval"):
            self.assertEqual(td.DEFAULT_T_PARAMS[k], PARAMS[k], f"参数漂移: {k}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
