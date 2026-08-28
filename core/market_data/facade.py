# -*- coding: utf-8 -*-
"""数据源门面（合并实施方案 P1-1）：默认 gm 主源，失败/超时降级腾讯。
每次降级 log.warning；返回 DataFrame 统一在 attrs["source"] 标记 "gm"/"tencent"/"cache"。
"""
import logging

import pandas as pd

from .gm_provider import GmProvider
from .tencent_provider import TencentProvider

log = logging.getLogger("market_data.facade")


class MarketDataFacade:
    """双源门面：优先 gm，异常/空结果降级腾讯。"""

    def __init__(self):
        self._gm = GmProvider()
        self._tx = TencentProvider()

    def _gm_ready(self) -> bool:
        return getattr(self._gm, "_ready", False)

    def _mark(self, df: pd.DataFrame, src: str) -> pd.DataFrame:
        df.attrs["source"] = src
        return df

    def daily(self, code: str, days: int = 800) -> pd.DataFrame:
        if self._gm_ready():
            try:
                df = self._gm.daily(code, days)
                if df is not None and not df.empty:
                    return self._mark(df, "gm")
            except Exception as e:
                log.warning("gm.daily 降级腾讯(%s): %s", code, str(e)[:100])
        df = self._tx.daily(code, days)
        return self._mark(df, df.attrs.get("source", "tencent"))

    def minute(self, code: str, date: str) -> pd.DataFrame:
        if self._gm_ready():
            try:
                df = self._gm.minute(code, date)
                if df is not None and not df.empty:
                    return self._mark(df, "gm")
            except Exception as e:
                log.warning("gm.minute 降级腾讯(%s %s): %s", code, date, str(e)[:100])
        df = self._tx.minute(code, date)
        return self._mark(df, df.attrs.get("source", "tencent"))

    def snapshot(self, codes: list) -> dict:
        # 快照契约是 dict 非 DataFrame，source 无法进 attrs——gm 优先，空/异常回退腾讯
        if self._gm_ready():
            try:
                out = self._gm.snapshot(codes)
                if out:
                    return out
            except Exception as e:
                log.warning("gm.snapshot 降级腾讯: %s", str(e)[:100])
        return self._tx.snapshot(codes)

    def index_daily(self, index: str = "sh000001", days: int = 800) -> pd.DataFrame:
        if self._gm_ready():
            try:
                df = self._gm.index_daily(index, days)
                if df is not None and not df.empty:
                    return self._mark(df, "gm")
            except Exception as e:
                log.warning("gm.index_daily 降级腾讯(%s): %s", index, str(e)[:100])
        df = self._tx.index_daily(index, days)
        return self._mark(df, df.attrs.get("source", "tencent"))


_facade_singleton = None


def get_provider():
    """返回数据源门面（单例）。默认 gm 主源，失败降级腾讯。"""
    global _facade_singleton
    if _facade_singleton is None:
        _facade_singleton = MarketDataFacade()
    return _facade_singleton
