# -*- coding: utf-8 -*-
"""core/diag_metrics 单测（2026-11-23 P1-6）：计数语义 + 落盘格式 + 自身静默纪律。"""
import json
import os
import tempfile
import unittest

from core import diag_metrics as dm


class TestSwallowCounting(unittest.TestCase):
    def setUp(self):
        dm._reset_for_test()

    def test_counts_accumulate_per_tag(self):
        for _ in range(3):
            dm.swallow("a.x")
        dm.swallow("a.y")
        snap = dm.snapshot()
        self.assertEqual(snap["swallows"]["a.x"]["count"], 3)
        self.assertEqual(snap["swallows"]["a.y"]["count"], 1)

    def test_last_exception_recorded(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            dm.swallow("a.err", e)
        self.assertIn("ValueError", dm.snapshot()["swallows"]["a.err"]["last_exc"])

    def test_swallow_never_raises(self):
        # 传什么垃圾都不许抛（诊断链静默纪律）
        dm.swallow(None)
        dm.swallow(12345, object())

    def test_process_tag(self):
        dm.set_process_tag("gui")
        self.assertEqual(dm.snapshot()["process"], "gui")


class TestFlush(unittest.TestCase):
    def setUp(self):
        dm._reset_for_test()

    def test_no_write_when_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "m.jsonl")
            self.assertIsNone(dm.flush(p))
            self.assertFalse(os.path.exists(p))

    def test_append_jsonl_one_line(self):
        dm.set_process_tag("t")
        dm.swallow("a.x")
        dm.swallow("a.x")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "sub", "m.jsonl")   # 目录不存在也应自动建
            self.assertEqual(dm.flush(p), p)
            dm.flush(p)                              # 第二次 append 不覆盖
            lines = open(p, encoding="utf-8").read().strip().split("\n")
            self.assertEqual(len(lines), 2)
            row = json.loads(lines[0])
            self.assertEqual(row["process"], "t")
            self.assertEqual(row["swallows"]["a.x"]["count"], 2)

    def test_flush_bad_path_returns_none_not_raise(self):
        dm.swallow("a.x")
        self.assertIsNone(dm.flush("Z:\\不存在的盘符\\x\\m.jsonl"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
