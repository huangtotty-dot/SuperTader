# -*- coding: utf-8 -*-
"""GUI 看板 / 成交额浮窗 子进程生命周期单测 —— core/gui_subprocess.py（2026-11-14 补）。

不真起 GUI/浮窗子进程，只测**编排契约**（mock `subprocess.Popen`/`threading.Thread`）：
① `--no-gui` / `--no-widget` 短路（不 Popen）；② 浮窗脚本缺失短路；
③ Popen 命令行构造（GUI=`[python, <root>/gui.py]`；浮窗带 `--parent-pid` 防孤儿）；
④ 子进程输出**不丢 DEVNULL**（2026-10-21 教训：挂件秒退不能黑进洞）；⑤ close 语义（运行中 terminate、None/已退 no-op）。
运行：python tests/phase3/test_gui_subprocess.py
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import subprocess  # noqa: E402

import core.gui_subprocess as gs  # noqa: E402


class _GuiBase(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "root": gs._ROOT,
            "gui_proc": gs._GUI_PROC,
            "gui_fp": gs._GUI_LOG_FP,
            "widget_proc": gs._WIDGET_PROC,
            "widget_fp": gs._WIDGET_LOG_FP,
        }
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        gs._ROOT = self._saved["root"]
        gs._GUI_PROC = self._saved["gui_proc"]
        gs._GUI_LOG_FP = self._saved["gui_fp"]
        gs._WIDGET_PROC = self._saved["widget_proc"]
        gs._WIDGET_LOG_FP = self._saved["widget_fp"]


class TestLaunchGui(_GuiBase):
    def test_no_gui_flag_short_circuits(self):
        with mock.patch.object(sys, "argv", ["main.py", "--no-gui"]), \
             mock.patch("subprocess.Popen") as popen:
            gs.launch_gui()
        popen.assert_not_called()

    def test_popen_command_and_not_devnull(self):
        fake = mock.Mock(pid=12345)
        fake.poll.return_value = None
        with mock.patch.object(gs, "_ROOT", self.tmp), \
             mock.patch("subprocess.Popen", return_value=fake) as popen, \
             mock.patch.object(gs.threading, "Thread"):
            gs.launch_gui()
        popen.assert_called_once()
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd, [sys.executable, os.path.join(self.tmp, "gui.py")])
        self.assertEqual(popen.call_args[1]["cwd"], self.tmp)
        # 2026-10-21 教训：stdout 不能是 DEVNULL（否则秒退黑进洞，主日志像没事发生）
        self.assertIsNot(popen.call_args[1]["stdout"], subprocess.DEVNULL)

    def test_popen_failure_is_silent(self):
        with mock.patch.object(gs, "_ROOT", self.tmp), \
             mock.patch("subprocess.Popen", side_effect=OSError("boom")):
            gs.launch_gui()  # 不抛异常，静默降级


class TestLaunchTurnoverWidget(_GuiBase):
    def test_no_widget_flag_short_circuits(self):
        with mock.patch.object(sys, "argv", ["main.py", "--no-widget"]), \
             mock.patch("subprocess.Popen") as popen:
            gs.launch_turnover_widget()
        popen.assert_not_called()

    def test_missing_script_short_circuits(self):
        with mock.patch.object(gs, "_ROOT", self.tmp), \
             mock.patch("subprocess.Popen") as popen:
            gs.launch_turnover_widget()  # tmp 下无 scripts/turnover_widget.py
        popen.assert_not_called()

    def test_popen_with_parent_pid(self):
        script_dir = os.path.join(self.tmp, "scripts")
        os.makedirs(script_dir, exist_ok=True)
        with open(os.path.join(script_dir, "turnover_widget.py"), "w", encoding="utf-8") as f:
            f.write("")
        fake = mock.Mock(pid=999)
        fake.poll.return_value = None
        with mock.patch.object(gs, "_ROOT", self.tmp), \
             mock.patch("subprocess.Popen", return_value=fake) as popen, \
             mock.patch.object(gs.threading, "Thread"):
            gs.launch_turnover_widget()
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], os.path.join(self.tmp, "scripts", "turnover_widget.py"))
        # 带父 pid：main.py 被强杀时浮窗自己发现父进程没了就退出（防孤儿占互斥量）
        self.assertEqual(cmd[2:], ["--parent-pid", str(os.getpid())])


class TestClose(_GuiBase):
    def test_close_gui_terminates_running(self):
        fake = mock.Mock()
        fake.poll.return_value = None
        gs._GUI_PROC = fake
        gs.close_gui()
        fake.terminate.assert_called_once()

    def test_close_gui_noop_when_none(self):
        gs._GUI_PROC = None
        gs.close_gui()  # 不抛异常

    def test_close_gui_noop_when_already_exited(self):
        fake = mock.Mock()
        fake.poll.return_value = 0
        gs._GUI_PROC = fake
        gs.close_gui()
        fake.terminate.assert_not_called()

    def test_close_widget_clears_proc(self):
        fake = mock.Mock()
        fake.poll.return_value = None
        gs._WIDGET_PROC = fake
        gs.close_turnover_widget()
        fake.terminate.assert_called_once()
        self.assertIsNone(gs._WIDGET_PROC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
