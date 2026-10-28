# -*- coding: utf-8 -*-
"""t_gui 单实例闸（2026-10-28）。

背景：owner 报「运行 main.py 之后**始终没有跳出 GUI**」。日志里是
`⚠️ 已有一个 GUI 在运行 —— 本次启动退出`，但当时进程列表里一个 t_gui 都没有。
两个 bug 叠在一起：

  1. 拿不到锁就 `sys.exit(7)`，**不给旧实例留退出时间** ⇒ 旧的一走，变成"一个窗口也没有"；
  2. `CreateMutexW` 在"已存在"时**仍然返回一个有效句柄**，没 `CloseHandle` ⇒ 本进程一直持有
     那个内核对象，旧实例退出后 mutex 也永远"不空"，任何重试都会失败。

本文件钉住第 2 条（纯进程内可测）；第 1 条是 `__main__` 里的等待循环，靠代码审查。

运行：python -m pytest -q tests/phase3/test_gui_single_instance.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402


class TestGuiSingleInstance(unittest.TestCase):
    def test_已存在时不能泄漏句柄(self):
        """同名已存在 ⇒ 返回 None，**且要关掉 CreateMutexW 返回的那个句柄**。

        不关的话：本进程也算持有者 ⇒ 别人（另一个 t_gui / 重试循环）永远等不到锁。
        """
        import ctypes
        k32 = ctypes.windll.kernel32
        h1 = t_gui._acquire_gui_single_instance()
        if h1 is None:
            self.skipTest("已有真实 GUI 在跑（这个闸是全局的），跳过")
        self.assertNotEqual(h1, True, "Windows 上应拿到真句柄")
        try:
            # 第二次：同名已存在
            self.assertIsNone(t_gui._acquire_gui_single_instance())
            # 释放第一个 ⇒ 第三次必须能重新拿到
            k32.CloseHandle(h1)
            h1 = None
            h3 = t_gui._acquire_gui_single_instance()
            self.assertIsNotNone(
                h3, "泄漏句柄会让 mutex 永远不空 —— 正是 2026-10-28 16:06 GUI 起不来的原因")
            if h3 not in (True, None):
                k32.CloseHandle(h3)
        finally:
            if h1 not in (None, True):
                try:
                    k32.CloseHandle(h1)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
