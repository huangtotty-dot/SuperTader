# -*- coding: utf-8 -*-
"""provider 数据契约单测（合并实施方案 §0.2）：列/单位/口径校验。
gm 需要终端运行；终端不可用或网络失败时 graceful skip（不阻断其他用例）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]
MINUTE_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
SNAP_KEYS = {"price", "open", "high", "low", "volume", "ts_date"}


class TestTencentContract(unittest.TestCase):
    """腾讯 provider 契约（直连，网络不可达时 skip）。"""

    @classmethod
    def setUpClass(cls):
        from core.market_data.tencent_provider import TencentProvider
        cls.tx = TencentProvider()

    def test_daily_shape(self):
        df = self.tx.daily("600481", 100)
        self.assertFalse(df.empty, "日线不应为空")
        self.assertEqual(list(df.columns), DAILY_COLS)
        self.assertTrue(all(df["volume"] > 0), "volume 应 >0（手）")
        self.assertGreaterEqual(len(df), 50)

    def test_minute_shape(self):
        import pandas as pd
        df = self.tx.minute("600481", "2026-08-28")
        if df.empty:  # 非交易日或接口空 → skip
            self.skipTest("分钟数据为空")
        for c in MINUTE_COLS:
            self.assertIn(c, df.columns)
        self.assertTrue(pd.api.types.is_numeric_dtype(df["volume"]))

    def test_snapshot_shape(self):
        s = self.tx.snapshot(["600481", "002639"])
        if not s:
            self.skipTest("快照为空")
        for code, row in s.items():
            self.assertEqual(set(row.keys()), SNAP_KEYS, code)
            self.assertTrue(row["price"] > 0)
            self.assertIsNotNone(row["ts_date"])

    def test_index_daily_shape(self):
        df = self.tx.index_daily("sh000001", 100)
        self.assertFalse(df.empty)
        self.assertIn("date", df.columns)
        self.assertIn("close", df.columns)


class TestGmContract(unittest.TestCase):
    """gm provider 契约（终端运行时执行，否则 skip）。"""

    @classmethod
    def setUpClass(cls):
        from core.market_data.gm_provider import GmProvider
        cls.gm = GmProvider()
        if not getattr(cls.gm, "_ready", False):
            raise unittest.SkipTest("gm 终端/会话 token 不可用")

    def test_daily_shape(self):
        df = self.gm.daily("600481", 100)
        self.assertEqual(list(df.columns), DAILY_COLS)
        self.assertGreaterEqual(len(df), 50)
        self.assertTrue(all(df["volume"] > 0))

    def test_minute_shape(self):
        df = self.gm.minute("600481", "2026-08-27")
        self.assertEqual(list(df.columns), MINUTE_COLS)
        self.assertTrue(all(df["time"].str.fullmatch(r"\d{2}:\d{2}")))

    def test_snapshot_shape(self):
        s = self.gm.snapshot(["600481", "002639"])
        self.assertTrue(s, "gm 快照不应为空")
        for code, row in s.items():
            self.assertEqual(set(row.keys()), SNAP_KEYS, code)
            self.assertIsNotNone(row["ts_date"])

    def test_index_daily_shape(self):
        df = self.gm.index_daily("sh000001", 100)
        self.assertIn("date", df.columns)
        self.assertIn("close", df.columns)
        self.assertGreaterEqual(len(df), 50)


if __name__ == "__main__":
    unittest.main()
