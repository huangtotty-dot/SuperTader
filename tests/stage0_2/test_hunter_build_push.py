# -*- coding: utf-8 -*-
"""选股猎手 · 「建仓信号（按板块）」推送 · 候选提取单测（2026-09-21）。

## 为什么要有这个测试

owner 需求：猎手里出现「符合建仓条件」的股票时推送到飞书，按板块划分。
「符合建仓条件」= GUI「建仓」列的绿色 `x·GO`，即 `t_gui._hunter_build_conformance`
的时机门控 GO（市场有方向/多头结构/回撤到位/金叉加分）。

本测试**只测纯提取函数 `build_build_candidates`**（不联网、不发消息）。

⚠️ 绝不在此调用 `send_build_candidates` —— 那会真的往 owner 的飞书群发消息。

覆盖：只取 GO、按板块分组、板块与组内排序、细分拼接、无候选返回空、
       非列表/缺字段不炸（猎手数据来自多来源，字段可能缺）。

运行：python tests/stage0_2/test_hunter_build_push.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HUNTER = os.path.join(_ROOT, "stock_hunter")
for _p in (_ROOT, _HUNTER):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.push_feishu import build_build_candidates  # noqa: E402


def _stock(code, name, go, met, score, chg=0.0, concepts=None):
    return {"code": code, "name": name, "score": score, "change_pct": chg,
            "concepts": concepts or [], "build_go": go, "build_met": met,
            "build_reason": f"符合{met}/4"}


class TestBuildBuildCandidates(unittest.TestCase):
    def test_only_go_stocks_selected(self):
        sec = {"半导体": [
            _stock("000988", "华工科技", True, 3, 23),
            _stock("300548", "博创科技", False, 2, 11),   # 未 GO → 不应入选
            _stock("688037", "芯源微", False, 2, 12),
        ]}
        g = build_build_candidates(sec)
        self.assertEqual(len(g), 1)
        self.assertEqual([s["代码"] for s in g[0]["stocks"]], ["000988"])

    def test_grouped_by_sector(self):
        sec = {
            "半导体": [_stock("000988", "华工科技", True, 3, 23)],
            "医药": [_stock("300204", "舒泰神", True, 2, 7),
                     _stock("000538", "云南白药", True, 3, 9)],
        }
        g = build_build_candidates(sec)
        self.assertEqual([x["sector"] for x in g], ["医药", "半导体"],
                         "候选多的板块应排在前")
        self.assertEqual(len(g[0]["stocks"]), 2)
        # 组内：符合数 desc → 得分 desc
        self.assertEqual([s["代码"] for s in g[0]["stocks"]], ["000538", "300204"])

    def test_sort_within_sector_by_met_then_score(self):
        sec = {"X": [
            _stock("a", "A", True, 2, 30),
            _stock("b", "B", True, 3, 10),
            _stock("c", "C", True, 3, 20),
        ]}
        g = build_build_candidates(sec)
        self.assertEqual([s["代码"] for s in g[0]["stocks"]], ["c", "b", "a"],
                         "先按符合数 desc；同为 3/4 时按得分 desc")

    def test_concepts_joined(self):
        sec = {"医药": [_stock("000538", "云南白药", True, 3, 9,
                               concepts=["中药", "百年老字号"])]}
        g = build_build_candidates(sec)
        self.assertEqual(g[0]["stocks"][0]["细分"], "中药|百年老字号")

    def test_no_candidates_returns_empty(self):
        sec = {"半导体": [_stock("000988", "华工科技", False, 2, 23)]}
        self.assertEqual(build_build_candidates(sec), [])

    def test_robust_to_missing_or_empty(self):
        """猎手数据字段可能缺（-- 显示为 None/缺失），不能炸。"""
        for bad in (None, {}, {"板块": None}, {"板块": []},
                    {"板块": [{"code": "1"}]}):     # 缺 build_go → 视为未 GO
            with self.subTest(bad=bad):
                self.assertEqual(build_build_candidates(bad), [])

    def test_missing_optional_fields_default(self):
        sec = {"X": [{"code": "600000", "build_go": True}]}   # 只有 code + go
        g = build_build_candidates(sec)
        self.assertEqual(len(g), 1)
        s = g[0]["stocks"][0]
        self.assertEqual(s["名称"], "")
        self.assertEqual(s["总得分"], 0)
        self.assertEqual(s["符合数"], 0)
        self.assertEqual(s["细分"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
