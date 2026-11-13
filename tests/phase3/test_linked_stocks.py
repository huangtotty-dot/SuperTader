# -*- coding: utf-8 -*-
"""「个股联动」后端单测（2026-11-13，全离线）。

口径：按过去 N 日**收益相关性**从全池挑正相关的票（证据见 tmp/linkage_study.py：
N=120 时挑出的 10 只，未来 20 日平均相关 0.533；同概念随机 10 只只有 0.46）。

覆盖：
  A. 合成矩阵：与目标同步涨跌的票排第一，无关的票排后/被排除；
  B. 只返回正相关；
  C. 名称唯一命中 / 找不到 / 多只命中；
  D. 代码不在矩阵里 → 明确报错；
  E. 矩阵档缺失 → warming（并触发后台重算，不抛）。

运行：python -m unittest tests.phase3.test_linked_stocks -v
"""
import os
import sys
import tempfile
import unittest

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402


class TestLinkedStocks(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.fp = os.path.join(self.td, "linkage_matrix.npz")
        self._old_fp = t_gui.Api._LINK_FP
        t_gui.Api._LINK_FP = __import__('pathlib').Path(self.fp)
        t_gui.Api._LINK_MEM = {"mtime": None, "data": None}
        self.api = t_gui.Api()

    def tearDown(self):
        t_gui.Api._LINK_FP = self._old_fp
        t_gui.Api._LINK_MEM = {"mtime": None, "data": None}

    def _build(self, n=80):
        """3 只票：688001 与 688002 同步（相关≈1），688003 反向（相关≈-1）。"""
        rng = np.random.default_rng(0)
        base = rng.normal(0, 0.02, n)
        R = np.zeros((n, 3), dtype=np.float32)
        R[:, 0] = base
        R[:, 1] = base + rng.normal(0, 0.001, n)      # 与 0 几乎完全同步
        R[:, 2] = -base + rng.normal(0, 0.001, n)     # 与 0 反向
        dates = np.array([f"2026-08-{i+1:02d}" for i in range(n)])
        np.savez_compressed(self.fp, dates=dates, codes=np.array(["688001", "688002", "688003"]), R=R)

    def test_rank_and_positive_only(self):
        self._build()
        d = self.api.load_linked_stocks("688001", 60)
        self.assertTrue(d["available"])
        self.assertEqual(d["code"], "688001")
        codes = [r["code"] for r in d["rows"]]
        self.assertEqual(codes, ["688002"])            # 反向那只不出现（只要正相关）
        self.assertGreater(d["rows"][0]["corr"], 0.9)
        self.assertLessEqual(d["rows"][0]["corr"], 1.0)
        self.assertEqual(d["days"], 60)
        self.assertEqual(len(d["window"]), 2)

    def test_unknown_code(self):
        self._build()
        d = self.api.load_linked_stocks("999999", 60)
        self.assertFalse(d["available"])
        self.assertIn("不在联动矩阵", d["error"])

    def test_name_resolution(self):
        self._build()
        # 名称走韭研名表：老接口 _jiuyan_names 里有"华工科技"这类，这里用注入的方式验证分支
        old = t_gui._jiuyan_names
        t_gui._jiuyan_names = lambda: {"688001": "甲科技", "688002": "乙科技", "688009": "甲科技"}
        try:
            d = self.api.load_linked_stocks("乙科技", 60)
            self.assertTrue(d["available"])
            self.assertEqual(d["code"], "688002")
            # 多只同名 ⇒ 明确报错（别猜）
            d2 = self.api.load_linked_stocks("甲科技", 60)
            self.assertFalse(d2["available"])
            self.assertIn("匹配到多只", d2["error"])
            d3 = self.api.load_linked_stocks("不存在的名字", 60)
            self.assertFalse(d3["available"])
        finally:
            t_gui._jiuyan_names = old

    def test_missing_matrix_is_warming(self):
        t_gui.Api._LINK_BUILD = {"running": False, "at": 0.0}
        d = self.api.load_linked_stocks("688001", 60)
        self.assertFalse(d["available"])
        self.assertTrue(d.get("warming"))
        # 不应抛异常；后台重算被节流置位（这里只看 at 被写过）
        self.assertGreater(t_gui.Api._LINK_BUILD.get("at") or 0, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
