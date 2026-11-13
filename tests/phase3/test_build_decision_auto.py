# -*- coding: utf-8 -*-
"""auto 侧建仓判定适配器单测 —— execution/auto/build_decision_auto.py（2026-11-13 补）。

P4-6 把 gm_main BASE 建仓块的数据适配进 core/build_decision.py 决策核（双侧单一真源）。
本测试只钉适配器**自己**的两条契约（决策核 core/build_decision 另有测试）：
① `_load_build_decision` 必须返回 `core.build_decision` 的**同一模块对象**（单一真源，
   不允许 importlib 再加载一份副本导致两侧口径漂移）；② `decide` 在日线不足（features 为空）
   时 **fail-closed**——go=False、data_insufficient=True、verdict=weak、score=0，绝不静默放行。

运行：python tests/phase3/test_build_decision_auto.py
"""
import os
import sys
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import execution.auto.build_decision_auto as bda  # noqa: E402
from core import build_decision as core_bd  # noqa: E402


class TestLoadBuildDecision(unittest.TestCase):
    def test_returns_core_module_identity(self):
        """单一真源：适配器持有的 bd 就是 core.build_decision 本尊，不是第二份副本。"""
        self.assertIs(bda._load_build_decision(), core_bd)

    def test_core_exposes_required_api(self):
        """决策核必须暴露适配器依赖的 6 个符号（P4-6 契约）。"""
        for name in ("DEFAULT_TIMING_PARAMS", "regime_from_index_daily",
                     "features_from_daily", "timing_decision",
                     "verdict_from_timing", "intraday_confirm"):
            self.assertTrue(hasattr(bda.bd, name), name)


class TestDecideFailClosed(unittest.TestCase):
    def test_data_insufficient_fails_closed(self):
        """日线不足（features 为空，<61 行）→ fail-closed，绝不静默放行建仓。"""
        with mock.patch.object(bda.bd, "regime_from_index_daily",
                               return_value={"regime": "trend_up"}), \
             mock.patch.object(bda.bd, "features_from_daily", return_value={}):
            r = bda.decide(pd.DataFrame(), pd.DataFrame(), "2026-11-13")
        self.assertFalse(r["go"])
        self.assertTrue(r["data_insufficient"])
        self.assertEqual(r["verdict"], "weak")
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["regime"], "trend_up")
        self.assertEqual(r["veto"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
