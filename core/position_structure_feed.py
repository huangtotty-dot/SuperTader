# -*- coding: utf-8 -*-
"""core/position_structure_feed.py — 持仓结构层的**取数适配**（IO 全在这里）
====================================================================================
定位：把生产数据（tushare 5min/30min 磁盘缓存 + 日线 provider）喂给
      `core/position_structure.compute_structure`（该模块是**纯函数、无 IO/无网络**，
      纪律写在它的 docstring 里；取数一律留在本层，勿污染纯函数层）。

与「建仓信号扫描」保持一致：
  · 代码→ts_code 映射、分钟线缓存目录、日线 provider 与扫描/GUI 同源（`core.chart_cache`
    + `core.market_data`），不另起一套取数。
  · `date_str` 语义与扫描一致：None=今日实时；传历史日=**只读磁盘、截到该日**（零网络）。

数据源现实（2026-10-11 实测，决定本层为何这样写）：
  · tushare **无原生 10min** ⇒ 10min 由 5min 降采样（`position_structure.resample_5min_to_10min`，
    剔除 09:30 竞价棒）。
  · 5min 长历史档 `{ts}_5min_d540.json` 末根停在 2026-09-18（陈旧），**新鲜源只有 plain 档**，
    由 `chart_cache.prefetch_minute(freqs=..., "5min")` 盘后写入。故 live 路径缺新鲜档时出网。
  · 单只成本实测：5min ~0.5s、30min ~0.15s、日线 ~0.1s(常命中缓存)；算法本身 ~8ms。

⚠️ 本层只产出**结构参考**（止损位/阻力/补仓档/阶段），**不是买卖信号**。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pandas as pd

from core import position_structure as PS
from core import chart_cache as _cc

# 各周期取数窗口（自然日）；须满足 compute_structure 的最小根数要求
_DAYS_5MIN = 30          # ≈21 交易日 ≈1000 根 5min → ≈500 根 10min（要求 ≥80）
_DAYS_30MIN = 45         # ≈31 交易日 ≈250 根（要求 ≥75）
_FRESH_GAP_DAYS = 5      # 末根距今超过此自然日数 ⇒ 视为陈旧，live 路径出网补


def ts_code_of(code: str) -> str:
    """6 位码 / sh600000 / 600000.SH → tushare ts_code。认不出返回 ''。
    与 `t_gui._min_bars_ts_code` 同规则（后者不 import，避免 core→t_gui 反向依赖）。

    ⚠️ 已修 t_gui 版本的一个笔误：判「已是 ts_code 形式」时它写的是 `c[3] == "."`，
       而 `NNNNNN.XX` 的句点在 **索引 6**（`c[3]` 恒为数字）⇒ 该分支在 GUI 侧实际从不命中
       （现网无影响：调用方传的都是 6 位码；此处按正确索引实现，勿回抄旧写法）。"""
    c = str(code).split("_")[0].strip()
    pre = c[:2].lower()
    if pre in ("sh", "sz", "bj") and c[2:].isdigit():
        return c[2:] + "." + {"sh": "SH", "sz": "SZ", "bj": "BJ"}[pre]
    if len(c) == 9 and c[6] == "." and c[:6].isdigit():
        return c.upper()
    if len(c) == 6 and c.isdigit():
        ex = "SH" if c[0] in "56" else ("BJ" if c[0] in "48" else "SZ")
        return f"{c}.{ex}"
    return ""


def _in_session(now: datetime) -> bool:
    """A 股连续竞价时段（含午休边界）：09:30-11:35 / 12:55-15:05，仅工作日。"""
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return (9, 30) <= hm <= (11, 35) or (12, 55) <= hm <= (15, 5)


def _fresh_enough(df: pd.DataFrame, now: datetime = None) -> bool:
    """够新判定（决定 live 路径是否出网补）：

      · **盘中**（09:30–11:35 / 12:55–15:05）→ 末根须是**当日**且滞后 **≤35min**
        （=1 个 30min 时段 + 5min 余量）⇒ 每个槽位最多重取一次；
      · **当日收盘后**（15:05 之后的工作日）→ 末根须是**当日**（把 15:00 那根最终 bar 取回来）；
      · **盘前 / 周末 / 节假日** → 末根在 _FRESH_GAP_DAYS 自然日内（覆盖周末与短假）。

    ⚠️ 2026-10-30 之前盘中只判「日期=今天」：数据源换成腾讯后该条件**整天满足**，
    结构会冻结在上午第一根；而换源前（tushare 盘中只给到上一交易日）它又**永不满足**，
    每槽位对每只票重打一次无效网络。两个方向都不对。
    """
    now = now or datetime.now()
    if df is None or df.empty:
        return False
    try:
        last = pd.Timestamp(df["time"].iloc[-1]).to_pydatetime()
    except Exception:
        return False
    if _in_session(now):
        # 2026-10-30（owner：「那两处口径改动 做」）：原来只判「末根日期 == 今天」——
        # 一旦数据源真能给出今天的首根（腾讯），这个判据就**整天都满足**，结构会冻结在
        # 上午第一次取到的那根。改成「滞后 ≤ 1 个 30min 时段（+5min 余量）」：
        #   · 每个槽位最多重取一次（腾讯 m30 右端时间最多滞后 30min）；
        #   · 09:30~10:00 首根还没出来时，取回来仍不新鲜 ⇒ 走 `_net_cooldown` 的 10 分钟冷却，
        #     不会变成每 10s 一次的轮询风暴。
        return (last.date() >= now.date()) and (now - last) <= pd.Timedelta(minutes=35)
    # 2026-10-30：**收盘后（15:05~24:00）要求末根是今天**——否则「当日已收盘」的最终 bars
    # 永远进不来（旧规则在 5 个自然日内一律算新鲜 ⇒ 收盘后还停在昨天）。取不到就走冷却。
    if now.weekday() < 5 and (now.hour, now.minute) > (15, 5):
        return last.date() >= now.date()
    return (now - last).days <= _FRESH_GAP_DAYS


# ── 出网冷却（2026-10-14）──────────────────────────────────────────────────
# 病根：`_fresh_enough` 在**盘中**要求末根是**当日**，而 tushare `stk_mins` 盘中只给到
#   **上一交易日**收盘（实测 2026-10-14 14:13 取回来仍止于 10-13 15:00）⇒ 该判据盘中
#   **永远不可能满足** ⇒ 每轮预热都对同一只票重打网络 + 重写盘 + 重解析，永不收敛。
#   实测：20 分钟内 245 个 plain 档被改写、GUI 进程 CPU 长期 ~45%、pywebview 结果投递被拖死。
# 处置：**「出网了还是不够新」就冷一段时间**（数据源滞后不是本进程能修的）。
#   ⚠️ 不是关闭出网：冷却期一过照样重试，盘后源追上了自然恢复 `disk+net`。
_NET_RETRY_COOLDOWN_S = 600.0
_NET_COOLDOWN: dict = {}
_NET_COOLDOWN_LOCK = threading.Lock()


def _net_cooling(ts_code: str, freq: str) -> bool:
    with _NET_COOLDOWN_LOCK:
        t = _NET_COOLDOWN.get((ts_code, freq))
    return t is not None and (time.monotonic() - t) < _NET_RETRY_COOLDOWN_S


def _net_cooldown_start(ts_code: str, freq: str) -> None:
    with _NET_COOLDOWN_LOCK:
        if len(_NET_COOLDOWN) > 5000:          # 防无界增长（全池也才几千只）
            _NET_COOLDOWN.clear()
        _NET_COOLDOWN[(ts_code, freq)] = time.monotonic()


def _net_cooldown_reset() -> None:
    """清空冷却表（测试/排障用）。"""
    with _NET_COOLDOWN_LOCK:
        _NET_COOLDOWN.clear()


def _minute_bars(ts_code: str, freq: str, days: int, date_str=None,
                 allow_network: bool = True) -> tuple:
    """磁盘优先（含长历史档），live 路径不新鲜且允许时出网补并落盘。
    返回 (df, source)：source ∈ {disk, net, disk+net, none}。

    `disk` 也覆盖「**刚试过出网没拿到更新的**」——那属于数据源滞后，不重复打网络（见上方冷却块）。
    """
    df = _cc.load_minute_asof(ts_code, freq, date_str)
    if date_str:                       # 历史日：只读磁盘（零网络），不做新鲜度判断
        return df, ("disk" if not df.empty else "none")
    if _fresh_enough(df):
        return df, "disk"
    if not allow_network:
        return df, ("disk" if not df.empty else "none")
    if _net_cooling(ts_code, freq):    # 刚出网过、源还是老的 ⇒ 本轮直接吃磁盘
        return df, ("disk" if not df.empty else "none")
    # 2026-10-30：**腾讯优先**（盘中唯一实时源，含未收盘根）；tushare 退为兜底
    # （北交所腾讯返回空、或腾讯接口异常时）。两者同口径：量=手、不复权。
    try:
        net = _cc.fetch_minute_tencent(ts_code, freq)
    except Exception:
        net = None
    if net is None or getattr(net, "empty", True):
        try:
            net = _cc.fetch_minute_online(ts_code, freq, days)
        except Exception:
            net = None
    if net is None or net.empty:
        _net_cooldown_start(ts_code, freq)
        return df, ("disk" if not df.empty else "none")
    try:
        _cc.save_minute_history(ts_code, freq, net)      # 并入 plain 档（内容没变则不落盘）
    except Exception:
        pass
    merged = _cc.load_minute_asof(ts_code, freq, date_str)
    if not _fresh_enough(merged):
        _net_cooldown_start(ts_code, freq)               # 拿了还是不够新 ⇒ 源滞后，冷却
    return merged, ("disk+net" if not df.empty else "net")


def prefetch_minutes(code: str, date_str: str = None, allow_network: bool = True) -> dict:
    """把 `load_structure` 要用的两档分钟线**先取回来**（纯 IO），返回 {freq: src}。

    为什么单独拆出来（2026-10-30，owner「继续优化 CPU」）：
    `_kick_pb_struct_warm._one` 原来在 `with _BG_PANDAS_SEM:` **里面**调 `load_structure`，
    而该函数默认出网 ⇒ 2 个 pandas 名额被**网络等待**占住（实测单只出网 0.9~3.6s、
    纯磁盘 0.12s），标签批（同样要这 2 个名额）排在后面 ⇒ 每 30 分钟边界 3~5 分钟的尖峰。
    拆开后：取数在信号量外并发（IO 不吃名额），信号量里只做 CPU。

    与 `load_structure` 内部**完全同参**（freq/days/date_str 一致）；取回即落盘
    （`_minute_bars` 内部 save），随后 `load_structure(..., allow_network=False)` 读盘得到同一份。
    失败静默（返回值里对应 src 仍是 none/disk，由调用方照实展示）。
    """
    ts_code = ts_code_of(code)
    if not ts_code:
        return {}
    out = {}
    for freq, days in (("5min", _DAYS_5MIN), ("30min", _DAYS_30MIN)):
        try:
            _df, src = _minute_bars(ts_code, freq, days, date_str, allow_network)
            out[freq] = src
        except Exception:
            pass
    return out


def load_structure(code: str, date_str: str = None, allow_network: bool = True,
                   daily_df: pd.DataFrame = None) -> dict:
    """取数 → 结构层。**不抛**：任何失败返回 {"ok": False, "reason": ...}。

    code        : 6 位代码（或 ts_code）
    date_str    : None=今日实时；'YYYY-MM-DD'=历史日（纯磁盘、截到该日）
    allow_network: 历史回放/热路径传 False（零网络）
    daily_df    : 已取好的日线（扫描侧可复用，省一次 provider 调用）
    """
    ts_code = ts_code_of(code)
    if not ts_code:
        return {"ok": False, "reason": f"代码无法识别: {code}"}

    b5, src5 = _minute_bars(ts_code, "5min", _DAYS_5MIN, date_str, allow_network)
    if b5.empty:
        return {"ok": False, "reason": "无 5min 数据（结构层需 10min，tushare 无原生 10min）"}
    b30, src30 = _minute_bars(ts_code, "30min", _DAYS_30MIN, date_str, allow_network)
    if b30.empty:
        return {"ok": False, "reason": "无 30min 数据"}

    # 日线：优先复用调用方已取的 → **本地日线缓存** → 最后才走 provider。
    #
    # ⚠️ 2026-10-17（owner：「列更新还是很慢，彻底解决」）：原来**每只票都走
    #   `get_provider().daily(code)`**。实测同一批持仓里，有的 0.19s、有的 **3.73s** ——
    #   后者是 GM 的 4.5s 限流窗（见 [[gm_not_unreachable_probe_window]]）把调用推给了腾讯。
    #   而这一层是**按只串行/低并发**的（预热池 2 worker）⇒ 十只持仓就能卡出十几秒 ⏳。
    #   本地日线缓存（`chart_cache.load_daily_display`，26ms/只、带 12 天新鲜度闸）与 provider
    #   同源（猎手跑完会把当日 bar 回写进去），没有理由每只都去碰网络。
    if daily_df is None or getattr(daily_df, "empty", True):
        try:
            from core import chart_cache as _cc_daily
            _c6 = str(code).split("_")[0]
            _c6 = _c6[-6:] if len(_c6) >= 6 else _c6
            daily_df = _cc_daily.load_daily_display(_c6)
        except Exception:
            daily_df = None
    if daily_df is None or getattr(daily_df, "empty", True):
        try:
            from core.market_data import get_provider
            daily_df = get_provider().daily(code)
        except Exception as e:
            return {"ok": False, "reason": f"日线取数失败: {str(e)[:60]}"}
    if daily_df is None or daily_df.empty:
        return {"ok": False, "reason": "无日线数据"}

    dcols = {str(c).lower(): c for c in daily_df.columns}
    need = ("open", "high", "low", "close")
    if not all(k in dcols for k in need):
        return {"ok": False, "reason": f"日线缺列: {list(daily_df.columns)}"}
    _datecol = dcols.get("date") or dcols.get("trade_date")
    if _datecol is None:
        return {"ok": False, "reason": "日线缺日期列"}
    dly = pd.DataFrame({
        "date": pd.to_datetime(daily_df[_datecol]),
        "open": daily_df[dcols["open"]].astype(float),
        "high": daily_df[dcols["high"]].astype(float),
        "low": daily_df[dcols["low"]].astype(float),
        "close": daily_df[dcols["close"]].astype(float),
    })
    if date_str:                       # 历史日：日线也截到该日（防未来函数）
        dly = dly[dly["date"] <= pd.Timestamp(date_str)].reset_index(drop=True)

    # ⚠️ 2026-10-17（owner：「列更新还是很慢，彻底解决」）：`load_minute_asof` 读回来的是
    # **合并后的全量长历史**（5min 实测 18,326 根 = 十几年的档），而 `_DAYS_5MIN/_DAYS_30MIN`
    # 只用于**出网补数**、从来没用它截过盘上数据。原样丢进 resample/compute ⇒ **白算 12 倍**：
    # cProfile 单只 0.5s 里 resample 0.22s + compute 0.46s 几乎全是这堆多余数据。
    # 按需截尾（5min 每交易日 48 根+竞价 ⇒ 按 60 根/日留余量；30min 按 9 根/日）。
    _K5 = int(_DAYS_5MIN * 60)
    if len(b5) > _K5:
        b5 = b5.tail(_K5).reset_index(drop=True)
    _K30 = int(_DAYS_30MIN * 9)
    if len(b30) > _K30:
        b30 = b30.tail(_K30).reset_index(drop=True)

    b10 = PS.resample_5min_to_10min(b5.rename(columns={"time": "dt"}))
    st = PS.compute_structure(b10, b30.rename(columns={"time": "dt"}), dly)

    # 数据口径透明化：消费方必须能看到"这份结构截至哪根、走的哪个源、是否陈旧"
    # ⚠️ 陈旧必须显式暴露：5min 长历史档 `_d540` 停在 2026-09-18，若目标日在其后且无 plain 档，
    #    10min 会静默止于 09-18（本仓踩过的 idx_daily 静默截短同类坑）⇒ 由 stale 标记兜住。
    _d10 = pd.Timestamp(b10["dt"].iloc[-1]).normalize() if not b10.empty else None
    _d30 = pd.Timestamp(b30["time"].iloc[-1]).normalize()
    _dd = pd.Timestamp(dly["date"].iloc[-1]).normalize() if not dly.empty else None
    if date_str:
        _req = pd.Timestamp(date_str).normalize()
        _lags = [f"{n}止于{d.date()}" for n, d in
                 (("10min", _d10), ("30min", _d30), ("日线", _dd)) if d is not None and d < _req]
        stale, stale_reason = bool(_lags), ("；".join(_lags) + f"，早于目标日 {_req.date()}" if _lags else None)
    else:
        _age = (pd.Timestamp.now().normalize() - _d10).days if _d10 is not None else 999
        stale = _age > 4                       # >4 自然日覆盖周末+短假
        stale_reason = f"10min 止于 {_d10.date()}（{_age} 天前）" if stale else None
    st["data_asof"] = {
        "bars10_last": str(b10["dt"].iloc[-1]) if not b10.empty else None,
        "bars30_last": str(b30["time"].iloc[-1]),
        "daily_last": str(dly["date"].iloc[-1].date()) if not dly.empty else None,
        "n_bars10": int(len(b10)), "n_bars30": int(len(b30)), "n_daily": int(len(dly)),
        "src_5min": src5, "src_30min": src30,
        "stale": stale, "stale_reason": stale_reason,
    }
    st["text"] = PS.format_structure(st)          # 展示用整段文本（tooltip 单一真源）
    return st
