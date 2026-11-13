# -*- coding: utf-8 -*-
"""引擎装配点单测 —— core/engine_ctx.py（2026-11-13 补）。

`make_engine_ctx` 原在 main.py，是实盘主循环与回放 core/replay 共用的「引擎装配点」。
拆出来是为了别让回放 `from main import ...` 把实盘副作用带进来。它最关键的契约是
**身份一致性**：传入 EngineContext 的各可变状态必须是各真模块里的**同一对象**（不是拷贝）。
一旦有人把 HOLDINGS/DAILY_CONTEXT_CACHE 等在装配点里重新实例化成新 dict，实盘主循环和
回放就会各自持有两份状态、互不同步——这类 Bug 最阴险（两个进程各跑各的，数据对不上）。

本测试逐个 `is` 断言这 12 个共享状态 + 2 个函数身份，钉死单一真源。

运行：python tests/phase3/test_engine_ctx.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
import core.data_fetcher as df  # noqa: E402
import core.utils as cu  # noqa: E402
from core.engine_ctx import make_engine_ctx  # noqa: E402


class TestMakeEngineCtx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = make_engine_ctx()

    def test_mutable_state_identity(self):
        """可变状态必须是各真模块的同一对象（不是拷贝）。"""
        ctx = self.ctx
        self.assertIs(ctx.holdings, config.HOLDINGS)
        self.assertIs(ctx.signal_outcome_tracker, config.SIGNAL_OUTCOME_TRACKER)
        self.assertIs(ctx.minute_fetch_status, df.MINUTE_FETCH_STATUS)
        self.assertIs(ctx.minute_fetch_detail, df.MINUTE_FETCH_DETAIL)
        self.assertIs(ctx.daily_context_cache, df.DAILY_CONTEXT_CACHE)
        self.assertIs(ctx.daily_decision_stats, cu.DAILY_DECISION_STATS)

    def test_function_identity(self):
        """函数/工厂也必须是真模块里的同一函数对象。"""
        ctx = self.ctx
        self.assertIs(ctx.now, df._now)
        self.assertIs(ctx.get_today_str, df.get_today_str)
        self.assertIs(ctx.fetch_minute_bar, df.fetch_minute_bar)
        self.assertIs(ctx.append_jsonl, cu._append_jsonl)
        self.assertIs(ctx.trace_path, cu._trace_path)
        self.assertIs(ctx.default_daily_context, cu._default_daily_context)

    def test_backtest_cache_defaults_empty(self):
        """engine_ctx 模块自身没有 BACKTEST_DAY_CACHE，装配点给空 dict 兜底，不阻断实盘。"""
        self.assertEqual(self.ctx.backtest_day_cache, {})

    def test_two_ctx_share_same_mutable_state(self):
        """两次装配得到的新 EngineContext 仍共享同一份 holdings（单例状态不因装配被拆散）。"""
        ctx2 = make_engine_ctx()
        self.assertIs(self.ctx.holdings, ctx2.holdings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
