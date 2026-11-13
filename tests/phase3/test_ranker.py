# -*- coding: utf-8 -*-
"""排名与 TOP5 生成单测 —— stock_hunter/modules/ranker.py（2026-11-13 补）。

纯函数模块（只依赖 pandas），钉住报告生成的核心排序规则：
① ScoreRanker 按总得分降序 + 涨停家数/成交额/细分数量逐级打破平局，字符串数值可容错转 float；
② Top5Ranker 的去重（同代码留最高分）→ 板块覆盖（同板块最多 max_same_sector=2）→
   多样性补充（覆盖 <3 板块则从备选补不同板块）→ 强制填充至 top_count=5 的整条流水线；
③ EnhancedTop5Ranker 的增强得分数学（热度/稀缺/成交额加权）与原得分保留字段。

运行：python tests/phase3/test_ranker.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from stock_hunter.modules.ranker import (  # noqa: E402
    DetailRanker, EnhancedTop5Ranker, ScoreRanker, Top5Ranker,
)


def _stock(code, score, sector=None, **kw):
    d = {"代码": code, "总得分": score}
    if sector is not None:
        d["所属板块"] = sector
    d.update(kw)
    return d


class TestScoreRanker(unittest.TestCase):
    def test_sort_by_score_desc(self):
        items = [_stock("1", 50), _stock("2", 90), _stock("3", 70)]
        codes = [i["代码"] for i in ScoreRanker().rank(items)]
        self.assertEqual(codes, ["2", "3", "1"])

    def test_tie_break_by_涨停家数(self):
        items = [
            _stock("1", 50, 涨停家数=1),
            _stock("2", 50, 涨停家数=3),
            _stock("3", 50, 涨停家数=2),
        ]
        codes = [i["代码"] for i in ScoreRanker().rank(items)]
        self.assertEqual(codes, ["2", "3", "1"])

    def test_tie_break_by_成交额_then_细分数量(self):
        items = [
            _stock("1", 50, 涨停家数=1, 成交额=100, 细分数量=5),
            _stock("2", 50, 涨停家数=1, 成交额=100, 细分数量=9),
            _stock("3", 50, 涨停家数=1, 成交额=300, 细分数量=0),
        ]
        codes = [i["代码"] for i in ScoreRanker().rank(items)]
        self.assertEqual(codes, ["3", "2", "1"])

    def test_string_numeric_coerced(self):
        """成交额以字符串传入时仍按数值参与排序（不是把 "300" 当字符串比较）。"""
        items = [
            _stock("1", 50, 涨停家数=1, 成交额="9"),
            _stock("2", 50, 涨停家数=1, 成交额="100"),
        ]
        codes = [i["代码"] for i in ScoreRanker().rank(items)]
        self.assertEqual(codes, ["2", "1"])

    def test_bad_string_falls_to_zero(self):
        items = [
            _stock("1", 50, 涨停家数=1, 成交额="oops"),
            _stock("2", 50, 涨停家数=1, 成交额=5),
        ]
        codes = [i["代码"] for i in ScoreRanker().rank(items)]
        self.assertEqual(codes, ["2", "1"])


class TestTop5Ranker(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(Top5Ranker().select([]), [])

    def test_dedup_keeps_highest_score(self):
        stocks = [_stock("1", 100, "X"), _stock("1", 90, "X"), _stock("2", 80, "Y")]
        result = Top5Ranker().select(stocks)
        codes = [s["代码"] for s in result]
        self.assertEqual(codes, ["1", "2"])
        self.assertEqual(result[0]["总得分"], 100)

    def test_sector_cap_two_then_force_fill(self):
        """同板块最多 2 个：第 3 个进备选，最终强制填充仍回到前 5。"""
        stocks = [
            _stock("1", 100, "X"),
            _stock("2", 90, "X"),
            _stock("3", 80, "X"),  # X 的第 3 个 → 备选
            _stock("4", 70, "Y"),
            _stock("5", 60, "Z"),
        ]
        result = Top5Ranker().select(stocks)
        codes = [s["代码"] for s in result]
        # 前两轮：1,2(X) 4(Y) 5(Z)；多样性已 3 板块满足；不足 5 则从备选补 3
        self.assertEqual(codes, ["1", "2", "4", "5", "3"])

    def test_diversity_pulls_new_sector(self):
        """覆盖板块不足 min_diversity=3 时，从备选补一个不同板块。"""
        stocks = [
            _stock("1", 100, "X"),
            _stock("2", 90, "X"),
            _stock("3", 80, "Y"),
            _stock("4", 70, "Y"),
            _stock("5", 60, "Z"),  # 新板块，优先级高于同板块的 6
            _stock("6", 50, "X"),
        ]
        result = Top5Ranker().select(stocks)
        codes = [s["代码"] for s in result]
        # 选择流：1(X) 2(X) → X 满 2；3(Y) 4(Y) → Y 满 2；5(Z) → 5 条截断
        self.assertEqual(codes[:5], ["1", "2", "3", "4", "5"])

    def test_renumbered_from_one(self):
        stocks = [_stock(str(i), 100 - i, f"S{i % 3}") for i in range(1, 8)]
        result = Top5Ranker().select(stocks)
        self.assertEqual(len(result), 5)
        self.assertEqual([s["排名"] for s in result], [1, 2, 3, 4, 5])

    def test_missing_sector_defaults_unknown(self):
        """无「所属板块/板块」字段的条目按「未知」板块处理，不崩。"""
        stocks = [_stock("1", 100), _stock("2", 90), _stock("3", 80)]
        result = Top5Ranker().select(stocks)
        self.assertEqual([s["代码"] for s in result], ["1", "2", "3"])


class TestEnhancedTop5Ranker(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(EnhancedTop5Ranker().select([]), [])

    def test_enhanced_score_math_and_original_preserved(self):
        """无热度表时热度默认 30/100=0.3，稀缺 1/10=0.1，成交额 0 → 精确加权数学。"""
        stocks = [_stock("1", 100, "X", 暗线概念数=1, 成交额=0)]
        result = EnhancedTop5Ranker().select(stocks)
        self.assertEqual(len(result), 1)
        s = result[0]
        # 100*0.5 + 0.3*37*0.3 + 0.1*37*0.15 + 0 = 50 + 3.33 + 0.555 = 53.885 → 53.9
        self.assertAlmostEqual(s["总得分"], 53.9, places=1)
        self.assertEqual(s["原始得分"], 100)
        self.assertEqual(s["热度加成%"], 30.0)
        self.assertEqual(s["概念加成%"], 10.0)

    def test_heat_map_boosts_sector(self):
        """提供 summary_df 后，热度分/趋势进入查找表并抬高对应板块标的得分。"""
        stocks = [_stock("1", 100, "X", 暗线概念数=1, 成交额=0)]
        summary = pd.DataFrame([{"板块": "X", "热度分": 100.0, "趋势": "🔥加速"}])
        result = EnhancedTop5Ranker().select(stocks, summary_df=summary)
        s = result[0]
        self.assertEqual(s["板块热度"], 100.0)
        self.assertEqual(s["板块趋势"], "🔥加速")
        # heat_bonus=1.0 → 100*0.5 + 1.0*37*0.3 + 0.1*37*0.15 = 50 + 11.1 + 0.555 = 61.655 → 61.7
        self.assertAlmostEqual(s["总得分"], 61.7, places=1)


class TestDetailRanker(unittest.TestCase):
    def test_rank_by_strongest_stock(self):
        rows = [
            {"概念": "A", "最强股得分": 50},
            {"概念": "B", "最强股得分": 90},
            {"概念": "C", "最强股得分": 70},
        ]
        ranked = DetailRanker().rank_by_strongest_stock(rows)
        self.assertEqual([r["概念"] for r in ranked], ["B", "C", "A"])

    def test_fallback_to_最高分(self):
        rows = [
            {"概念": "A", "最高分": 10},
            {"概念": "B", "最高分": 30},
        ]
        ranked = DetailRanker().rank_by_strongest_stock(rows)
        self.assertEqual([r["概念"] for r in ranked], ["B", "A"])

    def test_build_detail_rows_returns_dataframe_renumbered(self):
        rows = [{"概念": "A", "最强股得分": 1}, {"概念": "B", "最强股得分": 2}]
        df = DetailRanker().build_detail_rows(rows)
        self.assertIsInstance(df, pd.DataFrame)
        self.assertEqual(list(df["排名"]), [1, 2])
        self.assertEqual(list(df["概念"]), ["B", "A"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
