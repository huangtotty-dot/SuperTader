# -*- coding: utf-8 -*-
"""数据源门面（合并实施方案 P1-1）：默认 gm 主源，失败/超时降级腾讯。
每次降级 log.warning；返回 DataFrame 统一在 attrs["source"] 标记 "gm"/"tencent"/"cache"。
"""
import concurrent.futures as _cf
import logging
from datetime import datetime, timedelta

import pandas as pd

from .gm_provider import GmProvider
from .tencent_provider import TencentProvider

log = logging.getLogger("market_data.facade")

# 指数当日 forming bar 缓存（key=(index, today)，同一天内多次 index_daily 不重复拉腾讯分时；
# 手动扫描 24 只每只都会走 timing_gate→index_daily，避免每只都拉一次分时导致慢/限流）
_index_forming_cache = {}

# 指数昨收缓存（2026-09-28，index_snapshot 用）：GM current 不含昨收 → 由日线取。
# 昨收盘内恒定，按 (symbol, today) 缓存，避免每 10s 轮询都打一次日线。
_index_preclose_cache = {}


def _code_tail(code: str) -> str:
    """"sh000001"→"000001"；"SHSE.000001"→"000001"；"000001"→"000001"。
    （GM current 回的是六位码，需与调用方传入的符号对回去。）"""
    s = str(code).strip()
    if "." in s:
        return s.split(".")[-1].lower()
    if s[:2].lower() in ("sh", "sz", "bj"):
        return s[2:].lower()
    return s.lower()

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
    # P1-2(2026-09-10): gm SDK 无超时（history_n 可无限期挂死），线程池硬超时。
    # 2026-10-07: 12s → 4s。GM 抖动时单标的调用（daily/index_snapshot/minute）会卡满这个值，
    # 而它们被 GUI 的 js_api 同步调用 ⇒ 界面冻十几秒。单标的 GM 健康时 <1s，4s 足够宽裕；
    # 超时照旧走腾讯兜底。批量仍用 _GM_BATCH_TIMEOUT(60s)。
    _GM_CALL_TIMEOUT = 4.0

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
        # P1-2: 单 worker 池承载 gm 调用，超时即弃池重建（挂死线程不可回收）
        self._gm_pool = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-call")

    def _gm_ready(self) -> bool:
        return bool(self._gm is not None and getattr(self._gm, "_ready", False))

    def _gm_ok(self) -> bool:
        """可尝试 gm = 已 ready 且不在不可达冷却窗内。"""
        return self._gm_ready() and (self._gm_down_until is None
                                     or datetime.now() >= self._gm_down_until)

    def _gm_ok_reset(self) -> None:
        self._gm_down_until = None

    def _gm_call(self, desc: str, fn, *args, timeout: float = None, **kwargs):
        """P1-2(2026-09-10): 给 gm SDK 调用套线程池硬超时。
        gm 无超时，挂死既不返回也不抛 → 既有 except 熔断捕不到。超时抛 TimeoutError（内置），
        由调用点既有 `except Exception → _note_gm_down → 腾讯兜底` 接住；超时后丢弃该池重建。

        `timeout` 缺省用 `_GM_CALL_TIMEOUT`（12s，单标的调用）；批量调用（daily_batch）
        单次 350 只要 ~19s，必须显式放宽，否则会被 12s 误杀。"""
        if self._gm_pool is None:
            self._gm_pool = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-call")
        fut = self._gm_pool.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=self._GM_CALL_TIMEOUT if timeout is None else timeout)
        except _cf.TimeoutError:
            try:
                self._gm_pool.shutdown(wait=False)
            except Exception:
                pass
            self._gm_pool = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-call")
            raise TimeoutError(f"gm {desc} 调用超时>{self._GM_CALL_TIMEOUT}s（SDK 无超时挂死）")

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
        # 北交所分流（2026-09-29）：GM 与腾讯 K 线**均不供北交所**（实测 BJSE./BSE./BJ. 全 0 行；
        # 腾讯 ifzq 0/40），走 bj_daily 可插拔取数器。放在 GM 之前——否则白烧一次必然为空的 GM 调用。
        # 取不到返回空帧（不抛），由调用方跳过并计数。
        from .codec import market_of
        if market_of(code) == "BJ":
            return self._bj_daily(code, days, period)
        # 2026-08-31 手动盘数据源与自动盘对齐：去掉腾讯 cache-first，gm 优先（腾讯仅降级兜底）。
        # 自动盘(gm_main)纯 gm 直拉；手动盘此处同样优先 gm，保证两侧数据/判定一致。
        if self._gm_ok():
            try:
                df = self._gm_call("daily", self._gm.daily, code, days)
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

    def _bj_daily(self, code: str, days: int, period: str) -> pd.DataFrame:
        """北交所日线（可插拔取数器，见 `bj_daily` 模块）。永不抛；取不到返回空帧。"""
        from . import bj_daily
        df = bj_daily.fetch_bj_daily(code, days)
        if df is not None and not df.empty:
            try:
                from .tencent_provider import save_daily_cache
                save_daily_cache(code, df)      # 供 gm 不可用时段的通用兜底读端复用
            except Exception:
                pass
        return self._mark(_resample_period(df, period), "bj_em")

    # 批量日线参数（2026-09-29）。GM `history` 的上限是**总行数约 200k**
    # （1000 只×~200 根 ≈ 199k ✓；1500 只 ✗ GmError 1029），故批量上限取决于留窗长度。
    # 配合 `_lookback_start`（1.5 倍）⇒ `days=200` 约 200 根/只 ⇒ 900 只 ≈ 180k 行，留余量。
    # 实测 900 只一批 ≈ 7s（早先 400 只 19.1s 是**窗口过宽**所致，不是标的数）。
    _BATCH_MAX = 900
    _GM_BATCH_TIMEOUT = 60.0

    def daily_many(self, codes: list, days: int = 250) -> dict:
        """批量日线 → {6位码: DataFrame}。全池扫描专用，避免逐只 GM 串行。

        非北交所码走 GM `history` 批量（`_BATCH_MAX` 只/批）；北交所码分流到 bj_daily
        （可插拔，取不到则**该码不出现在结果里**）。

        **本方法不抛**：GM 不可用/超时/异常 → 已取到的部分照常返回，缺的码由调用方补空并计数。
        """
        from .codec import market_of
        codes = [str(c).split("_")[0] for c in (codes or []) if c]
        if not codes:
            return {}
        bj = [c for c in codes if market_of(c) == "BJ"]
        rest = [c for c in codes if market_of(c) != "BJ"]
        out = {}
        if rest and self._gm_ok():
            for i in range(0, len(rest), self._BATCH_MAX):
                chunk = rest[i:i + self._BATCH_MAX]
                try:
                    got = self._gm_call("daily_batch", self._gm.daily_batch, chunk, days,
                                        timeout=self._GM_BATCH_TIMEOUT)
                    if got:
                        out.update(got)
                    self._gm_ok_reset()
                except Exception as e:
                    self._note_gm_down("daily_batch", f"{len(chunk)}只", e)
                    break          # GM 不可达：不再逐批重试刷屏，剩余码由调用方补空
        elif rest:
            log.warning("daily_many: GM 不可用，%d 只非北交所码本轮无日线", len(rest))
        if bj:
            from . import bj_daily
            for idx, c in enumerate(bj):
                df = bj_daily.fetch_bj_daily(c, days)
                if df is not None and not df.empty:
                    out[c] = df
                elif bj_daily.LAST_ERROR is not None:
                    # 首次失败即判主机不可达，剩余同族码不再逐个重试——
                    # 否则 321 只 × 8 次重试 × 0.8s ≈ 37 分钟纯等待。
                    log.warning("daily_many: 北交所首次取数失败(%s)，跳过剩余 %d 只",
                                bj_daily.LAST_ERROR, len(bj) - idx - 1)
                    break
        return self._append_forming_batch(out, rest)

    def _append_forming_batch(self, frames: dict, codes: list) -> dict:
        """批量补当日 forming bar（与 `_maybe_append_forming` 逐条同闸口语义）。

        为什么要批量版：`_maybe_append_forming` 是**逐只**调 `self._tx.snapshot([code])`
        （一次 HTTP 一只），全池 5000+ 只会让扫描退回 5000+ 次往返——正是本次要消除的瓶颈。
        这里改用 `snapshot_auction`（一次 HTTP 打多只，且 `_tx_symbol` 已支持 bj）。

        闸口逐条对齐个股版：非工作日 / 不在 09:15-23:59 / 快照 `ts_date` 非当日 → 不补
        （否则伪造 bar，08-28 教训）。GM `history` 盘中同样不含当日 bar，故这一步不可省。
        """
        _now = datetime.now()
        today = _now.strftime("%Y-%m-%d")
        if (_now.weekday() >= 5 or not ("09:15" <= _now.strftime("%H:%M") <= "23:59")
                or not frames):
            return frames
        todo = [c for c in codes
                if c in frames and frames[c] is not None and not frames[c].empty
                and str(frames[c]["date"].iloc[-1]) < today]
        if not todo:
            return frames
        try:
            snaps = self._tx.snapshot_auction(todo)
        except Exception:
            return frames
        for code in todo:
            snap = snaps.get(code)
            if not snap or snap.get("ts_date") != today or not snap.get("price"):
                continue
            px = snap["price"]
            fb = pd.DataFrame([{"date": today, "open": snap.get("open") or px,
                                "high": snap.get("high") or px, "low": snap.get("low") or px,
                                "close": px, "volume": snap.get("vol_hand") or 0.0}])
            frames[code] = pd.concat([frames[code], fb], ignore_index=True)
        return frames

    def append_forming_bar(self, df: pd.DataFrame, code: str) -> pd.DataFrame:
        """公开薄封装（2026-10-04，纯加法）：给日线 df 补当日 forming bar。

        图表 cache-first 路径读磁盘历史后需要补当日那根；直接暴露 `_maybe_append_forming`
        的语义给 t_gui，避免调用私有方法。**不改变 `daily()` 的 GM-first 行为。**
        """
        try:
            return self._maybe_append_forming(df, code)
        except Exception:
            return df

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
                df = self._gm_call("minute", self._gm.minute, code, date)
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
                out = self._gm_call("snapshot", self._gm.snapshot, codes)
                if out:
                    self._gm_ok_reset()
                    return out
            except Exception as e:
                self._note_gm_down("snapshot", ",".join(str(c) for c in codes[:3]), e)
        return self._tx.snapshot(codes)

    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        if self._gm_ok():
            try:
                df = self._gm_call("index_daily", self._gm.index_daily, index, days, end_date)
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

    def _pre_close_of(self, symbol: str, today: str):
        """指数昨收（GM current 不提供 → 从日线取；按日缓存，盘内恒定只取一次）。

        ⚠️ **必须传 end_date**：provider 只在 `end_date is None` 时回写共享长历史缓存
        `t_io/cache/daily_kline/index_{sym}.json`。不带 end_date 发一个 days=5 的请求，
        会把 800 行的历史缓存**整个覆盖成 5 行**（2026-09-28 实测踩中，已修复）。
        带 end_date 同样拿到所需尾部，但不写缓存。
        """
        hit = _index_preclose_cache.get(symbol)
        if hit and hit[0] == today:
            return hit[1]
        try:
            df = self.index_daily(symbol, days=10, end_date=today)
            if df is None or df.empty:
                return None
            prev = df[df["date"].astype(str) < today]["close"]
            pre = float(prev.iloc[-1]) if len(prev) else None
            if pre:
                _index_preclose_cache[symbol] = (today, pre)
            return pre
        except Exception:
            return None

    def index_snapshot(self, codes: list) -> dict:
        """指数实时报价（2026-09-28，GUI 指数状态板）。返回 {调用方原样传入的代码: {...}}。

        GM 优先（`gm.api.current`，经 `_gm_index_symbol` 正确编码指数；**不可用** `snapshot()`
        ——其内部 `codec.to_gm` 按首位数字判市场，会把 sh000001 编成 SZSE.sh000001）。
        GM 不可用/抛错/返回空 → 腾讯 `index_auction`（qt.gtimg.cn）兜底。
        两路归一为同一形态：{price, pre_close, change, change_pct, open, high, low, volume, ts_date, source}

        注：GM `current` 的 payload **只有 14 个字段且不含昨收**（实测 SHSE.000001：
        price/open/high/low/cum_volume/cum_amount/created_at/trade_type…），故昨收改由日线取
        （`_pre_close_of`，按日缓存）。这是"GM 实时价 + GM 日线昨收"，未引入第二实时源。
        """
        codes = [str(c) for c in (codes or []) if c]
        if not codes:
            return {}
        if self._gm_ok():
            try:
                got = self._gm_call("index_snapshot", self._gm.index_snapshot, codes)
                if got:
                    # GM 回的是六位码（"000001"），调用方给的是 "sh000001" / "SHSE.000001"
                    tail2in = {_code_tail(c): c for c in codes}
                    today = datetime.now().strftime("%Y-%m-%d")
                    out = {}
                    for k, v in got.items():
                        sym = tail2in.get(str(k).lower(), str(k))
                        px = v.get("price")
                        pre = v.get("pre_close") or self._pre_close_of(sym, today)
                        chg = (px - pre) if (px and pre) else None
                        out[sym] = {**v, "pre_close": pre, "change": chg,
                                    "change_pct": (chg / pre * 100) if (chg is not None and pre) else None,
                                    "source": "gm"}
                    if out:
                        self._gm_ok_reset()
                        return out
            except Exception as e:
                self._note_gm_down("index_snapshot", " ".join(codes)[:60], e)
        # 腾讯兜底（字段[3]=最新价、[4]=昨收）
        try:
            tx = self._tx.index_auction(codes)
        except Exception:
            tx = {}
        out = {}
        for code, v in (tx or {}).items():
            price, pc = v.get("auction_price"), v.get("pre_close")
            out[code] = {
                "price": price, "pre_close": pc,
                "change": (price - pc) if (price and pc) else None,
                "change_pct": v.get("gap_pct"),
                "open": None, "high": None, "low": None, "volume": None,
                "ts_date": None, "name": v.get("name"), "source": "tencent",
            }
        return out

    def index_minute(self, index: str = "sh000688", count_bars: int = 800,
                     freq: str = "300s") -> pd.DataFrame:
        """指数分钟线（2026-09-21 起指数分钟数据源统一到掘金）。

        不回退腾讯：腾讯分时只给当日、喂不饱递推指标（RSI/MACD）的预热需求，
        且时间戳口径与掘金不同源，混用会制造新的不一致。gm 不可用时返回空表，
        由调用方决定「跳过本次」还是走别的路径。
        """
        if self._gm_ok():
            try:
                df = self._gm_call("index_minute", self._gm.index_minute, index, count_bars, freq)
                if df is not None and not df.empty:
                    self._gm_ok_reset()
                    return self._mark(df, "gm")
            except Exception as e:
                self._note_gm_down("index_minute", f"{index} {freq}", e)
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])


_facade_singleton = None


def get_provider():
    """返回数据源门面（单例）。默认 gm 主源，失败降级腾讯。"""
    global _facade_singleton
    if _facade_singleton is None:
        _facade_singleton = MarketDataFacade()
    return _facade_singleton
