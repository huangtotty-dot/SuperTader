# -*- coding: utf-8 -*-
"""数据源门面（合并实施方案 P1-1）：默认 gm 主源，失败/超时降级腾讯。
每次降级 log.warning；返回 DataFrame 统一在 attrs["source"] 标记 "gm"/"tencent"/"cache"。
"""
import logging

import pandas as pd

from .gm_provider import GmProvider
from .tencent_provider import TencentProvider

log = logging.getLogger("market_data.facade")


def _resample_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """日线重采样为周/月线（OHLC 聚合），period ∈ {day, week, month}。"""
    if period not in ("week", "month") or df is None or df.empty:
        return df
    d = df.copy()
    d["dt"] = pd.to_datetime(d["date"])
    rule = "W-FRI" if period == "week" else "ME"
    g = d.groupby(pd.Grouper(key="dt", freq=rule))
    out = pd.DataFrame({
        "date": g["dt"].max().dt.strftime("%Y-%m-%d"),
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    }).dropna(subset=["close"])
    return out.reset_index(drop=True)


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

    def daily(self, code: str, days: int = 800, period: str = "day") -> pd.DataFrame:
        # 审核残留建议: cache-first 快读（15分钟新鲜度，削减 gm 每次实拉 10× 负载）
        cached = self._tx.daily_cache(code)
        if cached is not None:
            return self._mark(_resample_period(cached, period), "cache")
        if self._gm_ready():
            try:
                df = self._gm.daily(code, days)
                if df is not None and not df.empty:
                    # 阻断5: gm 日线对当日 forming bar（带 ts_date 新鲜度闸；窗口至收盘后16:00，重审#7）
                    df = self._maybe_append_forming(df, code)
                    # 阻断6: gm 结果写缓存（含盘中 15 分钟新鲜度/B-1，由 tencent 缓存读取端执行）
                    from .tencent_provider import save_daily_cache
                    save_daily_cache(code, df)
                    return self._mark(_resample_period(df, period), "gm")
            except Exception as e:
                log.warning("gm.daily 降级腾讯(%s): %s", code, str(e)[:100])
        df = self._tx.daily(code, days)
        return self._mark(_resample_period(df, period), df.attrs.get("source", "tencent"))

    def _maybe_append_forming(self, df: pd.DataFrame, code: str) -> pd.DataFrame:
        """对 gm 日线补当日 bar（P1 审核阻断5+重审#7）。
        gm history_n 盘中/盘后初段不含当日 daily bar（实测 14:11/14:29 end_time=当日23:59:59 仍只返回到昨日），
        故窗口覆盖至收盘后 16:00：盘后快照=当日收盘（ts_date=当日），补入即为完整当日 bar。
        ts_date 新鲜度闸：快照时间戳非当日（盘前/节假日）不补，避免伪造 bar（08-28 教训）。
        注：gm 结算（~15:35）后是否已含当日 bar 待 15:35 后实测确认；无论是否，本窗口在 16:00 前均可靠补全。"""
        import datetime as _dt
        _now = _dt.datetime.now()
        today = _now.strftime("%Y-%m-%d")
        if _now.weekday() >= 5 or not ("09:15" <= _now.strftime("%H:%M") <= "16:00"):
            return df
        if df is None or df.empty or str(df["date"].iloc[-1]) >= today:
            return df
        base = str(code).split("_")[0]
        snap = self._tx.snapshot([base]).get(base)
        if not snap or snap.get("ts_date") != today or not snap.get("price"):
            return df
        px = snap["price"]
        fb = pd.DataFrame([{"date": today, "open": snap.get("open") or px,
                            "high": snap.get("high") or px, "low": snap.get("low") or px,
                            "close": px, "volume": snap.get("volume") or 0.0}])
        return pd.concat([df, fb], ignore_index=True)

    def minute(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        # 先查分钟 CSV 缓存（TTL 内命中直接返回，避免每轮重拉，保留既有快路径）
        cached = self._tx.minute_cache(code, date, ttl_seconds)
        if not cached.empty:
            return self._mark(cached, "cache")
        if self._gm_ready():
            try:
                df = self._gm.minute(code, date)
                if df is not None and not df.empty:
                    self._tx.save_minute_cache(code, date, df)
                    return self._mark(df, "gm")
            except Exception as e:
                log.warning("gm.minute 降级腾讯(%s %s): %s", code, date, str(e)[:100])
        df = self._tx.minute(code, date, ttl_seconds)
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

    def index_minute(self, index: str = "sh000001") -> pd.DataFrame:
        """指数当日分时。gm 无累计量差分形态，指数分钟保留腾讯（P1-2 #6）。"""
        df = self._tx.index_minute(index)
        return self._mark(df, df.attrs.get("source", "tencent"))

    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        if self._gm_ready():
            try:
                df = self._gm.index_daily(index, days, end_date)
                if df is not None and not df.empty:
                    # 阻断6: gm 指数结果写缓存（end_date 缺省时）
                    if end_date is None:
                        from .tencent_provider import save_index_daily_cache
                        save_index_daily_cache(index, df)
                    return self._mark(df, "gm")
            except Exception as e:
                log.warning("gm.index_daily 降级腾讯(%s): %s", index, str(e)[:100])
        df = self._tx.index_daily(index, days, end_date)
        return self._mark(df, df.attrs.get("source", "tencent"))


_facade_singleton = None


def get_provider():
    """返回数据源门面（单例）。默认 gm 主源，失败降级腾讯。"""
    global _facade_singleton
    if _facade_singleton is None:
        _facade_singleton = MarketDataFacade()
    return _facade_singleton
