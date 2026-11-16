# -*- coding: utf-8 -*-
"""`compute_concept_top3` 的概念列口径 / 空池兜底（2026-11-16，全离线）。

背景（owner 报「尾盘市场温度不更新」）：`execution/daily_sentiment.py` 原来按
`概念板块 != ""` 过滤，而 report_gen 的 DataLoader 现在把分类放在 `韭研分类`/`韭研概念`，
`概念板块`（旧 concept_boards）恒空 ⇒ 过滤后 0 行 ⇒ `fetch_for_date([])` 里
`ThreadPoolExecutor(max_workers=min(N, 0))` 抛 ValueError ⇒ 14:30 尾盘任务整个中断，
e5 涨跌停缓存 / sentiment_daily.jsonl 都没有当天行。

这里钉住两条契约：
  A. 概念列按**实际有值的列**取（韭研概念 > 概念板块 > concept），旧档仍可用；
  B. 一列都没有 / 池子为空 ⇒ 直接返回 0，**不出网、不抛**（不再让异常吞掉整张日档）。

运行：python -m unittest tests.phase3.test_daily_sentiment_concept_pool -v
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import execution.daily_sentiment as ds  # noqa: E402


def _fake_mods(df, seen):
    """替掉 report_gen 的三个模块：DataLoader / MarketDataFetcher / ConceptScorer。"""

    class DataLoader:
        def __init__(self, *a, **k):
            pass

        def load_watchlist(self):
            return df

    class MarketDataFetcher:
        def __init__(self, *a, **k):
            self.last_failed = []

        def fetch_for_date(self, codes, date_str):
            seen["codes"] = list(codes)
            if not codes:                      # 生产里就是这样炸的
                raise ValueError("max_workers must be greater than 0")
            return pd.DataFrame({"代码": list(codes), "涨跌幅": [1.0] * len(codes)})

    class ConceptScorer:
        def __init__(self, dimensions=None):
            pass

        def compute_batch(self, stock_list):
            out = []
            for s in stock_list:
                c = dict(s)
                c["总得分"] = 10
                out.append(c)
            return out

    return {"rg_data_loader": types.SimpleNamespace(DataLoader=DataLoader),
            "rg_market_data": types.SimpleNamespace(MarketDataFetcher=MarketDataFetcher),
            "rg_scorer": types.SimpleNamespace(ConceptScorer=ConceptScorer)}


class TestConceptPoolColumns(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.params = {"report_gen_dir": self.td}
        self.seen = {}

    def _run(self, df):
        with mock.patch.object(ds, "_load_report_gen_modules",
                               side_effect=lambda _d: _fake_mods(df, self.seen)):
            return ds.compute_concept_top3("2026-11-16", params=self.params)

    def test_jiuyan_concept_column_is_used(self):
        """新档（韭研分类/韭研概念有值、概念板块空）→ 打分池非空，行情按这批代码取。"""
        df = pd.DataFrame({
            "代码": ["000001", "000002", "000003"],
            "概念板块": ["", "", ""],
            "韭研分类": ["AI应用", "半导体", ""],
            "韭研概念": ["AI游戏", "存储", ""],
        })
        r = self._run(df)
        self.assertEqual(r["pool_rows"], 2, "应按 韭研概念 非空过滤出 2 行")
        self.assertEqual(sorted(self.seen["codes"]), ["000001", "000002"])
        self.assertTrue(r["top3_names"], "有池子时 TOP3 应能算出来")

    def test_legacy_concept_board_column_still_works(self):
        """旧档（只有 概念板块）照旧可用。"""
        df = pd.DataFrame({
            "代码": ["000001"],
            "概念板块": ["AI应用"],
            "韭研分类": [""],
            "韭研概念": [""],
        })
        r = self._run(df)
        self.assertEqual(r["pool_rows"], 1)
        self.assertEqual(self.seen["codes"], ["000001"])

    def test_no_concept_column_no_network(self):
        """一列概念都没有 → 记 0 返回，不调 fetch（原来在这里抛 ValueError）。"""
        df = pd.DataFrame({"代码": ["000001", "000002"], "名称": ["A", "B"]})
        r = self._run(df)
        self.assertEqual(r["pool_rows"], 0)
        self.assertEqual(r["top3_avg"], 0.0)
        self.assertNotIn("codes", self.seen, "空池不许出网")


if __name__ == "__main__":
    unittest.main(verbosity=2)
