# -*- coding: utf-8 -*-
"""codec 单元测试（合并实施方案 P1-1 验收）。可被 pytest / unittest 运行。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from core.market_data import codec  # noqa: E402


class TestCodec(unittest.TestCase):
    def test_market_of(self):
        self.assertEqual(codec.market_of("600481"), "SH")
        self.assertEqual(codec.market_of("002639"), "SZ")
        self.assertEqual(codec.market_of("588170"), "SH")
        self.assertEqual(codec.market_of("688008"), "SH")
        self.assertEqual(codec.market_of("300054"), "SZ")

    def test_strip_account(self):
        self.assertEqual(codec.strip_account("600481_A"), "600481")
        self.assertEqual(codec.strip_account("600481_B"), "600481")
        self.assertEqual(codec.strip_account("600481"), "600481")

    def test_to_gm(self):
        self.assertEqual(codec.to_gm("600481"), "SHSE.600481")
        self.assertEqual(codec.to_gm("002639"), "SZSE.002639")
        self.assertEqual(codec.to_gm("600481_A"), "SHSE.600481")  # 后缀剥离

    def test_to_internal(self):
        self.assertEqual(codec.to_internal("SHSE.600481"), "600481")
        self.assertEqual(codec.to_internal("SZSE.002639"), "002639")
        self.assertEqual(codec.to_internal("600481"), "600481")  # 已是内部格式
        self.assertEqual(codec.to_internal("sh600481"), "600481")  # 兼容小写


if __name__ == "__main__":
    unittest.main()
