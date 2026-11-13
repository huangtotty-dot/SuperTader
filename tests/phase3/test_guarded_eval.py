# -*- coding: utf-8 -*-
"""`_eval_js_guarded` 单测（2026-11-13）。

背景：pywebview 的 `evaluate_js` **没有超时**（`Invoke` 后 `semaphore.acquire()` 一直等），
渲染进程僵死时调用线程永久卡住、把桥接槽位吃光。2026-11-13 实测就是这个形态：
主窗口最小化一个多小时后页面不再执行脚本（CPU 0%、无崩溃提示），线程卡在 evaluate_js。

覆盖：
  A. 正常窗口 → True，脚本被调用一次；
  B. 渲染进程"僵死"（evaluate_js 永不返回）→ **在 timeout 内返回 False**，不拖住调用方；
  C. 连续 5 次超时后 → 直接跳过（不再起线程，避免泄漏）；
  D. 同上一次还没回来（busy）→ 直接跳过。

运行：python -m unittest tests.phase3.test_guarded_eval -v
"""
import os
import sys
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402


class _Win:
    def __init__(self, hang=False, boom=False):
        self.hang = hang
        self.boom = boom
        self.calls = 0
        self._release = threading.Event()

    def evaluate_js(self, script):
        self.calls += 1
        if self.boom:
            raise RuntimeError("boom")
        if self.hang:
            self._release.wait(30)      # 模拟渲染进程不再执行脚本
        return None


class TestGuardedEval(unittest.TestCase):
    def setUp(self):
        t_gui._EVAL_GUARD.update({"busy": False, "fail": 0})

    def tearDown(self):
        t_gui._EVAL_GUARD.update({"busy": False, "fail": 0})

    def test_ok(self):
        w = _Win()
        self.assertTrue(t_gui._eval_js_guarded(w, "1+1", "t", timeout=1.0))
        self.assertEqual(w.calls, 1)

    def test_exception_is_false(self):
        self.assertFalse(t_gui._eval_js_guarded(_Win(boom=True), "x", "t", timeout=1.0))

    def test_hang_returns_within_timeout(self):
        w = _Win(hang=True)
        t0 = time.time()
        self.assertFalse(t_gui._eval_js_guarded(w, "x", "t", timeout=0.5))
        dt = time.time() - t0
        self.assertLess(dt, 2.0)                 # 关键：不能被渲染进程拖死
        self.assertEqual(t_gui._EVAL_GUARD["fail"], 1)
        w._release.set()                          # 放掉模拟线程

    def test_give_up_after_five(self):
        t_gui._EVAL_GUARD["fail"] = 5
        w = _Win()
        self.assertFalse(t_gui._eval_js_guarded(w, "x", "t", timeout=0.2))
        self.assertEqual(w.calls, 0)             # 不再发起

    def test_busy_skip(self):
        t_gui._EVAL_GUARD["busy"] = True
        w = _Win()
        self.assertFalse(t_gui._eval_js_guarded(w, "x", "t", timeout=0.2))
        self.assertEqual(w.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
