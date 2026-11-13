# -*- coding: utf-8 -*-
"""拦截可见性去重单测 —— core/intercept_notice.py（2026-11-13 补）。

F-11(2026-09-04) 把「每股每向每日 1 条」布尔去重改成 20min 滚动冷却窗：同 (code:action)
距上次成功发送 < COOLDOWN_S 抑制，否则放行。三件事要钉死：
① 冷却窗内抑制 / 窗外放行；② 旧布尔 True 视为「很久前已发」→ 放行（升级兼容）；
③ send 失败不写时间戳（mark_sent 只在成功后调），重启后允许补报。

运行：python tests/phase3/test_intercept_notice.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.intercept_notice import (  # noqa: E402
    mark_sent, read_state, send_allowed, write_state,
)

_NOW = datetime(2026, 9, 4, 10, 57, 0)


class TestSendAllowed(unittest.TestCase):
    def test_empty_state_allows(self):
        self.assertTrue(send_allowed({}, "2026-09-04", "002451:BUY_LOW", _NOW))

    def test_within_cooldown_suppresses(self):
        st = {"2026-09-04": {"002451:BUY_LOW": "2026-09-04 10:47:00"}}  # 10 分钟前，< 20min
        self.assertFalse(send_allowed(st, "2026-09-04", "002451:BUY_LOW", _NOW))

    def test_exact_cooldown_boundary_allows(self):
        """恰好 20 分钟（< 而非 <=）→ 放行。"""
        st = {"2026-09-04": {"k": "2026-09-04 10:37:00"}}
        self.assertTrue(send_allowed(st, "2026-09-04", "k", _NOW))

    def test_beyond_cooldown_allows(self):
        st = {"2026-09-04": {"002451:BUY_LOW": "2026-09-04 10:00:00"}}
        self.assertTrue(send_allowed(st, "2026-09-04", "002451:BUY_LOW", _NOW))

    def test_legacy_bool_true_allows(self):
        """旧布尔 True 视为「很久前已发」→ 放行（升级当日兼容）。"""
        st = {"2026-09-04": {"002451:BUY_LOW": True}}
        self.assertTrue(send_allowed(st, "2026-09-04", "002451:BUY_LOW", _NOW))

    def test_unparseable_timestamp_allows(self):
        st = {"2026-09-04": {"002451:BUY_LOW": "garbage"}}
        self.assertTrue(send_allowed(st, "2026-09-04", "002451:BUY_LOW", _NOW))

    def test_different_key_independent(self):
        st = {"2026-09-04": {"A:BUY_LOW": "2026-09-04 10:34:00"}}
        self.assertTrue(send_allowed(st, "2026-09-04", "B:BUY_LOW", _NOW))

    def test_different_day_independent(self):
        st = {"2026-09-03": {"002451:BUY_LOW": "2026-09-03 10:34:00"}}
        self.assertTrue(send_allowed(st, "2026-09-04", "002451:BUY_LOW", _NOW))


class TestMarkSentRoundtrip(unittest.TestCase):
    def test_mark_then_suppress_within_cooldown(self):
        st = {}
        mark_sent(st, "2026-09-04", "002451:BUY_LOW", _NOW)
        self.assertFalse(send_allowed(st, "2026-09-04", "002451:BUY_LOW", _NOW))
        # 20 分钟后放行
        later = datetime(2026, 9, 4, 11, 17, 1)
        self.assertTrue(send_allowed(st, "2026-09-04", "002451:BUY_LOW", later))


class TestReadWriteState(unittest.TestCase):
    def test_read_missing_file_returns_empty(self):
        self.assertEqual(read_state("/nonexistent/definitely/not/here.json"), {})

    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "state.json")
            write_state(p, {"2026-09-04": {"k": "2026-09-04 10:34:00"}})
            self.assertEqual(read_state(p)["2026-09-04"]["k"], "2026-09-04 10:34:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
