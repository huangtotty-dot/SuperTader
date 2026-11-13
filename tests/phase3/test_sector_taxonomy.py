# -*- coding: utf-8 -*-
"""板块主题分类单测 —— sector_rotation/taxonomy.py（2026-11-13 补）。

`classify_theme_family` / `infer_theme_level` / `_rank_score` 是纯函数（无网络/IO），
钉住三件事：① 指数参考名不被误分类；② FAMILY_RULES 的关键词**首中即赢**（顺序敏感，
有人重排就会改分类结果）；③ 题材层级按「宽基/微型/细分」三档 + 成分数阈值划分。

运行：python tests/phase3/test_sector_taxonomy.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from sector_rotation.taxonomy import (  # noqa: E402
    _rank_score, classify_theme_family, infer_theme_level,
)


class TestClassifyThemeFamily(unittest.TestCase):
    def test_index_reference_names(self):
        for n in ("全A等权指数", "沪深主板指数", "创业板指数", "科创板指数"):
            self.assertEqual(classify_theme_family(n), "市场指数", n)

    def test_unambiguous_single_keyword(self):
        cases = {
            "白酒": "大消费",
            "半导体": "半导体电子",
            "军工": "高端制造军工",
            "银行": "金融",
            "航运": "交通物流",
            "光伏设备": "新能源电力",
            "环保": "环保公用",
            "房地产": "地产基建",
        }
        for name, fam in cases.items():
            self.assertEqual(classify_theme_family(name), fam, name)

    def test_first_match_wins(self):
        """「电力设备」同时命中 新能源电力(电力) 与 高端制造军工(设备)，须首中 → 新能源电力。

        这是 FAMILY_RULES 顺序敏感性的显式钉死：谁把「新能源电力」往后挪，这个测试就翻红。
        """
        self.assertEqual(classify_theme_family("电力设备"), "新能源电力")

    def test_unknown_falls_to_other(self):
        self.assertEqual(classify_theme_family("量子计算"), "其他主题")

    def test_empty_and_none(self):
        self.assertEqual(classify_theme_family(None), "其他主题")
        self.assertEqual(classify_theme_family("   "), "其他主题")


class TestInferThemeLevel(unittest.TestCase):
    def test_index_reference(self):
        self.assertEqual(infer_theme_level("全A等权指数", 0), "指数参考")

    def test_broad_sector_by_name(self):
        self.assertEqual(infer_theme_level("计算机", 30), "宽基主线")

    def test_broad_sector_by_count(self):
        self.assertEqual(infer_theme_level("某种题材", 100), "宽基主线")

    def test_micro_theme_low_count(self):
        self.assertEqual(infer_theme_level("某种题材", 5), "微型题材")
        self.assertEqual(infer_theme_level("某种题材", 0), "微型题材")

    def test_sub_theme_mid_count(self):
        self.assertEqual(infer_theme_level("某种题材", 30), "细分题材")

    def test_bad_count_coerced_to_zero(self):
        self.assertEqual(infer_theme_level("某种题材", "不是数字"), "微型题材")


class TestRankScore(unittest.TestCase):
    def test_single_or_no_value_is_neutral_50(self):
        self.assertEqual(list(_rank_score(pd.Series([10.0]))), [50.0])
        self.assertEqual(list(_rank_score(pd.Series([float("nan")]))), [50.0])

    def test_pct_rank_scaled_to_100(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0])
        got = _rank_score(s)
        # rank(pct=True) 是 rank/N，最小值 = 1/N*100，不是 0；与 pandas 口径逐位一致
        self.assertTrue(list(got) == list(s.rank(pct=True) * 100.0), f"与 pandas pct rank 不一致: {list(got)}")
        self.assertAlmostEqual(max(got), 100.0, places=6)
        self.assertTrue(list(got) == sorted(got), "rank 应单调不减")


if __name__ == "__main__":
    unittest.main(verbosity=2)
