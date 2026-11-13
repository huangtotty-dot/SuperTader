# -*- coding: utf-8 -*-
"""K 线纯算法单测 —— core/chart_analytics.py（2026-11-13 补）。

只钉四个纯算法的**确定性输出契约**，不评信号效力（箱体/斐波那契已被
[[box_breakout_round2_verdict]] [[fib_time_no_predictive_power]] 研究否证，标记已下线，
故这里锁的是「输入→输出」的机械正确性，防止后续改动静默改变数值口径）：
① `_detect_boxes` 横盘带→箱体（rel=0 在箱内）、<30 行→空；
② `_detect_channel` 上行/下行/横盘方向 + 斜率 + <25 行 fail-flat + 方向反转；
③ `_calc_support_resistance` 按现价强制分类 R1/S1（上下方各保留 1 个）、全平→双空；
④ `_calc_fibonacci` 单一摆动→回撤/扩展位（golden=0.618）、单边行情 fallback、样本不足/幅度为零 fail。

运行：python tests/phase3/test_chart_analytics.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core import chart_analytics as ca  # noqa: E402


def mk(n, close_fn, high_off=0.1, low_off=0.1, start="2026-01-01"):
    """造日线 DataFrame：date/close/high/low。high=close+high_off，low=close−low_off。"""
    dates = pd.date_range(start, periods=n, freq="D")
    c = np.array([close_fn(i) for i in range(n)], dtype=float)
    return pd.DataFrame({"date": dates, "close": c, "high": c + high_off, "low": c - low_off})


class TestDetectBoxes(unittest.TestCase):
    def test_flat_band_yields_in_range_box(self):
        """横盘带 [10.0, 10.4]、close=10.2 → 检出一个箱体，现价在箱内（rel=0）。"""
        r = ca._detect_boxes(mk(40, lambda i: 10.2, high_off=0.2, low_off=0.2))
        self.assertEqual(len(r), 1)
        b = r[0]
        self.assertAlmostEqual(b["low"], 10.0, places=2)
        self.assertAlmostEqual(b["high"], 10.4, places=2)
        self.assertEqual(b["rel"], 0)  # 现价在箱体内

    def test_box_quality_fields_present(self):
        """箱体质量评级字段：宽度 3.9%<5 → 微箱；双边触及 30+30 → 极强；满质量分。"""
        b = ca._detect_boxes(mk(40, lambda i: 10.2, high_off=0.2, low_off=0.2))[0]
        self.assertEqual(b["width_grade"], "微")
        self.assertEqual(b["touch_grade"], "极强")
        self.assertEqual(b["quality_score"], 10)
        self.assertIn("微箱(3.9%)", b["display"])

    def test_short_series_returns_empty(self):
        self.assertEqual(ca._detect_boxes(mk(20, lambda i: 10.0)), [])


class TestDetectChannel(unittest.TestCase):
    def test_uptrend(self):
        r = ca._detect_channel(mk(40, lambda i: 10 + i * 0.05))
        self.assertEqual(r["direction"], "up")
        self.assertAlmostEqual(r["slope"], 0.05, places=4)
        self.assertAlmostEqual(r["ret_40d"], 19.5, places=1)
        self.assertEqual(len(r["up_line"]), 2)
        self.assertEqual(len(r["dn_line"]), 2)
        self.assertIsNone(r["reversal"])

    def test_downtrend(self):
        r = ca._detect_channel(mk(40, lambda i: 20 - i * 0.05))
        self.assertEqual(r["direction"], "down")
        self.assertAlmostEqual(r["slope"], -0.05, places=4)
        self.assertAlmostEqual(r["ret_40d"], -9.7, places=1)

    def test_flat(self):
        self.assertEqual(ca._detect_channel(mk(40, lambda i: 10.0))["direction"], "flat")

    def test_short_series_fail_flat(self):
        self.assertEqual(
            ca._detect_channel(mk(20, lambda i: 10.0)),
            {"direction": "flat", "slope": 0, "up_line": [], "dn_line": []},
        )

    def test_direction_reversal(self):
        """前 40 日下跌、后 40 日上涨 → 反转 down_to_up。"""
        r = ca._detect_channel(mk(80, lambda i: (20 - i * 0.1) if i < 40 else (16 + (i - 40) * 0.1)))
        self.assertEqual(r["direction"], "up")
        self.assertEqual(r["reversal"], "down_to_up")


def _sr_df(last=10.0, h=10.25, l=9.75):
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "close": float(last),
        "high": float(h),
        "low": float(l),
        "ma20": np.nan, "ma60": np.nan, "ma180": np.nan,
        "volume": 100000.0,
    })


class TestSupportResistance(unittest.TestCase):
    def test_pivot_classification(self):
        """last_close=10.0、H=10.25、L=9.75 → R1=10.25(阻力)、S1=9.75(支撑)，各保留 1 个。"""
        r = ca._calc_support_resistance(_sr_df())
        self.assertEqual(len(r["supports"]), 1)
        self.assertEqual(len(r["resistances"]), 1)
        s = r["supports"][0]
        self.assertAlmostEqual(s["price"], 9.75, places=2)
        self.assertEqual(s["label"], "S1中枢")
        self.assertEqual(s["strength"], 2)
        res = r["resistances"][0]
        self.assertAlmostEqual(res["price"], 10.25, places=2)
        self.assertEqual(res["label"], "R1中枢")

    def test_all_flat_returns_empty(self):
        """全平（high=low=close=10.0）→ 无候选，双方向皆空。"""
        r = ca._calc_support_resistance(_sr_df(10.0, 10.0, 10.0))
        self.assertEqual(r["supports"], [])
        self.assertEqual(r["resistances"], [])


def _rise_fall_rise(i):
    """涨(10→12)→跌(12→6)→涨(6→11)，内点恰有一峰(bar9)一谷(bar19)。"""
    if i <= 9:
        return 10 + (12 - 10) * i / 9.0
    if i <= 19:
        return 12 - (12 - 6) * (i - 9) / 10.0
    return 6 + (11 - 6) * (i - 19) / 10.0


def _v_shape(i):
    """跌(12→5)→涨(5→12.5)，单边行情（只有一谷，无内点峰）。"""
    if i <= 14:
        return 12 - 0.5 * i
    return 5 + 0.5 * (i - 14)


class TestFibonacci(unittest.TestCase):
    def test_sample_insufficient(self):
        self.assertEqual(ca._calc_fibonacci(mk(5, lambda i: 10.0), "daily"),
                         {"available": False, "reason": "样本不足"})

    def test_zero_amplitude(self):
        """high==low 的平线 → 摆动幅度为零。"""
        dates = pd.date_range("2026-01-01", periods=30, freq="D")
        c = np.full(30, 10.0)
        df = pd.DataFrame({"date": dates, "close": c, "high": c.copy(), "low": c.copy()})
        self.assertEqual(ca._calc_fibonacci(df, "daily")["reason"], "摆动幅度为零")

    def test_single_swing_levels(self):
        """跌势摆动（峰 12.1 在前、谷 5.9 在后）→ 回撤/扩展位 + 0.618 为 golden。"""
        fr = ca._calc_fibonacci(mk(30, _rise_fall_rise), "daily")
        self.assertTrue(fr["available"])
        self.assertEqual(fr["direction"], "down")
        self.assertFalse(fr["fallback"])
        sw = fr["swing"]
        self.assertAlmostEqual(sw["low"]["price"], 5.9, places=2)
        self.assertAlmostEqual(sw["high"]["price"], 12.1, places=2)
        self.assertEqual(sw["low"]["index"], 19)
        self.assertEqual(sw["high"]["index"], 9)
        self.assertAlmostEqual(sw["amplitude_pct"], 105.08, places=2)
        self.assertEqual(sw["bars"], 10)

        levels = fr["levels"]
        self.assertEqual(len(levels), 7)
        self.assertEqual(sum(1 for x in levels if x["kind"] == "retracement"), 5)
        self.assertEqual(sum(1 for x in levels if x["kind"] == "extension"), 2)
        goldens = [x for x in levels if x["golden"]]
        self.assertEqual(len(goldens), 1)
        g = goldens[0]
        self.assertEqual(g["ratio"], 0.618)
        self.assertAlmostEqual(g["price"], 9.732, places=2)
        self.assertEqual(g["label"], "61.8%")
        # 跌势所有回撤/扩展位都低于现价 11 → 全为支撑
        self.assertTrue(all(x["side"] == "support" for x in levels))

    def test_one_sided_fallback(self):
        """V 形单边行情（无内点峰）→ fallback=True，方向由低点在前判定为 up。"""
        fr = ca._calc_fibonacci(mk(30, _v_shape), "daily")
        self.assertTrue(fr["available"])
        self.assertEqual(fr["direction"], "up")
        self.assertTrue(fr["fallback"])
        self.assertAlmostEqual(fr["swing"]["low"]["price"], 4.9, places=2)
        self.assertAlmostEqual(fr["swing"]["high"]["price"], 12.6, places=2)

    def test_min_period_date_has_time(self):
        """分钟周期锚点日期须带 HH:MM，否则图上标不出取自哪根。"""
        fr = ca._calc_fibonacci(mk(30, _v_shape), "min5")
        self.assertIn(" ", fr["swing"]["low"]["date"])
        self.assertIn(":", fr["swing"]["low"]["date"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
