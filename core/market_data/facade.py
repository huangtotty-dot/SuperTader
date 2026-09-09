# -*- coding: utf-8 -*-
"""数据源门面（合并实施方案 P1-1）：默认 gm 主源，失败/超时降级腾讯。
每次降级 log.warning；返回 DataFrame 统一在 attrs["source"] 标记 "gm"/"tencent"/"cache"。
"""
import logging
from datetime import datetime, timedelta

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
    """双源门面：优先 gm，异常/空结果降级腾讯。

    GM 熔断（2026-09-08）：gm._ready 只校验 token 不验连接，掘金终端未启动/断连时每次
    history_n 都抛 1001"无法连接终端服务"→ 逐调用 warning。故加 60s 冷却：首次不可达告警一次，
    窗内直接走腾讯（不再逐调用重试刷屏）；窗后恢复尝试，成功即复位。
    """
    _GM_COOLDOWN_SECONDS = 60

    def __init__(self):
        # H1/G2(2026-09-09): GmProvider 构造容错——gm SDK/解释器不可用时置 None，腾讯兜底不再被绑架
        # （09-08/09-09 事故：无 gm 解释器重启 → facade 构造期 import gm 爆炸 → 腾讯兜底陪葬 → 盘后全 0）
        try:
            self._gm = GmProvider()
        except Exception as e:
            log.warning("gm provider 初始化失败(gm SDK/解释器不可用): %s → 本次运行全部走腾讯兜底",
                        str(e)[:120])
            self._gm = None
        self._tx = TencentProvider()
        self._gm_down_until = None

    def _gm_ready(self) -> bool:
        return bool(self._gm is not None and getattr(self._gm, "_ready", False))

    def _gm_ok(self) -> bool:
        """可尝试 gm = 已 ready 且不在不可达冷却窗内。"""
        return self._gm_ready() and (self._gm_down_until is None
                                     or datetime.now() >= self._gm_down_until)

    def _gm_ok_reset(self) -> None:
        self._gm_down_until = None

    def _note_gm_down(self, ctx: str, key: str, exc: Exception) -> None:
        """gm 调用失败（多为终端服务不可达）→ 置冷却窗并每窗只告警一次。"""
        now = datetime.now()
        if self._gm_down_until is None or now >= self._gm_down_until:
            self._gm_down_until = now + timedelta(seconds=self._GM_COOLDOWN_SECONDS)
            log.warning("gm 服务不可达(%s %s: %s) → %ds 内直接走腾讯，不再逐调用重试",
                        ctx, key, str(exc)[:100], self._GM_COOLDOWN_SECONDS)
        else:
            self._gm_down_until = now + timedelta(seconds=self._GM_COOLDOWN_SECONDS)  # 续窗

    def _mark(self, df: pd.DataFrame, src: str) -> pd.DataFrame:
        df.attrs["source"] = src
        return df

    def daily(self, code: str, days: int = 800, period: str = "day") -> pd.DataFrame:
        # 2026-08-31 手动盘数据源与自动盘对齐：去掉腾讯 cache-first，gm 优先（腾讯仅降级兜底）。
        # 自动盘(gm_main)纯 gm 直拉；手动盘此处同样优先 gm，保证两侧数据/判定一致。
        if self._gm_ok():
            try:
                df = self._gm.daily(code, days)
                if df is not None and not df.empty:
                    # 阻断5: gm 日线对当日 forming bar（带 ts_date 新鲜度闸；窗口至收盘后16:00，重审#7）
                    df = self._maybe_append_forming(df, code)
                    # 阻断6: gm 结果写缓存（供 gm 不可用时段兜底；含盘中 15 分钟新鲜度/B-1，由 tencent 缓存读取端执行）
                    from .tencent_provider import save_daily_cache
                    save_daily_cache(code, df)
                    self._gm_ok_reset()   # 连通成功 → 复位熔断
                    return self._mark(_resample_period(df, period), "gm")
                # F-6(2026-09-04): gm 静默返空（588170 ETF 疑单点依赖腾讯）——补 warning 可观测
                self._gm_ok_reset()
                log.warning("gm.daily 返回空(%s) → 降级腾讯（ETF 疑 gm 静默返空）", code)
            except Exception as e:
                self._note_gm_down("daily", code, e)
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
        # F-1(2026-09-04): 窗口延至 23:59——16:00 后 gm 日线仍无当日 bar，eod 重扫(16:04)会退回昨日
        # 判出伪 signal(688037)；盘后快照 ts_date=当日闸已防伪造 bar，延窗只补真实收盘。
        if _now.weekday() >= 5 or not ("09:15" <= _now.strftime("%H:%M") <= "23:59"):
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
        # F-1(2026-09-04): 窗口延至 23:59——16:00 后 gm 日线仍无当日 bar，eod 重扫(16:04)会退回昨日
        # 判出伪 signal(688037)；盘后快照 ts_date=当日闸已防伪造 bar，延窗只补真实收盘。
        if _now.weekday() >= 5 or not ("09:15" <= _now.strftime("%H:%M") <= "23:59"):
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
        if self._gm_ok():
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
                    self._gm_ok_reset()   # 连通成功 → 复位熔断
                    return self._mark(df, "gm")
            except Exception as e:
                self._note_gm_down("minute", f"{code} {date}", e)
        df = self._tx.minute(code, date, ttl_seconds)
        return self._mark(df, df.attrs.get("source", "tencent"))

    def snapshot(self, codes: list) -> dict:
        # 快照契约是 dict 非 DataFrame，source 无法进 attrs——gm 优先，空/异常回退腾讯
        if self._gm_ok():
            try:
                out = self._gm.snapshot(codes)
                if out:
                    self._gm_ok_reset()
                    return out
            except Exception as e:
                self._note_gm_down("snapshot", ",".join(str(c) for c in codes[:3]), e)
        return self._tx.snapshot(codes)

    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        if self._gm_ok():
            try:
                df = self._gm.index_daily(index, days, end_date)
                if df is not None and not df.empty:
                    # 阻断6: gm 指数结果写缓存（end_date 缺省时）
                    if end_date is None:
                        # 2026-08-31: 补当日 forming bar（gm 盘中不含当日指数），否则 regime 用昨日收盘
                        df = self._maybe_append_index_forming(df, index)
                        from .tencent_provider import save_index_daily_cache
                        save_index_daily_cache(index, df)
                    self._gm_ok_reset()
                    return self._mark(df, "gm")
            except Exception as e:
                self._note_gm_down("index_daily", index, e)
        df = self._tx.index_daily(index, days, end_date)
        return self._mark(df, df.attrs.get("source", "tencent"))


_facade_singleton = None


def get_provider():
    """返回数据源门面（单例）。默认 gm 主源，失败降级腾讯。"""
    global _facade_singleton
    if _facade_singleton is None:
        _facade_singleton = MarketDataFacade()
    return _facade_singleton
