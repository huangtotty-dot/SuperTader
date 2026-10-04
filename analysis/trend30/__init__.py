# -*- coding: utf-8 -*-
"""analysis/trend30 — 30分钟线趋势判定（regime 层）。

依据 doc/solutions/2026-10-04_30分钟趋势判定方案.md §2–§4 实现三层状态机：
  ADX(14) 闸门 → EMA20/60 方向 → 2 根确认 + Supertrend 退出，输出 {BULL,BEAR,RANGE}。

指标口径全部复用 analysis/index_regime.py 的 Wilder 实现，避免与大盘 regime 判定漂移。
"""
from .indicators import (
    drop_forming_bar, collapse_stubs, mark_bar_meta, add_30min_indicators, linreg_quality,
)
from .state_machine import Trend30Config, Trend30StateMachine
from .adapter import get_trend30, state_to_trend

__all__ = [
    "Trend30Config", "Trend30StateMachine", "get_trend30", "state_to_trend",
    "add_30min_indicators", "mark_bar_meta", "collapse_stubs",
    "drop_forming_bar", "linreg_quality",
]
