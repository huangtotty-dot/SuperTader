# -*- coding: utf-8 -*-
"""黄金分割（斐波那契回撤/扩展）离线单测（2026-09-20）。

## 为什么要有这个测试

K线弹窗新增「黄金分割」图层：自动锚定某周期**最显著的一段摆动**，再按斐波那契
比例算出回撤位/扩展位。两处最容易悄悄错且肉眼难发现：

1. **锚点索引的坐标系**——`_calc_fibonacci` 只在 `tail(lookback)` 上找摆动，但前端
   `period.dates` 是**全量**序列。索引不做偏移修正就会把锚点连线画到错误的位置
   （线还在、图不报错，只是指向一段无关的行情）。
2. **回撤/扩展公式的方向**——上涨摆动与下跌摆动是镜像的，写反了会让支撑位变成
   阻力位，同样不报错。

本测试把这两条钉死。

## 怎么造出确定的摆动

`_zigzag()` 按「转折点+根数」线性拼接价格：段内单调 ⇒ 段内不产生分形极值，
转折点必然被 `_local_extrema` 检出。于是锚点落在哪个 index 是**可预测**的，
可以精确断言，不依赖真实行情。

运行：python tests/stage0_2/test_fibonacci_levels.py
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

import t_gui  # noqa: E402


def _zigzag(pivots, spread=0.005):
    """pivots: [(price, bars_to_reach_this_pivot), ...]（首项 bars 忽略）。

    段内线性插值（不含终点），最后补上末点 ⇒ 转折点正好落在累加索引上。
    返回 (df, pivot_indices)：pivot_indices[i] 是 pivots[i] 所在的 bar 索引。
    """
    prices, idxs, cur = [], [], 0
    for (p0, _), (p1, n) in zip(pivots, pivots[1:]):
        prices.extend(np.linspace(p0, p1, n, endpoint=False).tolist())
        cur += n
        idxs.append(cur)          # p1 所在索引
    prices.append(pivots[-1][0])
    rows = [{"date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
             "open": p, "close": p, "high": p * (1 + spread), "low": p * (1 - spread),
             "volume": 1e6} for i, p in enumerate(prices)]
    return pd.DataFrame(rows), [0] + idxs


class TestFibonacci(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = t_gui.Api()

    def _fib(self, df, period="daily"):
        return self.api._calc_fibonacci(df, period)

    def _assert_dates_match_indices(self, df, r):
        """锚点索引必须能索引回全量序列 —— 这是 lookback 偏移的回归点。"""
        for k in ("low", "high"):
            pt = r["swing"][k]
            self.assertEqual(
                df["date"].iloc[pt["index"]].strftime("%Y-%m-%d"), pt["date"],
                f"{k} 锚点索引 {pt['index']} 与日期 {pt['date']} 不一致（偏移未修正？）")

    def test_up_swing_anchors_and_levels(self):
        """上涨摆动（低在前、高在后）：锚点精确 + 0.618/1.618 公式正确。"""
        df, idx = _zigzag([(20, 0), (12, 30), (30, 40), (26, 20)])
        i_low, i_high = idx[1], idx[2]
        r = self._fib(df)

        self.assertTrue(r["available"])
        self.assertEqual(r["direction"], "up")
        self.assertFalse(r["fallback"])
        self.assertEqual(r["swing"]["low"]["index"], i_low)
        self.assertEqual(r["swing"]["high"]["index"], i_high)
        self._assert_dates_match_indices(df, r)

        L, H = r["swing"]["low"]["price"], r["swing"]["high"]["price"]
        by_ratio = {lv["ratio"]: lv for lv in r["levels"]}
        # 回撤位夹在摆动区间内
        for rt in t_gui._FIB_RETRACE:
            self.assertAlmostEqual(by_ratio[rt]["price"], round(H - (H - L) * rt, 3), places=3)
            self.assertLessEqual(by_ratio[rt]["price"], H + 1e-6)
            self.assertGreaterEqual(by_ratio[rt]["price"], L - 1e-6)
        # 扩展位在摆动区间上方
        for ex in t_gui._FIB_EXTENSION:
            self.assertAlmostEqual(by_ratio[ex]["price"], round(L + (H - L) * ex, 3), places=3)
            self.assertGreater(by_ratio[ex]["price"], H)
        # 黄金位唯一且为 0.618
        golden = [lv for lv in r["levels"] if lv["golden"]]
        self.assertEqual(len(golden), 1)
        self.assertAlmostEqual(golden[0]["ratio"], 0.618, places=6)
        self.assertTrue(golden[0]["label"].startswith("61.8"))
        # side 按现价分类
        cur = float(df["close"].iloc[-1])
        for lv in r["levels"]:
            self.assertEqual(lv["side"], "support" if lv["price"] < cur else "resistance")

    def test_down_swing_mirror_formula(self):
        """下跌摆动（高在前、低在后）：回撤自下而上、扩展在下方 —— 镜像公式。"""
        df, idx = _zigzag([(20, 0), (30, 40), (24, 45), (28, 15)])
        i_high, i_low = idx[1], idx[2]
        r = self._fib(df)

        self.assertTrue(r["available"])
        self.assertEqual(r["direction"], "down")
        self.assertFalse(r["fallback"])
        self.assertEqual(r["swing"]["high"]["index"], i_high)
        self.assertEqual(r["swing"]["low"]["index"], i_low)
        self._assert_dates_match_indices(df, r)

        L, H = r["swing"]["low"]["price"], r["swing"]["high"]["price"]
        by_ratio = {lv["ratio"]: lv for lv in r["levels"]}
        for rt in t_gui._FIB_RETRACE:
            self.assertAlmostEqual(by_ratio[rt]["price"], round(L + (H - L) * rt, 3), places=3)
        for ex in t_gui._FIB_EXTENSION:
            self.assertAlmostEqual(by_ratio[ex]["price"], round(H - (H - L) * ex, 3), places=3)
            self.assertLess(by_ratio[ex]["price"], L)

    def test_extension_dropped_when_nonpositive(self):
        """大幅下跌摆动时扩展位会算到 0 以下 ⇒ 必须丢弃，不能画负价。"""
        df, _ = _zigzag([(8, 0), (30, 40), (2, 45), (6, 15)])
        r = self._fib(df)

        self.assertTrue(r["available"])
        self.assertEqual(r["direction"], "down")
        self.assertTrue(all(lv["price"] > 0 for lv in r["levels"]))
        # H=30, L=2 ⇒ rng=28；1.272→-5.6、1.618→-15.3 均被丢弃
        kinds = [lv["kind"] for lv in r["levels"]]
        self.assertNotIn("extension", kinds)
        self.assertEqual(len(r["levels"]), len(t_gui._FIB_RETRACE))

    def test_fallback_when_no_pivot_at_all(self):
        """单边行情（无任何分形摆动点）⇒ 降级为回看区间高低点，仍可用。"""
        df, _ = _zigzag([(10, 0), (30, 60)])
        r = self._fib(df)

        self.assertTrue(r["available"])
        self.assertTrue(r["fallback"])
        self.assertEqual(r["direction"], "up")
        self._assert_dates_match_indices(df, r)
        self.assertEqual(r["swing"]["low"]["index"], 0)
        self.assertEqual(r["swing"]["high"]["index"], len(df) - 1)

    def test_fallback_when_all_swings_too_small(self):
        """有摆动点但幅度都低于门槛 ⇒ 同样降级（走 cands 非空那条分支）。"""
        df, _ = _zigzag([(10, 0), (10.3, 20), (10, 20), (10.3, 20)])
        r = self._fib(df)

        self.assertTrue(r["available"])
        self.assertTrue(r["fallback"])
        self.assertLess(r["swing"]["amplitude_pct"], t_gui._FIB_MIN_AMP["daily"])

    def test_index_offset_under_lookback(self):
        """总长 > lookback 时，锚点索引必须相对**全量**序列（偏移修正回归点）。"""
        # 前 230 根缓慢上行（段内单调、无分形极值）→ 把大摆动推到尾部，
        # 确保 tail(250) 会裁掉开头，从而 off > 0
        df, _ = _zigzag([(20, 0), (22, 230), (12, 30), (30, 40), (26, 20)])
        self.assertGreater(len(df), t_gui._FIB_LOOKBACK["daily"])
        r = self._fib(df)

        self.assertTrue(r["available"])
        self.assertFalse(r["fallback"])
        # 最显著的一段是 12→30（150%），不是 22→12（83%）
        self.assertEqual(r["direction"], "up")
        self.assertAlmostEqual(r["swing"]["low"]["price"], round(12 * 0.995, 3), places=3)
        self.assertAlmostEqual(r["swing"]["high"]["price"], round(30 * 1.005, 3), places=3)
        # 索引相对全量序列：必须 > lookback 裁掉的偏移量
        self.assertGreater(r["swing"]["low"]["index"], len(df) - t_gui._FIB_LOOKBACK["daily"])
        self._assert_dates_match_indices(df, r)

    def test_insufficient_sample(self):
        df, _ = _zigzag([(10, 0), (12, 3)])
        r = self._fib(df)
        self.assertFalse(r["available"])
        self.assertIn("reason", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
