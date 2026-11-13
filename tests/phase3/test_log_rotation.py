# -*- coding: utf-8 -*-
"""诊断日志上限轮转单测 —— core/log_rotation.py（2026-11-13 补）。

GUI 卡死排查日志（gui_freeze.log 等）都是手工 open(fp,"a").write(...)，没有
RotatingFileHandler，单日能灌几十 MB。cap_log_file 提供 6 行小函数在每次追加前调用：
超过 max_mb 就把当前文件改名成 `<fp>.1`（先删旧 .1），下一次 open(fp,"a") 自然新建。
要钉死：
① 未超限不动（不产生 .1）；② 超限改名且覆盖旧 .1；③ 文件不存在静默不抛；
④ max_mb 阈值含边界（恰等于阈值不轮转，`<=` 语义）。

运行：python tests/phase3/test_log_rotation.py
"""
import os
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.log_rotation import cap_log_file  # noqa: E402


class TestCapLogFile(unittest.TestCase):
    def test_under_limit_no_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gui_freeze.log")
            with open(p, "w") as f:
                f.write("small log line\n")
            cap_log_file(p, max_mb=1.0)
            self.assertTrue(os.path.exists(p))
            self.assertFalse(os.path.exists(p + ".1"))

    def test_over_limit_rotates(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gui_freeze.log")
            with open(p, "w") as f:
                f.write("x" * 100)  # 100 字节
            cap_log_file(p, max_mb=0.000001)  # 阈值 ≈ 1 字节
            self.assertFalse(os.path.exists(p), "超限后原文件应被改名")
            self.assertTrue(os.path.exists(p + ".1"), "轮转产物 .1 应存在")
            with open(p + ".1") as f:
                self.assertEqual(f.read(), "x" * 100)

    def test_old_backup_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gui_freeze.log")
            with open(p + ".1", "w") as f:
                f.write("stale backup")
            with open(p, "w") as f:
                f.write("y" * 100)
            cap_log_file(p, max_mb=0.000001)
            with open(p + ".1") as f:
                self.assertEqual(f.read(), "y" * 100, "旧 .1 应被覆盖")

    def test_missing_file_silent(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nope.log")
            cap_log_file(p, max_mb=0.000001)  # 不存在 → 不抛异常
            self.assertFalse(os.path.exists(p))

    def test_exact_boundary_no_rotation(self):
        """恰好等于阈值时用 `<=` 不轮转——钉死边界语义。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gui_freeze.log")
            data = "a" * 1024  # 1024 字节
            with open(p, "w") as f:
                f.write(data)
            cap_log_file(p, max_mb=1024 / (1024 * 1024))  # 阈值恰 = 1024 字节
            self.assertTrue(os.path.exists(p))
            self.assertFalse(os.path.exists(p + ".1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
