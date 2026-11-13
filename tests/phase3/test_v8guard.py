# -*- coding: utf-8 -*-
"""py_mini_racer 预热守卫单测 —— core/v8guard.py（2026-11-13 补）。

`prewarm_akshare_v8` 在主线程初始化一次 V8 pool，令其后任何后台线程内的懒初始化都安全
（否则 V8 会在首个触发线程是工作线程时 abort 整进程）。本测试只钉「缺 py_mini_racer → 返回
False 不抛」这条可离线确定的分支；**故意不测成功路径**——真实 MiniRacer() 会真起 V8，若在
测试线程首次初始化反而踩 [[project_pyminiracer_thread_safety]] 的坑。

运行：python tests/phase3/test_v8guard.py
"""
import os
import sys
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.v8guard import prewarm_akshare_v8  # noqa: E402


class TestPrewarmV8(unittest.TestCase):
    def test_missing_pyminiracer_returns_false(self):
        """把 py_mini_racer 从 sys.modules 里抹成 None，令 import 抛 ImportError → False。"""
        with mock.patch.dict(sys.modules, {"py_mini_racer": None}):
            self.assertFalse(prewarm_akshare_v8())

    def test_import_error_returns_false(self):
        """MiniRacer 构造抛任意异常也吞掉返回 False（fail-safe，绝不让守卫自己崩进程）。"""
        fake = mock.MagicMock(side_effect=RuntimeError("v8 init failed"))
        with mock.patch.dict(sys.modules, {"py_mini_racer": mock.MagicMock(MiniRacer=fake)}):
            self.assertFalse(prewarm_akshare_v8())


if __name__ == "__main__":
    unittest.main(verbosity=2)
