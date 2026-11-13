# -*- coding: utf-8 -*-
"""建仓信号扫描调度器单测 —— core/position_scan.py（2026-11-14 补）。

不真跑扫描，mock `_run_position_scan` 与 `_threading.Thread`，测**编排/闸门契约**：
① position_builder 加载失败（`_run_position_scan is None`）→ 两路全部静默不可用；
② 盘中：周末/非交易时段/5 分钟节流/上轮线程未结束 → 跳过；日期翻转清空当日已推送；
③ 盘中 worker：只推 `verdict=="signal"` 且**每票每天只推一次**（去重 set），scan_type=intraday；
④ 收盘：周末/当日已占位/15:05 前/失败 10 分钟节流 → 跳过；成功才占位，scan_type=eod。
历史易错点钉住：收盘「成功才占位」（原异常也占位→当日断供）、周末不触发。
运行：python tests/phase3/test_position_scan.py
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.position_scan as ps  # noqa: E402

FRIDAY_AM = datetime(2026, 11, 13, 10, 0, 0)   # 盘中
FRIDAY_EOD = datetime(2026, 11, 13, 15, 10, 0)  # 收盘后
SATURDAY = datetime(2026, 11, 14, 10, 0, 0)


class _ScanBase(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "run": ps._run_position_scan,
            "push_date": ps._position_builder_push_date,
            "intraday_last": ps._position_builder_intraday_last,
            "intraday_pushed": ps._position_builder_intraday_pushed,
            "eod_last": ps._position_builder_eod_last_attempt,
            "intraday_thread": ps._position_builder_intraday_thread,
        }
        # 日期翻转函数属性也重置，避免跨用例污染
        if hasattr(ps._maybe_run_position_builder_intraday, "_date"):
            delattr(ps._maybe_run_position_builder_intraday, "_date")
        self._reset()

    def tearDown(self):
        ps._run_position_scan = self._saved["run"]
        ps._position_builder_push_date = self._saved["push_date"]
        ps._position_builder_intraday_last = self._saved["intraday_last"]
        ps._position_builder_intraday_pushed = self._saved["intraday_pushed"]
        ps._position_builder_eod_last_attempt = self._saved["eod_last"]
        ps._position_builder_intraday_thread = self._saved["intraday_thread"]

    def _reset(self):
        ps._position_builder_push_date = ""
        ps._position_builder_intraday_last = None
        ps._position_builder_intraday_pushed = set()
        ps._position_builder_eod_last_attempt = None
        ps._position_builder_intraday_thread = None


class TestUnavailableGuard(_ScanBase):
    def test_intraday_unavailable(self):
        ps._run_position_scan = None
        with mock.patch.object(ps._threading, "Thread") as thr:
            ps._maybe_run_position_builder_intraday(FRIDAY_AM)
        thr.assert_not_called()

    def test_eod_unavailable(self):
        ps._run_position_scan = None
        with mock.patch.object(ps._threading, "Lock") as _lock:
            ps._maybe_run_position_builder(FRIDAY_EOD)
        self.assertEqual(ps._position_builder_push_date, "")


class TestIntradayGates(_ScanBase):
    def test_weekend_gate(self):
        ps._run_position_scan = mock.Mock()
        with mock.patch.object(ps._threading, "Thread") as thr:
            ps._maybe_run_position_builder_intraday(SATURDAY)
        thr.assert_not_called()

    def test_outside_trading_hours(self):
        ps._run_position_scan = mock.Mock()
        with mock.patch.object(ps._threading, "Thread") as thr:
            ps._maybe_run_position_builder_intraday(datetime(2026, 11, 13, 12, 0, 0))  # 午休
        thr.assert_not_called()

    def test_throttle_within_5min(self):
        ps._run_position_scan = mock.Mock()
        ps._position_builder_intraday_last = datetime(2026, 11, 13, 9, 58, 0)
        with mock.patch.object(ps._threading, "Thread") as thr:
            ps._maybe_run_position_builder_intraday(FRIDAY_AM)
        thr.assert_not_called()

    def test_previous_thread_alive_skips(self):
        ps._run_position_scan = mock.Mock()
        alive = mock.Mock()
        alive.is_alive.return_value = True
        ps._position_builder_intraday_thread = alive
        with mock.patch.object(ps._threading, "Thread") as thr:
            ps._maybe_run_position_builder_intraday(FRIDAY_AM)
        thr.assert_not_called()


class TestIntradayWorker(_ScanBase):
    def test_signal_dedup_and_scan_type(self):
        ps._run_position_scan = mock.Mock(return_value=[
            {"verdict": "signal", "code": "600481", "name": "A", "composite_score": 95},
            {"verdict": "signal", "code": "600481", "name": "A", "composite_score": 95},  # 重复
            {"verdict": "weak", "code": "002639", "name": "B", "composite_score": 10},
        ])
        with mock.patch.object(ps._threading, "Thread") as thr:
            thr.return_value.is_alive.return_value = False
            ps._maybe_run_position_builder_intraday(FRIDAY_AM)

        worker = thr.call_args.kwargs["target"]
        self.assertEqual(thr.call_args.kwargs["args"], ("2026-11-13",))
        worker("2026-11-13")  # 同步执行 worker，验证推送去重

        ps._run_position_scan.assert_called_once_with(
            date_str="2026-11-13", silent=True, scan_type="intraday")
        # 只推 signal、且重复 code 只推一次
        self.assertEqual(ps._position_builder_intraday_pushed, {"600481"})


class TestEodGates(_ScanBase):
    def test_weekend_gate(self):
        ps._run_position_scan = mock.Mock()
        ps._maybe_run_position_builder(SATURDAY)
        ps._run_position_scan.assert_not_called()

    def test_same_day_dedup(self):
        ps._run_position_scan = mock.Mock()
        ps._position_builder_push_date = "2026-11-13"
        ps._maybe_run_position_builder(FRIDAY_EOD)
        ps._run_position_scan.assert_not_called()

    def test_before_1505_gate(self):
        ps._run_position_scan = mock.Mock()
        ps._maybe_run_position_builder(datetime(2026, 11, 13, 15, 4, 0))
        ps._run_position_scan.assert_not_called()

    def test_retry_throttle_10min(self):
        ps._run_position_scan = mock.Mock()
        ps._position_builder_eod_last_attempt = datetime(2026, 11, 13, 15, 5, 0)
        ps._maybe_run_position_builder(datetime(2026, 11, 13, 15, 9, 0))  # <10min
        ps._run_position_scan.assert_not_called()

    def test_success_marks_date_and_scan_type(self):
        ps._run_position_scan = mock.Mock(return_value=[
            {"verdict": "signal", "code": "600481"},
            {"verdict": "weak", "code": "002639"},
        ])
        ps._maybe_run_position_builder(FRIDAY_EOD)
        ps._run_position_scan.assert_called_once_with(
            date_str="2026-11-13", silent=True, scan_type="eod")
        self.assertEqual(ps._position_builder_push_date, "2026-11-13")  # 成功才占位


if __name__ == "__main__":
    unittest.main(verbosity=2)
