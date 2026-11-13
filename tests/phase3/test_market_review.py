# -*- coding: utf-8 -*-
"""大盘复盘纯函数单测 —— core/market_review.py（2026-11-13 补，只测无网络/无 LLM 的部分）。

复盘主体走 LLM（见 [[daily_review_two_systems]]），这里只钉四段**确定性**逻辑：
① `_resample_minutes`：1min → 5/15/30/60 OHLCV 聚合（open=first/high=max/low=min/
   close=last/volume=sum，time 转字符串）；② `format_data_gate`：数据可用性前置闸横幅渲染
   （缺源高亮 + 补跑命令，禁止编造结论）；③ `_pick_sentiment_record`：情绪记录取**字段最全**
   的一条（防 index_regime 补跑时追加的残缺记录遮蔽完整记录）；④ `_load_holdings`：手动台账
   只读，qty>0 才收，做T模式从 per_stock 合并。

运行：python tests/phase3/test_market_review.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import core.market_review as mr  # noqa: E402


class TestResampleMinutes(unittest.TestCase):
    def _df(self):
        return pd.DataFrame({
            "time": [f"2026-09-19 09:{30 + i:02d}:00" for i in range(6)],
            "open": [100 + i for i in range(6)],
            "high": [101 + i for i in range(6)],
            "low": [99 + i for i in range(6)],
            "close": [100.5 + i for i in range(6)],
            "volume": [10 + 10 * i for i in range(6)],
        })

    def test_five_min_aggregation(self):
        recs = mr._resample_minutes(self._df(), "5min")
        self.assertEqual(len(recs), 2)  # 09:30-09:34 一档，09:35 一档
        b0, b1 = recs[0], recs[1]
        self.assertEqual(b0["open"], 100.0)         # first
        self.assertEqual(b0["high"], 105.0)         # max
        self.assertEqual(b0["low"], 99.0)           # min
        self.assertEqual(b0["close"], 104.5)        # last
        self.assertEqual(b0["volume"], 150.0)       # 10+20+30+40+50
        self.assertEqual(b0["time"], "2026-09-19 09:30:00")
        self.assertEqual(b1["open"], 105.0)
        self.assertEqual(b1["volume"], 60.0)

    def test_bad_input_returns_empty(self):
        self.assertEqual(mr._resample_minutes(pd.DataFrame({"a": [1]}), "5min"), [])


class TestFormatDataGate(unittest.TestCase):
    def test_all_ok(self):
        health = {"指数日线": {"ok": True, "reason": ""}, "情绪": {"ok": True, "reason": ""}}
        out = mr.format_data_gate(health, "2026-09-19")
        self.assertIn("全部数据源可用", out)
        self.assertNotIn("禁止编造", out)

    def test_missing_highlighted_with_cmd(self):
        health = {
            "情绪": {"ok": False, "reason": "无当日记录", "cmd": "python execution/daily_sentiment.py --date 2026-09-19"},
            "指数日线": {"ok": True, "reason": ""},
        }
        out = mr.format_data_gate(health, "2026-09-19")
        self.assertIn("⚠️", out)
        self.assertIn("缺失 1 项（情绪）", out)
        self.assertIn("禁止编造", out)
        self.assertIn("补跑: python execution/daily_sentiment.py", out)


class TestPickSentimentRecord(unittest.TestCase):
    def setUp(self):
        self._saved_base = mr.BASE
        self._tmp = tempfile.TemporaryDirectory()
        mr.BASE = Path(self._tmp.name)

    def tearDown(self):
        mr.BASE = self._saved_base
        self._tmp.cleanup()

    def _write_jsonl(self, lines):
        fp = Path(self._tmp.name) / "t_io" / "logs" / "sentiment_daily.jsonl"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines),
                      encoding="utf-8")

    def test_picks_most_complete_not_last(self):
        complete = {"date": "2026-09-24", "z_S": 1.2, "top3_avg": 3.4, "z_top3": 0.5,
                    "top3_names": ["a"], "sector_avgs": {"x": {"avg": 1}},
                    "per_stock": {"600000": {}}}
        crippled = {"date": "2026-09-24", "z_S": None, "top3_avg": None, "z_top3": None,
                    "top3_names": None, "sector_avgs": None, "per_stock": None}
        # 残缺记录写在**后面**（旧实现倒序取第一条匹配会遮蔽完整记录）
        self._write_jsonl([complete, crippled])
        got = mr._pick_sentiment_record("2026-09-24")
        self.assertIsNotNone(got)
        self.assertEqual(got.get("z_S"), 1.2)

    def test_no_record_returns_none(self):
        self._write_jsonl([{"date": "2026-09-23", "z_S": 1.0}])
        self.assertIsNone(mr._pick_sentiment_record("2026-09-24"))

    def test_missing_file_returns_none(self):
        self.assertIsNone(mr._pick_sentiment_record("2026-09-24"))


class TestLoadHoldings(unittest.TestCase):
    def setUp(self):
        self._saved_base = mr.BASE
        self._tmp = tempfile.TemporaryDirectory()
        mr.BASE = Path(self._tmp.name)

    def tearDown(self):
        mr.BASE = self._saved_base
        self._tmp.cleanup()

    def _write_holdings(self, data):
        fp = Path(self._tmp.name) / "t_io" / "state" / "holdings_manual.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_only_qty_positive(self):
        self._write_holdings({
            "600000": {"name": "贵州茅台", "qty": 100, "cost": 1500, "pre_close": 1510},
            "000001": {"name": "平安银行", "qty": 0, "cost": 10, "pre_close": 10},
        })
        got = mr._load_holdings("2026-09-24")
        self.assertIn("600000", got)
        self.assertNotIn("000001", got)
        self.assertEqual(got["600000"]["数量"], 100.0)

    def test_per_stock_merges_mode(self):
        self._write_holdings({"600000": {"name": "贵州茅台", "qty": 100}})
        per_stock = {"600000": {"mode_cn": "波段", "reason": "站上均线"}}
        got = mr._load_holdings("2026-09-24", per_stock)
        self.assertEqual(got["600000"]["做T模式"], "波段")
        self.assertEqual(got["600000"]["理由"], "站上均线")

    def test_missing_per_stock_marks_缺失(self):
        self._write_holdings({"600000": {"name": "贵州茅台", "qty": 100}})
        got = mr._load_holdings("2026-09-24")
        self.assertEqual(got["600000"]["做T模式"], "—（情绪记录缺失）")

    def test_missing_file_returns_empty(self):
        self.assertEqual(mr._load_holdings("2026-09-24"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
