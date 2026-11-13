# -*- coding: utf-8 -*-
"""main.py 主循环**逐阶段计时**的单测（2026-10-16）。**全离线、零副作用**。

背景：owner 报「建仓信号扫描没有实时更新，刷新频率要相对高」。查明**不是扫描慢**——
同一轮 52 只单独测得 66.5s；真凶是 **main.py 主循环一整轮要跑 ~15 分钟**：同一循环里
所有"每 5 分钟"任务的实测间隔全掉到 15 分钟（猎手站上5日线 15~18 分钟、猎手轮询心跳
34~42 分钟、大盘分时预警 32 分钟）⇒ 建仓扫描只能每 ~15 分钟被调到一次。

但**看不到 15 分钟切在哪一步**（原有 `log.debug("⏳ 本轮耗时 …")` 是 debug 级，
日志级别 INFO 下从未落盘）。故加本组计时；本文件把它钉住，重点是钉**"只观测、不改行为"**。

⚠️ **刻意不 `import main`**：main.py 是主程序（实盘副作用：起线程、联网、发飞书）。
计时逻辑已抽到 `core.scan_timing`（2026-11-13），本测试直接 import 它 ——
测的是真逻辑，但没有任何主程序副作用。

运行：python -m unittest tests.phase3.test_scan_stage_timing -v
"""
import logging
import re
import sys
import time as _time
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.scan_timing as _scan_timing  # 计时逻辑已抽模块（2026-11-13），不再切 main.py 源码

_MAIN_SRC = (_ROOT / "main.py").read_text(encoding="utf-8")
# 2026-11-13：scan_once 抽到 core/scan.py（#3 参数注入），逐阶段计时断言改读它。
_SCAN_SRC = (_ROOT / "core" / "scan.py").read_text(encoding="utf-8")
# 2026-11-13：MA5 watcher 抽到 core/hunter_watch.py，`run_live_watch` 断言改读它。
_HW_SRC = (_ROOT / "core" / "hunter_watch.py").read_text(encoding="utf-8")
_LOG_NAME = _scan_timing.log.name  # config.log → "做T助手"


def _load_timing_block():
    """返回计时模块的命名空间（含 _ScanStage / _SCAN_STAGES / _log_scan_round 等）。"""
    return vars(_scan_timing)


class TestScanStageTimer(unittest.TestCase):
    def setUp(self):
        self.ns = _load_timing_block()
        # 模块级状态跨测试共享（不再 exec 新鲜命名空间），这里重置成原始初值，
        # 避免 _SCAN_ROUNDS 计数残留让 test_fast_round_is_quiet 偶发刷 INFO。
        self.ns["_SCAN_STAGES"].clear()
        self.ns["_SCAN_ROUNDS"][:] = [0, 0.0, 0.0]

    def test_stage_accumulates_count_and_max(self):
        Stage = self.ns["_ScanStage"]
        stages = self.ns["_SCAN_STAGES"]
        stages.clear()
        for _ in range(3):
            with Stage("x"):
                _time.sleep(0.01)
        tot, n, mx = stages["x"]
        self.assertEqual(n, 3)
        self.assertGreater(tot, 0.02)
        self.assertGreaterEqual(mx, 0.01)
        self.assertLess(mx, tot, "最慢一次不该等于累计值")

    def test_exit_does_not_swallow_exceptions(self):
        """⚠️ 最关键的一条：计时是**观测**，不许改变控制流。

        `_ScanStage.__exit__` 若返回真值，`with` 块里的异常会被**静默吞掉** ——
        那等于把 scan_once 的异常处理整个改掉（本仓最忌讳的静默失败）。
        """
        Stage = self.ns["_ScanStage"]
        with self.assertRaises(ValueError):
            with Stage("boom"):
                raise ValueError("必须原样抛出去")

    def test_exception_still_recorded(self):
        """抛异常也要记到耗时（否则"卡到超时再抛"的阶段会从报表里消失）。"""
        Stage = self.ns["_ScanStage"]
        stages = self.ns["_SCAN_STAGES"]
        stages.clear()
        try:
            with Stage("y"):
                raise RuntimeError("x")
        except RuntimeError:
            pass
        self.assertEqual(stages["y"][1], 1)

    def test_report_filters_noise_and_sorts_desc(self):
        stages = self.ns["_SCAN_STAGES"]
        stages.clear()
        reg = self.ns["_scan_stage_add"]
        reg("慢", 30.0)
        reg("中", 5.0)
        reg("快", 0.01)          # < 0.5s ⇒ 不该出现（压噪声）
        rep = self.ns["_scan_stage_report"]()
        self.assertIn("慢", rep)
        self.assertIn("中", rep)
        self.assertNotIn("快", rep)
        self.assertLess(rep.index("慢"), rep.index("中"), "必须按耗时降序")

    def test_empty_report_is_readable(self):
        self.ns["_SCAN_STAGES"].clear()
        self.assertIn("无", self.ns["_scan_stage_report"]())

    def test_slow_round_logs_info(self):
        """超过阈值必须落 INFO —— 这正是"看不到 15 分钟切在哪"的修复点。"""
        self.ns["_SCAN_STAGES"].clear()
        self.ns["_scan_stage_add"]("B指数背离 ←嫌疑", 400.0)
        with self.assertLogs(_LOG_NAME, level="INFO") as cm:
            self.ns["_log_scan_round"](900.0)
        joined = "\n".join(cm.output)
        self.assertIn("900.0", joined)
        self.assertIn("B指数背离", joined, "慢阶段必须出现在归因行里")

    def test_fast_round_is_quiet(self):
        """正常轮次不许刷屏（否则一天几百行，等于没有日志）。"""
        logger = logging.getLogger(_LOG_NAME)
        with self.assertNoLogs(logger, level="INFO"):
            self.ns["_log_scan_round"](3.0)

    def test_round_counter_keeps_running_total(self):
        rounds = self.ns["_SCAN_ROUNDS"]
        before = rounds[0]
        self.ns["_log_scan_round"](2.0)
        self.assertEqual(rounds[0], before + 1)
        self.assertGreaterEqual(rounds[1], 2.0)


class TestScanStagesWired(unittest.TestCase):
    """钉住"该包的地方都包了" —— 漏包一个阶段，归因就会指向空气。"""

    # scan_once 交易时段分支里**必须**被计时的调用（2026-10-16 加计时时的全量清单）
    MUST_WRAP = (
        "_maybe_check_index_intraday_alert",
        "_maybe_check_index_divergence",
        "_maybe_run_position_builder_intraday",
        "_maybe_run_ma_break_alert",
        "_maybe_run_ma_reclaim_alert",
        "_maybe_check_trend30_alert",
        # 2026-10-20：`_maybe_check_hunter_ma5_alert` 已迁到独立 watcher（不再在 scan_once 里），
        # 见 test_hunter_ma5_watch_is_decoupled_from_main_loop
        "_maybe_run_hunter_poll",
        "_maybe_run_position_builder",
        "_maybe_run_daily_review",
        "_maybe_run_chart_prefetch",
        "_maybe_run_freshness_patrol",
    )

    def test_every_maybe_call_is_timed(self):
        for fn in self.MUST_WRAP:
            m = re.search(r'with _ScanStage\("[^"]*"\):\s*\n\s*' + re.escape(fn) + r'\(', _SCAN_SRC)
            self.assertIsNotNone(m, f"{fn} 没有被 _ScanStage 包住 —— 归因会漏掉它")

    def test_hunter_ma5_watch_is_decoupled_from_main_loop(self):
        """2026-10-20 owner「飞书提醒一定要及时」：MA5 watcher 必须是独立线程启动，
        不能再挂在主循环（主循环实测 ~15 分钟一轮，会把 5 分钟告警拖成 15 分钟）。"""
        self.assertIn("_start_hunter_ma5_watch()", _MAIN_SRC)
        self.assertNotIn("_maybe_check_hunter_ma5_alert(", _MAIN_SRC,
                         "旧的主循环告警路径应已删除，避免与新 watcher 重复推送")
        self.assertNotIn("_maybe_check_hunter_ma5_alert(", _SCAN_SRC,
                         "scan_once（已抽 core/scan.py）里也不得残留旧的主循环告警路径")
        self.assertIn("run_live_watch", _HW_SRC,
                      "MA5 watcher 本体（已抽到 core/hunter_watch.py）须仍用独立线程 run_live_watch")

    def test_holdings_loop_and_inner_steps_timed(self):
        self.assertIn('_scan_stage_add("I0持仓循环(全部)"', _SCAN_SRC)
        self.assertIn('_ScanStage("I1分钟线取数")', _SCAN_SRC)
        self.assertIn('_ScanStage("I2engine.evaluate")', _SCAN_SRC)

    def test_round_report_hooked_into_main_loop(self):
        self.assertIn("_log_scan_round(elapsed)", _MAIN_SRC,
                      "计时算了却没人调用 ⇒ 等于没加")

    def test_debug_line_preserved(self):
        """原来那行 debug 保留（有人/工具可能依赖它），只是不再指望它落盘。"""
        self.assertIn('log.debug(f"⏳ 本轮耗时', _MAIN_SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
