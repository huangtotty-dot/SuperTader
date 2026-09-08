# -*- coding: utf-8 -*-
"""src/holdings_sync.py F3-1/F3-3 单测（2026-09-08 归账口径重构·方案A 硬隔离）。

验收：eod 归账与尾部二次归账只写 virtual_qty 视图，实盘 qty/base/t_qty 绝不动摇。
本文件覆盖纯函数 virtual_view_qty（default=qty / 净增量 clamp≥0 / 不改 dict / tail 场景）。
既有 test_holdings_sync_invariant.py 仍覆盖 apply_eod_sync 不变量（保留给晨间 reconcile 口径）。
pytest / unittest 均可运行：python t_io/validation/holdings_sync/test_holdings_sync_virtual.py
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.holdings_sync import virtual_view_qty  # noqa: E402


class TestVirtualView(unittest.TestCase):
    def test_default_equals_qty(self):
        # 无 virtual 视图 → default=qty；delta=0 不变
        h = {"qty": 54500, "base": 54500, "t_qty": 54500}
        self.assertEqual(virtual_view_qty(h, 0), 54500)

    def test_0907_scenario_real_fields_untouched(self):
        # 09-07 事故样：588170 虚拟卖 9700 + auto 买 3800 → 净 delta=-5900。
        # 重构后实盘 qty/base/t_qty 不动摇，只产生 virtual 视图值。
        h = {"qty": 54500, "base": 54500, "t_qty": 54500, "name": "588170"}
        snap = dict(h)
        nv = virtual_view_qty(h, 3800 - 9700)   # bought - sold = -5900
        self.assertEqual(nv, 48600)
        self.assertEqual(h, snap)               # 纯函数不改 dict（实盘字段由 reconcile 独写）

    def test_clamp_nonnegative(self):
        h = {"qty": 200, "t_qty": 200}
        self.assertEqual(virtual_view_qty(h, -300), 0)   # 卖超 → clamp 0

    def test_tail_net_only(self):
        # F3-3 尾部：14:51 TAIL 卖 1400 → 只改 virtual，实盘不动
        h = {"qty": 2800, "base": 2800, "t_qty": 2800, "virtual_qty": 5000}
        nv = virtual_view_qty(h, -1400)
        self.assertEqual(nv, 3600)
        self.assertEqual(h["qty"], 2800)
        self.assertEqual(h["t_qty"], 2800)

    def test_accumulate_over_prior_view(self):
        h = {"qty": 2800, "virtual_qty": 2000}
        self.assertEqual(virtual_view_qty(h, 500), 2500)  # 基于既有视图叠加


if __name__ == "__main__":
    unittest.main(verbosity=2)
