# -*- coding: utf-8 -*-
"""信号门控集群单测 —— core/signal_gates.py（2026-11-13 补，只测纯函数）。

`_signal_whitelist` 是 C15+C25+C27 合并的信号评估白名单：qty==0 清仓股跳过全部评估；
t_qty==0 纯底仓股保留评估但跳推送（002639 一天 6 条买信号噪音案）；其余正常评估+推送。
此函数无 IO/无网络，直接单测三分支与边界。

运行：python tests/phase3/test_signal_gates.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.signal_gates import _signal_whitelist  # noqa: E402


class TestSignalWhitelist(unittest.TestCase):
    def test_zero_qty_skips_everything(self):
        """qty==0（清仓/零持仓）→ 全部跳过（C27：603667 清仓仍吃加急卖信号教训）。"""
        self.assertEqual(_signal_whitelist("x", {"qty": 0}),
                         {"eval_buy": False, "eval_sell": False, "push": False})

    def test_missing_qty_defaults_zero(self):
        self.assertEqual(_signal_whitelist("x", {}),
                         {"eval_buy": False, "eval_sell": False, "push": False})

    def test_pure_bottom_position_evaluates_but_no_push(self):
        """t_qty==0 纯底仓股：保留评估（日报展示）但跳推送（C25：002639 噪音案）。"""
        self.assertEqual(_signal_whitelist("x", {"qty": 100, "t_qty": 0}),
                         {"eval_buy": True, "eval_sell": True, "push": False})

    def test_active_t_position_full(self):
        self.assertEqual(_signal_whitelist("x", {"qty": 100, "t_qty": 50}),
                         {"eval_buy": True, "eval_sell": True, "push": True})

    def test_string_quantities_are_coerced(self):
        """qty/t_qty 以字符串传入时 int() 转换，语义不变。"""
        self.assertEqual(_signal_whitelist("x", {"qty": "100", "t_qty": "30"}),
                         {"eval_buy": True, "eval_sell": True, "push": True})
        self.assertEqual(_signal_whitelist("x", {"qty": "100", "t_qty": "0"}),
                         {"eval_buy": True, "eval_sell": True, "push": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
