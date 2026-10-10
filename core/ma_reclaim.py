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
