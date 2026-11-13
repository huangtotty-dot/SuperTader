# -*- coding: utf-8 -*-
"""板块热度追踪纯函数单测 —— stock_hunter/modules/heat_tracker.py（2026-11-13 补）。

只测无 IO 的纯逻辑：`_avg`/`_linear_slope`（回归斜率）与 `_compute_trend`（趋势标签）
+ `_get_past_scores`（历史取值 + 热度版本闸）。要钉死三件已出过 Bug 的事：
① 趋势是「前 5 日 + 今日」的回归斜率与「今日 vs 昨日」边际变化的加权，且高位滞涨降档；
② 冷启动（样本 <2）走 delta 阈值分支，样本不足显式返回 ➡️平稳而不是算错；
③ 历史取值只认**当前热度版本**（跨版本标尺不可比），旧版/缺字段条目一律跳过。

运行：python tests/phase3/test_heat_tracker.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stock_hunter.modules import heat_tracker as ht  # noqa: E402


class TestAvg(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(ht._avg([]), 0.0)

    def test_mean(self):
        self.assertEqual(ht._avg([2, 4, 6]), 4.0)


class TestLinearSlope(unittest.TestCase):
    def test_empty_or_single_is_zero(self):
        self.assertEqual(ht._linear_slope([]), 0.0)
        self.assertEqual(ht._linear_slope([5]), 0.0)

    def test_perfect_linear_slope(self):
        self.assertAlmostEqual(ht._linear_slope([10, 20, 30, 40]), 10.0)

    def test_constant_is_zero(self):
        self.assertEqual(ht._linear_slope([7, 7, 7, 7]), 0.0)

    def test_descending_negative_slope(self):
        self.assertAlmostEqual(ht._linear_slope([40, 30, 20, 10]), -10.0)


def _hist(entries_by_date):
    """构造 history dict：{date: [ {板块, 热度分, 热度版本}, ... ]}。"""
    return entries_by_date


def _row(sector, score, version=None):
    d = {"板块": sector, "热度分": score}
    if version is not None:
        d["热度版本"] = version
    return d


class TestGetPastScores(unittest.TestCase):
    def test_collects_current_version_in_order(self):
        history = {
            "d0": [_row("X", 10, 2)],
            "d1": [_row("X", 20, 2)],
            "d2": [_row("X", 30, 2)],
        }
        got = ht._get_past_scores("X", history, ["d0", "d1", "d2"], 2, 3)
        self.assertEqual(got, [10, 20, 30])

    def test_skips_wrong_version(self):
        """旧版本(v1)分值标尺不同，必须挡在回归之外（否则斜率是假的）。"""
        history = {
            "d0": [_row("X", 10, 1)],  # v1 → 跳过
            "d1": [_row("X", 20, 2)],  # v2 → 采
        }
        got = ht._get_past_scores("X", history, ["d0", "d1"], 1, 5)
        self.assertEqual(got, [20])

    def test_skips_missing_version_defaults_v1(self):
        """无「热度版本」字段的旧条目按 v1 处理 → 跳过。"""
        history = {"d0": [_row("X", 10)]}  # 无版本 → 默认 v1
        self.assertEqual(ht._get_past_scores("X", history, ["d0"], 0, 5), [])

    def test_skips_missing_score_field(self):
        history = {"d0": [{"板块": "X", "热度分": 50, "热度版本": 2}],
                   "d1": [{"板块": "X", "热度版本": 2}]}  # 缺热度分 → 跳过
        got = ht._get_past_scores("X", history, ["d0", "d1"], 1, 5)
        self.assertEqual(got, [50])


class TestComputeTrendColdStart(unittest.TestCase):
    def test_no_history_is_flat(self):
        self.assertEqual(ht._compute_trend("X", 50, {}, [], 0), "➡️平稳")

    def test_single_past_score_warm(self):
        """只有昨日一个样本 → 走 delta 阈值分支。"""
        history = {"d0": [_row("X", 50, 2)]}
        self.assertEqual(ht._compute_trend("X", 53, history, ["d0"], 1), "📈升温")
        self.assertEqual(ht._compute_trend("X", 47, history, ["d0"], 1), "📉退潮")
        self.assertEqual(ht._compute_trend("X", 50, history, ["d0"], 1), "➡️平稳")


class TestComputeTrendFull(unittest.TestCase):
    def _five(self, scores):
        """构造连续 5 个历史交易日（v2 版本）。"""
        history = {f"d{i}": [_row("X", scores[i], 2)] for i in range(5)}
        return history, [f"d{i}" for i in range(5)]

    def test_constant_flat(self):
        history, dates = self._five([50] * 5)
        self.assertEqual(ht._compute_trend("X", 50, history, dates, 5), "➡️平稳")

    def test_steady_climb_accelerates(self):
        history, dates = self._five([10, 20, 30, 40, 50])
        self.assertEqual(ht._compute_trend("X", 60, history, dates, 5), "🔥加速")


class TestEmptyHeatRow(unittest.TestCase):
    def test_structure(self):
        row = ht._empty_heat_row("某板块")
        self.assertEqual(row["板块"], "某板块")
        self.assertEqual(row["热度分"], 0)
        self.assertEqual(row["趋势"], "➡️平稳")
        self.assertEqual(row["成交额放大倍数"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
