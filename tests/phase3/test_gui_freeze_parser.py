# -*- coding: utf-8 -*-
"""GUI 卡死看板的 **header 解析** 单测（2026-10-14）。

**为什么有这份测试**：2026-10-14 我给看门狗加「哨兵按需落栈」时把 header 重排成
「心跳停在前、时间在后」，而看板的正则只认「时间在前」那种 ⇒ **当天下午起所有卡死段
对看板静默不可见**（看板照报 39 段、看着完全正常，实际漏了 31 段）。
静默失效 + 监控工具自己坏掉 = 最坏组合（本仓 10-11 就栽过一次「文件仍在增长时以尾部当下结论」）。

本测试钉住：**两种 header 顺序都必须能解析**（历史日志不能变瞎），
且**哨兵 dump 不能被当成卡死**（它只是"按需落栈"，不是心跳停）。

运行：python -m unittest tests.phase3.test_gui_freeze_parser -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]


def _load_board():
    """按绝对路径加载只读看板脚本（它在 scripts/experiment/，不是包）。"""
    fp = _ROOT / "scripts" / "experiment" / "gui_freeze_regression_20261011.py"
    spec = importlib.util.spec_from_file_location("gui_freeze_regression", fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOARD = _load_board()

_LINE_A = "===== 疑似界面卡死 2026-10-14 14:33:52（主线程心跳停 12s）线程栈 ====="
_LINE_B = "===== 疑似界面卡死（主线程心跳停 13s） 2026-10-14 16:00:04 线程栈 ====="
_LINE_SENTINEL = "===== 按需线程栈（哨兵触发，未卡死） 2026-10-14 14:37:18 线程栈 ====="


class TestHeaderParsing(unittest.TestCase):
    def test_historical_order_date_first(self):
        self.assertEqual(BOARD._match_hdr(_LINE_A), ("2026-10-14", "14:33:52", "12"))

    def test_regressed_order_heartbeat_first_still_parsed(self):
        """2026-10-14 下午被我改坏的形状 —— **必须继续能解析**（那批日志已经落盘了）。"""
        self.assertEqual(BOARD._match_hdr(_LINE_B), ("2026-10-14", "16:00:04", "13"))

    def test_sentinel_dump_is_not_a_freeze(self):
        """哨兵 dump 不是卡死事件，看板必须忽略它（否则"按需落栈"会把段数刷高）。"""
        self.assertIsNone(BOARD._match_hdr(_LINE_SENTINEL))

    def test_both_orders_yield_same_shape(self):
        for line in (_LINE_A, _LINE_B):
            g = BOARD._match_hdr(line)
            self.assertIsNotNone(g, line)
            self.assertEqual(len(g), 3)
            self.assertTrue(g[0].startswith("2026-"), "第 1 位必须是日期")
            self.assertIn(":", g[1], "第 2 位必须是时间")
            self.assertTrue(g[2].isdigit(), "第 3 位必须是心跳停秒数")


class TestWriterHeaderShape(unittest.TestCase):
    """写入侧必须产出**历史形状**（时间在 head 之后、尾巴之前）。

    只读地读源码断言——不启动 GUI。这条防的是"解析侧放宽了、写入侧又乱来"，
    两边都钉住才算真正稳定。
    """

    def test_watchdog_writes_date_before_parenthesis(self):
        src = (_ROOT / "t_gui.py").read_text(encoding="utf-8")
        # _dump 的写行必须是 `{head} {时间}{tail} 线程栈`
        self.assertIn('f"\\n===== {head} {datetime.now().strftime(\'%Y-%m-%d %H:%M:%S\')}"',
                      src, "t_gui._dump 的 header 形状被改动了 —— 看板解析依赖它")
        # 卡死分支必须把「心跳停」放进 tail（而不是 head）
        self.assertIn('_dump("疑似界面卡死", f"（主线程心跳停 {_now - _ts:.0f}s）")', src,
                      "卡死分支应把心跳停放 tail，保证时间是紧跟 head 的第一个字段")


if __name__ == "__main__":
    unittest.main(verbosity=2)
