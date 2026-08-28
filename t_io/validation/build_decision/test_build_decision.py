# -*- coding: utf-8 -*-
"""core/build_decision.py 单元测试（P3 建仓加仓同源验收：决策核分支覆盖 + 参数同步守卫）。
pytest / unittest 均可运行：python t_io/validation/build_decision/test_build_decision.py"""
import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import build_decision as bd  # noqa: E402


def _mk_daily(closes, highs=None, vols=None, start="2026-01-05"):
    """构造日线 df（date 连续工作日）。"""
    n = len(closes)
    dates = pd.bdate_range(start, periods=n).strftime("%Y-%m-%d")
    highs = highs or [c * 1.01 for c in closes]
    vols = vols or [1000.0] * n
    return pd.DataFrame({"date": dates, "open": closes, "high": highs,
                         "low": [c * 0.99 for c in closes], "close": closes, "volume": vols})


def _mk_index(closes, start="2026-01-05"):
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "close": closes})


def _features(**over):
    f = {"price": 10.0, "trend_multihead": True, "above_ma60": True, "drawdown": -0.01,
         "macd_golden_5d": False, "rsi": 45.0, "ma20": 9.9, "ma60": 9.8,
         "vol_ratio20": 1.0, "dist_ma60": 0.02}
    f.update(over)
    return f


class TestRegime(unittest.TestCase):
    def test_buffers(self):
        # MA60≈10 的平稳指数：close=10.06>10*1.005 → trend_up；10.02 → range；9.69<9.7 → trend_dn
        base = [10.0] * 80
        for last, expect in ((10.06, "trend_up"), (10.02, "range"), (9.69, "trend_dn")):
            df = _mk_index(base[:-1] + [last])
            r = bd.regime_from_index_daily(df, df["date"].iloc[-1])
            self.assertEqual(r["regime"], expect, f"close={last}")

    def test_insufficient_rows_unknown(self):
        df = _mk_index([10.0] * 60)
        r = bd.regime_from_index_daily(df, df["date"].iloc[-1])
        self.assertEqual(r["regime"], "unknown")

    def test_asof_no_future(self):
        # 截断日期之前的未来数据不得影响判定
        df = _mk_index([10.0] * 79 + [9.0])  # 末日大跌
        r = bd.regime_from_index_daily(df, df["date"].iloc[-2])  # as-of 倒数第二日
        self.assertNotEqual(r["close"], 9.0)


class TestFeatures(unittest.TestCase):
    def test_insufficient_returns_empty(self):
        self.assertEqual(bd.features_from_daily(_mk_daily([10.0] * 60), "2026-12-31"), {})
        self.assertEqual(bd.features_from_daily(None, "2026-12-31"), {})

    def test_basic_fields(self):
        closes = [10.0] * 79 + [11.0]
        vols = [1000.0] * 79 + [4000.0]
        f = bd.features_from_daily(_mk_daily(closes, vols=vols), "2999-01-01")
        self.assertEqual(f["price"], 11.0)
        self.assertTrue(f["trend_multihead"])
        self.assertEqual(f["vol_ratio20"], round(4000.0 / ((1000.0 * 19 + 4000.0) / 20), 2))
        self.assertIn("dist_ma60", f)
        self.assertIn("drawdown", f)


class TestTimingDecision(unittest.TestCase):
    def test_trend_up_go(self):
        d = bd.timing_decision(_features(), "trend_up")
        self.assertTrue(d["go"])
        self.assertEqual(d["veto"], [])

    def test_veto_vol_spike(self):
        d = bd.timing_decision(_features(vol_ratio20=3.0), "trend_up")
        self.assertFalse(d["go"])
        self.assertEqual(d["veto"], ["爆量3倍≥3"])

    def test_veto_dist_ma60(self):
        d = bd.timing_decision(_features(dist_ma60=0.21), "trend_up")
        self.assertFalse(d["go"])
        self.assertEqual(d["veto"], ["偏离MA60+21.0%>+20%"])

    def test_veto_not_applied_in_trend_dn(self):
        # 抄底侧爆量是恐慌出清常态，不否决
        d = bd.timing_decision(_features(drawdown=-0.15, rsi=15.0, vol_ratio20=5.0), "trend_dn")
        self.assertTrue(d["go"])
        self.assertEqual(d["veto"], [])

    def test_trend_dn_requires_deep_dd_and_rsi(self):
        self.assertTrue(bd.timing_decision(_features(drawdown=-0.11, rsi=19.9), "trend_dn")["go"])
        self.assertFalse(bd.timing_decision(_features(drawdown=-0.11, rsi=20.0), "trend_dn")["go"])
        self.assertFalse(bd.timing_decision(_features(drawdown=-0.09, rsi=15.0), "trend_dn")["go"])

    def test_range_no_go(self):
        d = bd.timing_decision(_features(), "range")
        self.assertFalse(d["go"])

    def test_trend_up_shallow_dd_required(self):
        self.assertFalse(bd.timing_decision(_features(drawdown=-0.031), "trend_up")["go"])
        self.assertFalse(bd.timing_decision(_features(trend_multihead=False), "trend_up")["go"])


class TestVerdictMapping(unittest.TestCase):
    def test_signal(self):
        self.assertEqual(bd.verdict_from_timing(True, "trend_up", _features()), ("signal", 90))
        self.assertEqual(bd.verdict_from_timing(True, "trend_up", _features(macd_golden_5d=True)), ("signal", 100))

    def test_watch_signal_range(self):
        v, s = bd.verdict_from_timing(False, "range", _features())
        self.assertEqual(v, "watch_signal")
        self.assertEqual(s, 60)  # range 市 t_regime 30 分锁死（结构30+回撤30+金叉0）
        _, s2 = bd.verdict_from_timing(False, "range", _features(macd_golden_5d=True))
        self.assertEqual(s2, 70)  # range 市 score 上限 70

    def test_approaching(self):
        v, _ = bd.verdict_from_timing(False, "trend_up", _features(trend_multihead=True, drawdown=-0.05))
        self.assertEqual(v, "approaching")  # 有方向且结构过（回撤未过）

    def test_weak(self):
        v, _ = bd.verdict_from_timing(False, "trend_up", _features(trend_multihead=False, drawdown=-0.05))
        self.assertEqual(v, "weak")

    def test_data_insufficient_fail_closed(self):
        # 数据不足：结构/回撤一律不通过，不得伪装成条件满足
        v, s = bd.verdict_from_timing(False, "trend_up", {}, data_insufficient=True)
        self.assertEqual(v, "weak")
        self.assertEqual(s, 30)  # 仅方向分


class TestIntradayConfirm(unittest.TestCase):
    """W35 日内确认：金样（2026-08-28 捕获自重构前 check_intraday_confirm，须逐字一致）。"""

    def _mk(self, closes, vols, start="2026-08-27 09:30"):
        times = pd.date_range(start, periods=len(closes), freq="1min")
        return pd.DataFrame({"time": times.strftime("%Y-%m-%d %H:%M"), "open": closes,
                             "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
                             "close": closes, "volume": vols,
                             "amount": [c * v for c, v in zip(closes, vols)]})

    def test_golden_up_vol(self):
        r = bd.intraday_confirm(self._mk([10 + 0.01 * i for i in range(120)], [1000] * 115 + [3000] * 5))
        self.assertEqual(r, (True, "15分钟确认: 站上EMA8=True(c=11.190/ema8=10.755) "
                                   "放量=True(量比1.43>1.2) 站上VWAP=True(vwap=10.639)", False))

    def test_golden_down(self):
        r = bd.intraday_confirm(self._mk([10 - 0.01 * i for i in range(120)], [1000] * 120))
        self.assertEqual(r, (False, "15分钟确认: 站上EMA8=False(c=8.810/ema8=9.245) "
                                    "放量=False(量比1.00>1.2) 站上VWAP=False(vwap=9.405)", False))

    def test_golden_insufficient(self):
        r = bd.intraday_confirm(self._mk([10] * 10, [1000] * 10))
        self.assertEqual(r, (False, "日内分钟数据不足", True))

    def test_golden_flat(self):
        r = bd.intraday_confirm(self._mk([10.0] * 120, [1000] * 120))
        self.assertEqual(r, (False, "15分钟确认: 站上EMA8=False(c=10.000/ema8=10.000) "
                                    "放量=False(量比1.00>1.2) 站上VWAP=True(vwap=10.000)", False))


class TestParamsSync(unittest.TestCase):
    def test_default_params_match_config(self):
        """DEFAULT_TIMING_PARAMS 是 goldminer 侧的参数来源——必须与 config.ENTRY_TIMING_PARAMS
        的关键键同值（防双侧参数漂移）。"""
        from config import ENTRY_TIMING_PARAMS as etp
        for k in ("regime_up_buffer", "trend_dn_rsi_max", "intraday_confirm_gate",
                  "intraday_confirm_vol_min", "veto_vol_spike", "veto_dist_ma60_max"):
            self.assertEqual(bd.DEFAULT_TIMING_PARAMS[k], etp[k], f"参数漂移: {k}")


class TestDdThreshold(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(bd.dd_threshold_ok(-0.03, "trend_up"))
        self.assertFalse(bd.dd_threshold_ok(-0.031, "trend_up"))
        self.assertTrue(bd.dd_threshold_ok(-0.101, "trend_dn"))
        self.assertFalse(bd.dd_threshold_ok(-0.10, "trend_dn"))
        self.assertTrue(bd.dd_threshold_ok(-0.03, "range"))   # B-4: range 用多头口径
        self.assertTrue(bd.dd_threshold_ok(-0.03, "unknown"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
