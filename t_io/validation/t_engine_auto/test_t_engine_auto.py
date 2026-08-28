# -*- coding: utf-8 -*-
"""t_engine_auto.py 单元测试（期B 做T引擎同源验收：auto 侧适配器契约 + 决策核委托）。
unittest 可运行：python t_io/validation/t_engine_auto/test_t_engine_auto.py

sys.path 注意：本测试需让 `from config.params import PARAMS` 命中 _gm/config 包（而非
superTrader 根 config.py 模块），故 _GM 目录置于 _ROOT 之前；core 走 _ROOT（三段式第2段）。"""
import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_AUTO = os.path.join(_ROOT, "execution", "auto")
_GM = os.path.join(_AUTO, "_gm")
for _p in (_ROOT, _AUTO, _GM):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import t_engine_auto as tea  # noqa: E402
from t_engine_auto import SignalEngine, TDecisionEngine  # noqa: E402


def _mk(closes, start="2026-08-29 09:30"):
    n = len(closes)
    times = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame({
        "time": times.strftime("%Y-%m-%d %H:%M:%S"),
        "date": times.strftime("%Y-%m-%d"),
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
        "amount": [1000.0 * c for c in closes],
        "vwap": [c * 0.9995 for c in closes],
    })


def _buy_trigger_df():
    up = [10.0 + (10.5 - 10.0) * i / 189 for i in range(190)]
    down = [10.5 - (10.5 - 10.46) * i / 9 for i in range(10)]
    return _mk(up + down)


_HOLDING = {"code": "600000.SH", "type": "stock", "t_qty": 0, "qty": 0,
            "cost": 0.0, "pre_close": 10.0}
_DAILY_CTX = {"daily_status": "ok", "daily_ma5": 10.0, "daily_ma5_state": "above_ma5_trend",
              "daily_atr": 0.03, "index_regime": "range"}


class TestAdapter(unittest.TestCase):
    def test_buy_trigger_delegates_to_core(self):
        se = SignalEngine()
        self.assertIsInstance(se._core, TDecisionEngine)
        bs, ss, sig = se.evaluate("600000.SH", "浦发", _buy_trigger_df(), _HOLDING, _DAILY_CTX)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "BUY_LOW")
        self.assertEqual(bs, 100.0)
        self.assertEqual(ss, 0.0)

    def test_channel_is_auto(self):
        se = SignalEngine()
        _, _, sig = se.evaluate("600000.SH", "浦发", _buy_trigger_df(), _HOLDING, _DAILY_CTX)
        self.assertEqual(sig.channel, "auto")
        self.assertEqual(sig.score, 100.0)

    def test_last_feats_contract(self):
        """gm_main 依赖 _last_feats[code] 同一 dict 原地写 price/profit_pct/vwap。"""
        se = SignalEngine()
        se.evaluate("600000.SH", "浦发", _buy_trigger_df(), _HOLDING, _DAILY_CTX)
        _f = se._last_feats.get("600000.SH")
        self.assertIsInstance(_f, dict)
        self.assertIn("price", _f)
        self.assertIn("profit_pct", _f)

    def test_last_decision_plural_blocks(self):
        """gm_main L1319-1320 读复数 buy_blocks/sell_blocks。"""
        se = SignalEngine()
        se.evaluate("600000.SH", "浦发", _buy_trigger_df(), _HOLDING, _DAILY_CTX)
        _ld = se.last_decision.get("600000.SH")
        self.assertIn("buy_blocks", _ld)
        self.assertIn("sell_blocks", _ld)
        self.assertEqual(_ld["action"], "BUY_LOW")
        self.assertEqual(_ld["reason"], "信号触发")

    def test_record_trade_action_and_buyback(self):
        se = SignalEngine()
        r = se.record_trade_action("600000.SH", "BUY_LOW", 100, 10.46)
        self.assertIn("armed", r)
        self.assertIn("buyback_filled", r)
        ab = se.arm_awaiting_buyback("600000.SH", 10.50, 100, "SELL_HIGH")
        self.assertIsNotNone(ab)
        self.assertIn("target_price", ab)
        self.assertIn("expire_date", ab)


class TestLoader(unittest.TestCase):
    def test_decision_module_loaded(self):
        self.assertIsNotNone(tea.td)
        self.assertEqual(tea.Signal, tea.td.Signal)
        self.assertEqual(tea.TDecisionEngine, tea.td.TDecisionEngine)


if __name__ == "__main__":
    unittest.main(verbosity=2)
