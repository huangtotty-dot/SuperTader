# -*- coding: utf-8 -*-
"""`load_pool_members` 单测（2026-11-13，全离线）。

为什么单独有这个方法：多窗口看盘的下拉要能选「避风港湾」里的票，而
`load_abnormal_pool` / `load_shelter_pool` 是**整表**接口（254 行实测 3~12s、还会起预热线程），
为了几十个下拉选项不值得。本方法只读状态档。

覆盖：
  A. 返回 {code, name}，只认 6 位数字码；
  B. 缺 name 时用概念名兜底；
  C. 档不存在 → 空列表（不抛）；
  D. 毫秒级（不触发任何计算/网络）。

运行：python -m unittest tests.phase3.test_pool_members -v
"""
import json
import os
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gui  # noqa: E402


class TestPoolMembers(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self._old_s = gui.Api._SHELTER_FP
        self._old_a = gui.Api._ABN_FP
        gui.Api._SHELTER_FP = type(self._old_s)(os.path.join(self.td, "shelter.json"))
        gui.Api._ABN_FP = type(self._old_a)(os.path.join(self.td, "abn.json"))
        self.api = gui.Api()

    def tearDown(self):
        gui.Api._SHELTER_FP = self._old_s
        gui.Api._ABN_FP = self._old_a

    def _write(self, fp, stocks):
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"stocks": stocks}, f, ensure_ascii=False)

    def test_basic(self):
        self._write(gui.Api._SHELTER_FP, {
            "600900": {"name": "长江电力", "added_at": "2026-11-01"},
            "000001_OLD": {"name": "平安银行"},          # 带后缀也要归一到裸码
        })
        self._write(gui.Api._ABN_FP, {"600519": {"name": "贵州茅台"}})
        r = self.api.load_pool_members()
        self.assertEqual(r["shelter"], [{"code": "600900", "name": "长江电力"},
                                        {"code": "000001", "name": "平安银行"}])
        self.assertEqual(r["abnormal"], [{"code": "600519", "name": "贵州茅台"}])

    def test_name_fallback_and_bad_codes(self):
        self._write(gui.Api._SHELTER_FP, {
            "600000": {},                                  # 无 name ⇒ 概念名/代码兜底
            "ABC": {"name": "x"},                          # 非 6 位 ⇒ 丢
            "12345": {"name": "y"},                        # 5 位 ⇒ 丢
        })
        r = self.api.load_pool_members()
        self.assertEqual(len(r["shelter"]), 1)
        self.assertEqual(r["shelter"][0]["code"], "600000")
        self.assertTrue(r["shelter"][0]["name"])           # 至少有值（名称或代码本身）

    def test_missing_files(self):
        r = self.api.load_pool_members()
        self.assertEqual(r, {"shelter": [], "abnormal": []})

    def test_fast(self):
        self._write(gui.Api._SHELTER_FP, {"600900": {"name": "长江电力"}})
        t0 = time.time()
        for _ in range(20):
            self.api.load_pool_members()
        self.assertLess((time.time() - t0) / 20, 0.05)     # 只读档，必须毫秒级


if __name__ == "__main__":
    unittest.main(verbosity=2)
