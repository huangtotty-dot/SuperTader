# -*- coding: utf-8 -*-
"""数据源门面（合并实施方案 P1-1）：默认 gm 主源，失败/超时降级腾讯。
每次降级 log.warning；返回 DataFrame 统一在 attrs["source"] 标记 "gm"/"tencent"/"cache"。
"""
import logging
from datetime import datetime

import pandas as pd

from .gm_provider import GmProvider
from .tencent_provider import TencentProvider

log = logging.getLogger("market_data.facade")

# 指数当日 forming bar 缓存（key=(index, today)，同一天内多次 index_daily 不重复拉腾讯分时；
# 手动扫描 24 只每只都会走 timing_gate→index_daily，避免每只都拉一次分时导致慢/限流）
_index_forming_cache = {}

# gm 分钟线短 TTL 去重（2026-08-31，手动盘数据源与自动盘对齐：gm 优先 + 去 cache-first 后，
# 手动扫描 ~24 只每轮 gm 60s 直拉最坏 12-48s；gm 数据专用内存去重把同股同分钟拉取压到至多 1 次）
_GM_MINUTE_TTL = 60          # 秒：去重窗口（< 5 分钟扫描节拍）
_gm_minute_cache = {}        # {(code, date): {"df": df, "ts": datetime}}


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
        # 2026-08-31 手动盘数据源与自动盘对齐：去掉腾讯 cache-first，gm 优先（腾讯仅降级兜底）。
        # 自动盘(gm_main)纯 gm 直拉；手动盘此处同样优先 gm，保证两侧数据/判定一致。
        if self._gm_ready():
            try:
                df = self._gm.daily(code, days)
                if df is not None and not df.empty:
                    # 阻断5: gm 日线对当日 forming bar（带 ts_date 新鲜度闸；窗口至收盘后16:00，重审#7）
                    df = self._maybe_append_forming(df, code)
                    # 阻断6: gm 结果写缓存（供 gm 不可用时段兜底；含盘中 15 分钟新鲜度/B-1，由 tencent 缓存读取端执行）
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

    def _maybe_append_index_forming(self, df: pd.DataFrame, index: str = "sh000001") -> pd.DataFrame:
        """指数日线补当日 forming bar（对齐个股 daily 的 _maybe_append_forming，2026-08-31 修复）。

        gm history_n 盘中不含当日指数 bar（与个股同，结算后才返回当日），否则 regime 判定
        用昨日收盘误判市场方向（实测：昨日 3952 盘中已 3982，建仓时效性被拖后）。
        用腾讯指数分时（index_minute）聚合当日 OHLCV 补上；分时失败则返回原 df（降级用昨日）。
        时间窗与个股一致：工作日 09:15-16:00，已含当日/周末/盘前不补。"""
        import datetime as _dt
        _now = _dt.datetime.now()
        today = _now.strftime("%Y-%m-%d")
        if _now.weekday() >= 5 or not ("09:15" <= _now.strftime("%H:%M") <= "16:00"):
            return df
        if df is None or df.empty or str(df["date"].iloc[-1]) >= today:
            return df
        _key = (index, today)
        fb = _index_forming_cache.get(_key)
        if fb is None:
            try:
                m = self._tx.index_minute(index)
            except Exception:
                return df
            if m is None or m.empty:
                return df
            fb = pd.DataFrame([{
                "date": today,
                "open": float(m["open"].iloc[0]),
                "high": float(m["high"].max()),
                "low": float(m["low"].min()),
                "close": float(m["close"].iloc[-1]),
                "volume": float(m["volume"].sum()),
                "amount": float(m["amount"].sum()),
            }])
            _index_forming_cache[_key] = fb
        return pd.concat([df, fb], ignore_index=True)

    def minute(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        # 2026-08-31 手动盘数据源与自动盘对齐：去掉腾讯 CSV cache-first，gm 优先（腾讯仅降级兜底）。
        # gm 数据内存 60s 去重：同股同分钟 60s 内不重复直拉，压住手动扫描 ~24 只的 gm 分钟拉取量。
        # ttl_seconds 仅作用于腾讯 CSV 兜底缓存（position_builder 传 0 = 兜底不吃陈旧 CSV）。
        if self._gm_ready():
            _k = (code, date)
            _hit = _gm_minute_cache.get(_k)
            if _hit and (datetime.now() - _hit["ts"]).total_seconds() < _GM_MINUTE_TTL:
                return self._mark(_hit["df"], "gm")
            try:
                df = self._gm.minute(code, date)
                if df is not None and not df.empty:
                    _gm_minute_cache[_k] = {"df": df.copy(), "ts": datetime.now()}
                    if len(_gm_minute_cache) > 200:  # 清理早于昨天的条目，防长期运行累积
                        _gm_minute_cache.clear()
                    self._tx.save_minute_cache(code, date, df)  # 写 CSV：gm 不可用时段腾讯兜底可读
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

    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        if self._gm_ready():
            try:
                df = self._gm.index_daily(index, days, end_date)
                if df is not None and not df.empty:
                    # 阻断6: gm 指数结果写缓存（end_date 缺省时）
                    if end_date is None:
                        # 2026-08-31: 补当日 forming bar（gm 盘中不含当日指数），否则 regime 用昨日收盘
                        df = self._maybe_append_index_forming(df, index)
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
