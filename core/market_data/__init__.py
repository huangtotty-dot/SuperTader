# -*- coding: utf-8 -*-
"""core/market_data — 数据同源抽象层（合并实施方案 P1）
统一四个方法契约（daily/minute/snapshot/index_daily），主源 gm、腾讯兜底降级。
对外入口：get_provider()。
"""
from .facade import get_provider

__all__ = ["get_provider"]
