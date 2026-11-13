# -*- coding: utf-8 -*-
"""构造 `EngineContext`（2026-11-13 自 main.py 抽出，供实盘 run_watch 与回放 core/replay 共用）。

`_make_engine_ctx` 原在 main.py，既是实盘主循环、又是回放 CLI 都要的「引擎装配点」。
拆 replay 时若让 core/replay `from main import _make_engine_ctx`，会把实盘副作用带进回放、
且形成 main↔replay 的脆弱耦合。故把装配逻辑独立成叶子模块：只依赖 config / core.utils /
core.data_fetcher / core.signal_engine，无反向依赖。
"""
from __future__ import annotations

from config import HOLDINGS, SIGNAL_OUTCOME_TRACKER
from core.utils import (
    DAILY_DECISION_STATS, _append_jsonl, _default_daily_context, _trace_path,
)
from core.data_fetcher import (
    DAILY_CONTEXT_CACHE, MINUTE_FETCH_DETAIL, MINUTE_FETCH_STATUS,
    _now, fetch_minute_bar, get_today_str,
)
from core.signal_engine import EngineContext


def make_engine_ctx():
    """构造 EngineContext；各可变状态均为对应真模块中的同一对象。"""
    # 个别全局仅在回测/特定模块存在（如 BACKTEST_DAY_CACHE），缺失时用空默认，不阻断实盘
    def _g(name, default):
        return globals().get(name, default)
    return EngineContext(
        holdings=HOLDINGS,
        minute_fetch_status=MINUTE_FETCH_STATUS, minute_fetch_detail=MINUTE_FETCH_DETAIL,
        daily_decision_stats=DAILY_DECISION_STATS,
        daily_context_cache=DAILY_CONTEXT_CACHE, signal_outcome_tracker=SIGNAL_OUTCOME_TRACKER,
        backtest_day_cache=_g("BACKTEST_DAY_CACHE", {}),
        now=_now, get_today_str=get_today_str,
        append_jsonl=_append_jsonl, trace_path=_trace_path,
        fetch_minute_bar=fetch_minute_bar, default_daily_context=_default_daily_context,
    )
