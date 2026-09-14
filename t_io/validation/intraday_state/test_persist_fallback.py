# -*- coding: utf-8 -*-
"""Q-20260914-2 回归：intraday_state 原子替换失败必须降级直写 + 告警（2026-09-14）。

根因：占用源（会话级进程）以无 DELETE 共享持有目标文件 → Windows os.replace 必败 WinError 5。
原实现在重试耗尽后零日志零告警静默 fall-through，14:55 落盘失败当日无人察觉（对照 core/utils.py
同款逻辑有 WARN——这版是丢了告警的简化复制）。

故障注入：本进程持句柄 r+b（share=READ|WRITE 无 DELETE）复现占用源。
断言：① 打印 WARN ② 主文件仍被更新（降级直写）③ 不留 tmp 孤儿。

运行：python t_io/validation/intraday_state/test_persist_fallback.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.signal_engine as se  # noqa: E402
from core.signal_engine import SignalEngine  # noqa: E402

# 生产由 main.py 的 exec 共享命名空间注入这两个全局（见 configure_context / module_order），
# 独立 import 时需自备，否则方法体 NameError（与本次修复无关的既有耦合）。
se.get_today_str = lambda: "2026-09-14"  # type: ignore[attr-defined]


class FakeCore:
    def __init__(self):
        self.t_entry_price = {"600176": 44.88}


def _fake_engine(path: str):
    return SimpleNamespace(
        cycle_count={"600176": 3},
        buy_count_per_stock={"600176": 1},
        sell_count_per_stock={"600176": 2},
        trend_regimes={},
        _core=FakeCore(),
        _intraday_state_path=lambda: path,
    )


class TestPersistFallback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="intraday_q2_")
        self.path = os.path.join(self._tmp, "intraday_state.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"date": "2026-09-11", "cycle_count": {}}, f)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _orphan_tmps(self):
        return [n for n in os.listdir(self._tmp) if n.endswith(".tmp")]

    def test_degraded_direct_write_on_locked_target(self):
        held = open(self.path, "r+b")  # 占用源：无 DELETE 共享
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                SignalEngine._persist_intraday_state(_fake_engine(self.path))
            out = buf.getvalue()

            self.assertIn("降级直写", out, "未打印降级告警（静默 fall-through 复发）")
            self.assertNotIn("直写亦失败", out)

            with open(self.path, encoding="utf-8") as f:
                got = json.load(f)
            self.assertEqual(got["cycle_count"], {"600176": 3}, "主文件未被降级直写更新")
            self.assertEqual(got["t_entry_price"], {"600176": 44.88})
            self.assertEqual(self._orphan_tmps(), [], "降级直写后仍残留 tmp 孤儿")
        finally:
            held.close()

    def test_no_warn_when_uncontended(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            SignalEngine._persist_intraday_state(_fake_engine(self.path))
        self.assertEqual(buf.getvalue(), "", "正常路径不应产生告警")
        self.assertEqual(self._orphan_tmps(), [])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["cycle_count"], {"600176": 3})


if __name__ == "__main__":
    unittest.main(verbosity=2)
