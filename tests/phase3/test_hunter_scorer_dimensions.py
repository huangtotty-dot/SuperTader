# -*- coding: utf-8 -*-
"""选股猎手 · D1~D12 打分维度（2026-10-17 新增 D12 趋势性）。

钉住三件事：
  ① **启用集合与满分**：config 里启用 11 项、满分 47（D12=8）。任何一项权重被改、
     或被误加进/移出 config，这里立刻红。
  ② **D12 的判定**：站上 20 日线 = 8 分；未站上/字段缺失 = 0 分（缺失不能抛）。
  ③ **GUI 的「D分」明细映射**：`t_gui._score_dims` 把 "D12趋势性" 归成 "D12"、
     "大成交额额外加分" 归成 "大额"，且**只留非零项**。

运行：python -m unittest tests.phase3.test_hunter_scorer_dimensions -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "stock_hunter"))

from modules.scorer import ConceptScorer, D12趋势性Scorer       # noqa: E402

MAX_BY_DIM = {
    "D1强势形态且新高": 8, "D2强势形态": 6, "D4首板资金池": 3,
    "D5潜在突破10日": 2, "D6潜在突破5日": 1, "D7持续性": 5,
    "D8情绪分数": 6, "D9活跃程度": 3, "D11刚站上5日线": 2,
    "D12趋势性": 8, "大成交额额外加分": 3,
}


def _cfg_dims():
    cfg = json.loads((_ROOT / "stock_hunter" / "config.json").read_text(encoding="utf-8"))
    return cfg["scoring"]["dimensions"]


class TestEnabledDimensions(unittest.TestCase):
    def test_enabled_set_and_max_total(self):
        sc = ConceptScorer(dimensions=_cfg_dims())
        names = [s.name for s in sc.scorers]
        self.assertEqual(sorted(names), sorted(MAX_BY_DIM), "启用维度集合变了")
        self.assertEqual(sum(MAX_BY_DIM[n] for n in names), 47, "满分应为 47")

    def test_d12_enabled(self):
        self.assertIn("D12趋势性", _cfg_dims())


class TestD12Trend(unittest.TestCase):
    def test_above_ma20_scores_8(self):
        score, detail = D12趋势性Scorer().compute({"站上20日线": 1})
        self.assertEqual(score, 8)
        self.assertIn("8分", detail)

    def test_below_ma20_scores_0(self):
        self.assertEqual(D12趋势性Scorer().compute({"站上20日线": 0})[0], 0)

    def test_missing_field_is_zero_not_raise(self):
        self.assertEqual(D12趋势性Scorer().compute({})[0], 0)
        self.assertEqual(D12趋势性Scorer().compute({"站上20日线": None})[0], 0)

    def test_only_d12_contributes_when_others_zero(self):
        """一只只站上 MA20、其余条件都不满足的票，总分必须正好 8。"""
        sc = ConceptScorer(dimensions=_cfg_dims())
        stock = {"代码": "600001", "站上20日线": 1}
        total, details, _ = sc.compute(stock)
        self.assertEqual(total, 8)
        self.assertEqual(details["D12趋势性"], 8)
        self.assertEqual(sum(v for k, v in details.items() if k != "D12趋势性"), 0)


class TestScoreDimsMapping(unittest.TestCase):
    """GUI 的「D分」小字用的映射（后端 `_score_dims`）。"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_tg_for_test", _ROOT / "t_gui.py")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        self.api = mod.Api()

    def test_short_names_and_nonzero_only(self):
        got = self.api._score_dims({
            "总得分": 19, "D1强势形态且新高": 8, "D2强势形态": 0,
            "D11刚站上5日线": 2, "D12趋势性": 8, "大成交额额外加分": 1,
        })
        self.assertEqual(got, {"D1": 8, "D11": 2, "D12": 8, "大额": 1})

    def test_all_zero_and_garbage(self):
        self.assertEqual(self.api._score_dims({"D12趋势性": 0}), {})
        self.assertEqual(self.api._score_dims({"D12趋势性": None}), {})
        self.assertEqual(self.api._score_dims({}), {})
        self.assertEqual(self.api._score_dims(None), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
