# -*- coding: utf-8 -*-
"""均线回站（刚刚站上）判定 —— 口径单一源（2026-10-09）。

reclaim5 = 昨收 < 昨MA5 且 今价 > 今MA5：
  basis = 截至昨日收盘；prev_MA5 = mean(basis[-5:])；cur_MA5 = (sum(basis[-4:]) + price) / 5。
与 core/position_builder.check_ma_break 口径一致；此处是「日线帧」版，供**批量**扫描
（t_gui 全市场站上5日线扫描 / 猎手热门板块飞书告警）共用，避免两处实现漂移。
"""
import numpy as np


def ma5_state(closes) -> dict:
    """closes：日线收盘序列，末根=当日（forming bar 的 close 即最新价）。

    返回**完整状态**而非仅命中与否——盘中「曾破线后拉回」（V 反转）需要知道「此刻是否在
    MA5 下方」，而 `reclaim5_from_closes` 只回答「是否刚隔夜回站」，答不了那个问题。
    不足 6 根 / 价格非法 → None。

    - `above` 现价在 MA5 上方；`below` 现价在 MA5 下方
    - `overnight_reclaim` 昨收 < 昨MA5 且现价 > 今MA5（隔夜回站，原 reclaim5 口径）
    """
    if closes is None or len(closes) < 6:
        return None
    price = float(closes[-1])
    basis = np.asarray(closes[:-1], dtype=float)     # 截至昨日
    if len(basis) < 5 or price <= 0:
        return None
    prev_close = float(basis[-1])
    prev_ma5 = float(np.mean(basis[-5:]))
    cur_ma5 = float((np.sum(basis[-4:]) + price) / 5.0)
    if cur_ma5 <= 0:
        return None
    return {
        "price": price, "prev_close": prev_close,
        "ma5_prev": prev_ma5, "ma5": cur_ma5,
        "above": price > cur_ma5, "below": price < cur_ma5,
        "overnight_reclaim": bool(prev_close < prev_ma5 and price > cur_ma5),
        "dev5_pct": (price - cur_ma5) / cur_ma5 * 100,
    }


def _round_state(s: dict) -> dict:
    return {"price": round(s["price"], 3), "prev_close": round(s["prev_close"], 3),
            "ma5_prev": round(s["ma5_prev"], 3), "ma5": round(s["ma5"], 3),
            "dev5_pct": round(s["dev5_pct"], 2)}


def reclaim5_from_closes(closes) -> dict:
    """隔夜回站命中返回 {price, prev_close, ma5_prev, ma5, dev5_pct}；否则 None。"""
    s = ma5_state(closes)
    return _round_state(s) if (s and s["overnight_reclaim"]) else None


def reclaim5_frame(df, target) -> dict:
    """日线帧版：先按 target 切片（去掉目标日之后，防历史扫描前视），要求末根==target
    （交易日闸），再交 reclaim5_from_closes。df 需含 date/close 列。"""
    if df is None or df.empty:
        return None
    target = str(target)
    df = df[df["date"].astype(str) <= target]
    if df.empty or str(df["date"].iloc[-1]) != target:
        return None
    return reclaim5_from_closes(df["close"].astype(float).values)


# ══════════════════════════════════════════════════════════════════════
# 任意周期（5/10/20/30/60）的「当日回站」+ **突破时刻**（2026-10-23 owner）
#
# owner：「建仓信号扫描 / 30分钟风险体检 / 异常监控池 里，某只票当日刚刚突破 5/10/20/30/60
# 日线，请标注出突破的时间」。
#
# 口径与上面的 `ma5_state` **逐字一致**（只是把 5 换成任意周期 p）：
#   · 昨 MA(p) = mean(截至昨日的 p 根收盘)（含昨日）
#   · 今 MA(p) = (前 p-1 根收盘 + 今日现价) / p（含今日现价的滚动均线，与日线标签同口径）
#   · 回站 = 昨收 < 昨MA(p) 且 现价 > 今MA(p)
# 「突破时刻」用今日分钟 bar 逐根回放：某根 bar 的收盘价 > 该时刻的 MA(p) 的第一根，即为突破时刻。
# 注意化简后阈值是**常量** `mean(前 p-1 根收盘)`（因为今日价同时出现在两边），所以日内不会漂移。
# ══════════════════════════════════════════════════════════════════════
RECLAIM_PERIODS = (5, 10, 20, 30, 60)


def reclaim_state(closes, period: int):
    """`ma5_state` 的任意周期版。closes 末根 = 今日最新价（与 ma5_state 同约定）。

    返回 {period, price, prev_close, ma_prev, ma, above, reclaim, dev_pct}；数据不足/非法 → None。
    """
    if closes is None or len(closes) < period + 1:
        return None
    arr = np.asarray(closes, dtype=float)
    price = float(arr[-1])
    basis = arr[:-1]
    if price <= 0 or len(basis) < period:
        return None
    prev_close = float(basis[-1])
    ma_prev = float(np.mean(basis[-period:]))
    ma_cur = float((np.sum(basis[-(period - 1):]) + price) / period)
    if ma_cur <= 0:
        return None
    return {
        "period": int(period), "price": price, "prev_close": prev_close,
        "ma_prev": ma_prev, "ma": ma_cur,
        "above": bool(price > ma_cur),
        "reclaim": bool(prev_close < ma_prev and price > ma_cur),
        "dev_pct": (price - ma_cur) / ma_cur * 100.0,
    }


def reclaim_threshold(closes_hist, period: int):
    """当日「站上 MA(p)」的价格阈值 = mean(截至昨日的前 p-1 根收盘)。数据不足 → None。"""
    if closes_hist is None:
        return None
    basis = np.asarray(closes_hist, dtype=float)
    if len(basis) < period - 1 or period < 2:
        return None
    return float(np.mean(basis[-(period - 1):]))


def reclaim_first_time(closes_hist, period: int, bar_times, bar_closes):
    """今日**第一次**站上 MA(p) 的分钟 bar 时刻（"HH:MM"）。找不到 → None。

    `closes_hist` 截至**昨日**（不含今日）；`bar_times/bar_closes` 为今日分钟序列（升序）。
    ⚠️ 用 bar 的**收盘价**判定（与日线口径一致），不是最高价——盘中冲高回落不算站上。
    """
    thr = reclaim_threshold(closes_hist, period)
    if thr is None:
        return None
    for t, c in zip(bar_times, bar_closes):
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c > thr:
            return str(t)[11:16] if len(str(t)) >= 16 else str(t)
    return None


def reclaim_today(closes_hist, bars, periods=RECLAIM_PERIODS) -> dict:
    """当日回站汇总 → {period(int): {time, ma, dev_pct}}（只含**今天刚回站且有时刻**的周期）。

    `closes_hist`：截至昨日的日线收盘；`bars`：今日分钟帧（需 time/close 两列，升序）。
    没有分钟数据 / 不在回站状态 / 找不到时刻 ⇒ 该周期不出现（前端显示 —）。
    """
    try:
        if bars is None or getattr(bars, "empty", True) or len(bars) < 1:
            return {}
        bar_times = list(bars["time"])
        bar_closes = list(bars["close"])
    except Exception:
        return {}
    price = None
    for c in reversed(bar_closes):
        try:
            price = float(c)
        except (TypeError, ValueError):
            continue
        if price > 0:
            break
    if not price or price <= 0:
        return {}
    closes = list(closes_hist) + [price]
    out = {}
    for p in periods:
        st = reclaim_state(closes, p)
        if not st or not st["reclaim"]:
            continue
        t = reclaim_first_time(closes_hist, p, bar_times, bar_closes)
        if not t:
            continue
        out[int(p)] = {"time": t, "ma": round(st["ma"], 3), "dev_pct": round(st["dev_pct"], 2)}
    return out
