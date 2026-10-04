# -*- coding: utf-8 -*-
"""analysis/trend30 单测（2026-10-04）——无网络，全部合成数据。

覆盖：残桩合并/布局无关的 bar 元信息/未收盘 bar 丢弃/指标口径复用/状态机语义
（确认、滞回、Supertrend 快速退出、涨跌停跳过）。运行：python tests/phase3/test_trend30.py
"""
import os
import sys
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.trend30.indicators import (  # noqa: E402
    drop_forming_bar, collapse_stubs, mark_bar_meta, add_30min_indicators,
)
from analysis.trend30.state_machine import Trend30StateMachine, Trend30Config  # noqa: E402
from analysis.trend30.adapter import state_to_trend  # noqa: E402
from analysis.index_regime import _ir_adx, _ir_atr_wilder  # noqa: E402

_LEFT10 = ["09:30", "10:00", "10:30", "11:00", "11:30", "13:00", "13:30", "14:00", "14:30", "15:00"]


def _day10(d, px, stub_vol=6e4):
    vols = [3e6, 3.2e6, 2.8e6, 3.5e6, stub_vol, 3.1e6, 3.4e6, 2.9e6, 3.6e6, 6.2e5]
    rows = []
    for lb, v in zip(_LEFT10, vols):
        rows.append({"time": f"{d} {lb}:00", "open": px, "high": px + 0.02,
                     "low": px - 0.03, "close": px, "volume": v})
    return rows


class TestIndicatorLayer(unittest.TestCase):
    def test_collapse_stubs_10_to_8(self):
        df = pd.DataFrame(_day10("2026-08-01", 10.0))
        df["time"] = pd.to_datetime(df["time"])
        c = collapse_stubs(df)
        self.assertEqual(len(c), 8, "10 根布局应合并为 8 根")
        # 15:00 残桩并进 14:30，成交量累加
        last = c.iloc[-1]
        self.assertEqual(str(pd.Timestamp(last["time"]).strftime("%H:%M")), "14:30")
        self.assertAlmostEqual(last["volume"], 3.6e6 + 6.2e5)

    def test_mark_meta_layout_agnostic(self):
        # 10 根（有 11:30/15:00 残桩，有 13:00）
        df10 = pd.DataFrame(_day10("2026-08-01", 10.0)); df10["time"] = pd.to_datetime(df10["time"])
        m10 = mark_bar_meta(collapse_stubs(df10))
        self.assertTrue(m10.iloc[0]["is_first"]); self.assertEqual(m10.iloc[0]["bar_idx_of_day"], 1)
        self.assertTrue(m10.iloc[-1]["is_last"])
        self.assertEqual(m10["is_lunch_first"].sum(), 1)
        self.assertTrue(bool(m10[m10["is_lunch_first"]].iloc[0]["time"].strftime("%H:%M") == "13:00"))
        # 9 根原生布局（无 13:00，不假定根数）
        labels9 = ["09:30", "10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
        rows = [{"time": f"2026-08-01 {lb}:00", "open": 10, "high": 10.1, "low": 9.9,
                 "close": 10, "volume": 3e6} for lb in labels9]
        df9 = pd.DataFrame(rows); df9["time"] = pd.to_datetime(df9["time"])
        m9 = mark_bar_meta(df9)
        self.assertTrue(m9.iloc[0]["is_first"]); self.assertTrue(m9.iloc[-1]["is_last"])
        self.assertEqual(m9["is_lunch_first"].sum(), 1)
        self.assertTrue(bool(m9[m9["is_lunch_first"]].iloc[0]["time"].strftime("%H:%M") == "13:30"))

    def test_drop_forming_bar(self):
        # 模拟「10:15 拉取」：数据只到 10:00 这根（仍在形成 10:00–10:30）→ 丢弃
        df = pd.DataFrame(_day10("2026-08-01", 10.0)[:2]); df["time"] = pd.to_datetime(df["time"])
        d = drop_forming_bar(df, now=pd.Timestamp("2026-08-01 10:15"))
        self.assertEqual(str(d.iloc[-1]["time"].strftime("%H:%M")), "09:30")
        # 15:30 → 末根（15:00）已收盘，不丢
        d2 = drop_forming_bar(df, now=pd.Timestamp("2026-08-01 15:30"))
        self.assertEqual(len(d2), 2)

    def test_indicators_reuse_index_regime(self):
        rng = np.random.default_rng(1)
        rows = []
        px = 20.0
        days = pd.date_range("2026-05-01", periods=40, freq="D")
        for k in range(40):
            for lb in _LEFT10:
                px += rng.normal(0.02, 0.05)
                rows.append({"time": f"{days[k].strftime('%Y-%m-%d')} {lb}:00", "open": px,
                             "high": px + 0.1, "low": px - 0.1, "close": px, "volume": 3e6})
        df = pd.DataFrame(rows); df["time"] = pd.to_datetime(df["time"])
        out = add_30min_indicators(df)
        adx_ref, _, _ = _ir_adx(df, 14)
        self.assertTrue(np.allclose(out["adx"].values, adx_ref.values, equal_nan=True),
                        "adx 必须与 _ir_adx 同口径")
        atr_ref = _ir_atr_wilder(df, 14)
        self.assertTrue(np.allclose(out["atr14"].values, atr_ref.values, equal_nan=True),
                        "atr14 必须与 _ir_atr_wilder 同口径")


def _mrow(i, adx, rising, close, ema20, ema60, st_dir=1, atr_ratio=1.0,
          weight=1.0, limit=False, er10=0.5, r2=0.7, open_=None, volume=3e6):
    _o = open_  # None=未指定（稍后按上一根收盘填充，避免把跨 bar 跳变误当缺口）
    return {"time": pd.Timestamp("2026-10-01 09:30") + pd.Timedelta(minutes=30 * i),
            "open": _o,
            "high": max(close, close if _o is None else _o) + 0.1,
            "low": min(close, close if _o is None else _o) - 0.1,
            "close": close, "volume": volume,
            "adx": adx, "adx_rising": rising, "ema20": ema20, "ema60": ema60,
            "ema_spread": (ema20 - ema60) / ema60, "st_dir": st_dir, "atr_ratio": atr_ratio,
            "weight": weight, "is_limit_locked": limit, "er10": er10, "r2": r2}


def _fill_open(rows):
    """open=None 的行按上一根收盘填充（真缺口须显式给 open_）。"""
    out, prev = [], None
    for r in rows:
        r = dict(r)
        if r.get("open") is None:
            r["open"] = prev if prev is not None else r["close"]
        out.append(r)
        prev = r["close"]
    return out


class TestStateMachine(unittest.TestCase):
    def _run(self, rows):
        sm = Trend30StateMachine()
        out = sm.run(pd.DataFrame(_fill_open(rows)))
        return sm, list(out["state"])

    def test_range_to_bull_requires_two_confirm(self):
        rows = [
            _mrow(0, np.nan, False, 10, 10, 10),
            _mrow(1, np.nan, False, 10, 10, 10),
            _mrow(2, 30, True, 11, 10.2, 10.0),   # raw BULL, cnt=1
            _mrow(3, 30, True, 11, 10.2, 10.0),   # cnt=2 → BULL
        ]
        sm, states = self._run(rows)
        self.assertEqual(states[-1], "BULL")

    def test_hysteresis_hold_between_adx_off_and_on(self):
        rows = [
            _mrow(0, np.nan, False, 10, 10, 10), _mrow(1, np.nan, False, 10, 10, 10),
            _mrow(2, 30, True, 11, 10.2, 10.0), _mrow(3, 30, True, 11, 10.2, 10.0),  # BULL
            _mrow(4, 20, False, 11, 10.2, 10.0),  # ADX 18~22 灰区、方向消失
            _mrow(5, 20, False, 11, 10.2, 10.0),
        ]
        sm, states = self._run(rows)
        self.assertEqual(states[-1], "BULL", "ADX 在 18~22 灰区且未拐头 → 维持原状态")

    def test_supertrend_break_exits_to_range_not_bear(self):
        rows = [
            _mrow(0, np.nan, False, 10, 10, 10), _mrow(1, np.nan, False, 10, 10, 10),
            _mrow(2, 30, True, 11, 10.2, 10.0), _mrow(3, 30, True, 11, 10.2, 10.0),  # BULL
            _mrow(4, 30, True, 10.8, 10.2, 10.0, st_dir=-1),  # Supertrend 破位
        ]
        sm, states = self._run(rows)
        self.assertEqual(states[-1], "RANGE", "破位应快速退回 RANGE（非直接翻空）")

    def test_limit_locked_skips_confirm(self):
        rows = [
            _mrow(0, np.nan, False, 10, 10, 10), _mrow(1, np.nan, False, 10, 10, 10),
            _mrow(2, 30, True, 11, 10.2, 10.0),            # cnt=1
            _mrow(3, 30, True, 11, 10.2, 10.0, limit=True),  # 涨跌停 → 不计确认
        ]
        sm, states = self._run(rows)
        self.assertEqual(states[-1], "RANGE", "涨跌停 bar 不计入确认根数")

    def test_zero_weight_bar_does_not_fire(self):
        rows = [
            _mrow(0, np.nan, False, 10, 10, 10), _mrow(1, np.nan, False, 10, 10, 10),
            _mrow(2, 30, True, 11, 10.2, 10.0, weight=1.0),   # cnt=1
            _mrow(3, 30, True, 11, 10.2, 10.0, weight=0.5),   # 降权 → 不触发
            _mrow(4, 30, True, 11, 10.2, 10.0, weight=1.0),   # cnt=2 → BULL
        ]
        sm, states = self._run(rows)
        self.assertEqual(states[3], "RANGE")
        self.assertEqual(states[4], "BULL")

    def test_state_to_trend_mapping(self):
        self.assertEqual(state_to_trend("BULL"), "up")
        self.assertEqual(state_to_trend("BEAR"), "down")
        self.assertEqual(state_to_trend("RANGE"), "flat")
        self.assertEqual(state_to_trend(None), "flat")


class TestGapWindow(unittest.TestCase):
    """§4.4 缺口观察窗：加速确认 / 强信号直计 / 假突破回滚。"""

    def _run(self, rows):
        sm = Trend30StateMachine()
        return sm.run(pd.DataFrame(_fill_open(rows)))

    def test_gap_aligned_accelerates_confirm(self):
        rows = [_mrow(i, np.nan, False, 10, 10, 10) for i in range(8)]
        # 上跳缺口 + 同向：req 由 2 降为 1 → 首根即确认 BULL（无放量，排除 gap_bonus）
        rows.append(_mrow(8, 30, True, 11, 10.2, 10.0, open_=11.5))
        out = self._run(rows)
        self.assertEqual(out["state"].iloc[-1], "BULL", "缺口同向应加速到单根确认")

    def test_gap_volume_strong_signal(self):
        rows = [_mrow(i, np.nan, False, 10, 10, 10) for i in range(8)]
        # 缺口 + 放量(≥1.5×MA20vol)：gap_bonus 直计 +1；此处 req=1 双保险仍应 BULL
        rows.append(_mrow(8, 30, True, 11, 10.2, 10.0, open_=11.5, volume=9e6))
        out = self._run(rows)
        self.assertEqual(out["state"].iloc[-1], "BULL")

    def test_gap_false_breakout_rollback(self):
        rows = [_mrow(i, np.nan, False, 10, 10, 10) for i in range(8)]
        rows.append(_mrow(8, 30, True, 11, 10.2, 10.0))              # RANGE 候选 cnt=1
        # 缺口中枢：上跳缺口 + 放量 + 同向 → 直接 BULL（pre_state=RANGE）
        rows.append(_mrow(9, 30, True, 11.6, 10.2, 10.0, open_=11.5, volume=9e6))
        # 窗内回补缺口（close<=ref 11）→ 假突破回滚到 RANGE
        rows.append(_mrow(10, 30, True, 10.9, 10.2, 10.0, open_=11.6))
        out = self._run(rows)
        self.assertEqual(out["state"].iloc[9], "BULL")
        self.assertEqual(out["state"].iloc[10], "RANGE", "窗内回补应回滚")

    def test_gap_valid_after_window_no_rollback(self):
        # 用 gap_watch_bars=2 的小窗，便于构造「窗满后才回补」
        rows = [_mrow(i, np.nan, False, 10, 10, 10) for i in range(8)]
        rows.append(_mrow(8, 30, True, 11, 10.2, 10.0))
        rows.append(_mrow(9, 30, True, 11.6, 10.2, 10.0, open_=11.5, volume=9e6))  # 缺口→BULL
        rows.append(_mrow(10, 30, True, 11.2, 10.2, 10.0, open_=11.6))  # 未回补
        rows.append(_mrow(11, 30, True, 11.2, 10.2, 10.0, open_=11.2))  # 未回补
        rows.append(_mrow(12, 30, True, 10.9, 10.2, 10.0, open_=11.2))  # 回补（pos 12−9=3>2）
        sm = Trend30StateMachine(Trend30Config(gap_watch_bars=2))
        out = sm.run(pd.DataFrame(_fill_open(rows)))
        self.assertEqual(out["state"].iloc[-1], "BULL", "窗满未回补=突破有效，不回滚")


if __name__ == "__main__":
    unittest.main(verbosity=2)
