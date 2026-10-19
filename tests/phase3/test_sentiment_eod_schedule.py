# -*- coding: utf-8 -*-
"""收盘 EOD 热度/regime 落库调度的源码契约测试（2026-10-19）。

背景：`大盘跨日 S` 图读 `t_io/index_regime/state.json`，而 state 只在 **EOD** 模式写入：
  · 14:30 尾盘钩子是 `mode="tail"`，明确不写 state/trace；
  · 启动补算只补"昨天"；
  ⇒ 当日条目要等次日启动的 morning 推送，图会停在上一交易日（10-19 实测停在 10-16）。
本测试钉住三件事：①15:30 后有当日 EOD 调度；②子进程有 PYTHONPATH；③gm_main 不再传非法 mode。

⚠️ 与 test_scan_stage_timing 同款：**不 import main**（实盘副作用），只读源码做契约断言。
运行：python -m unittest tests.phase3.test_sentiment_eod_schedule -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_MAIN_SRC = (_ROOT / "main.py").read_text(encoding="utf-8")
_GM_SRC = (_ROOT / "execution" / "auto" / "gm_main.py").read_text(encoding="utf-8")


class TestSentimentEodSchedule(unittest.TestCase):
    def test_eod_hook_defined_and_wired(self):
        self.assertIn("def _maybe_run_sentiment_eod(now: datetime)", _MAIN_SRC)
        self.assertIn("_maybe_run_sentiment_eod(now)", _MAIN_SRC,
                      "scan_once 必须调用 EOD 钩子")
        self.assertIn("dtime(15, 30)", _MAIN_SRC, "收盘后 15:30 才允许跑")
        self.assertIn("_regime_state_last_date() >= today", _MAIN_SRC,
                      "已落库必须短路，避免重复子进程")

    def test_subprocess_sets_pythonpath(self):
        """按绝对路径启动脚本时 sys.path[0]=execution/，不设 PYTHONPATH 则 `import core` 必失败
        （10-12~10-16 的 sentiment_backfill 日志实证）。"""
        self.assertIn('_env["PYTHONPATH"]', _MAIN_SRC)
        self.assertIn("env=_env", _MAIN_SRC)
        self.assertIn('_launch_sentiment_subprocess(today, "eod", "sentiment_eod")', _MAIN_SRC)

    def test_gm_main_never_passes_invalid_mode(self):
        """index_regime 只接受 eod/morning/tail；旧的 `"live"` 会 ValueError 且被外层吞掉。"""
        self.assertNotIn('_ir_mode = "live"', _GM_SRC)
        self.assertIn('_ir_mode = "eod" if now.time() >= dtime(15, 5) else "morning"', _GM_SRC)


if __name__ == "__main__":
    unittest.main()
