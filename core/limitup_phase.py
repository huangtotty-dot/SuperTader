# -*- coding: utf-8 -*-
"""涨停股「阶段」分类（2026-11-15 owner：异常监控池按时期分类 + 找介入点）。

判词见 doc/research/2026-11-15_涨停股介入时机与阶段分类.md：
  · 介入点只有「追涨」：打板隔日冲高 / 断板后反包；次日追买、回踩低吸全负；
  · 高度板断板是最大风险（三板+断板 H=5 −5.96%）。
阶段 = **纯组织标签**（无预测 claim），只按「今日是否涨停 / 昨日是否涨停 / 最近涨停日距今」
把涨停股分 6 类，帮 owner 一眼区分「今天能动什么」与「断板后已经走远的死单」。

本模块只有纯函数 + 常量，不 import 行情/配置依赖（与 core/rules 同层）。
"""
from __future__ import annotations

from core.rules import limit_pct as _limit_pct

# 阶段排序权重（小 = 靠前）。后端 `_pool_rows` 用它分组排序，前端只渲染。
PHASE_ORDER = {
    "连板中": 0,
    "反包": 1,
    "今日首板": 2,
    "断板D+1": 3,
    "断板回踩": 4,
    "断板走远": 5,
    "无近期涨停": 6,
    "无数据": 7,
}

# 反包判定：断板后再次涨停，上一次涨停在距今 2~10 日内（更早视为新一轮首板）。
_REBOUND_LOOKBACK = 10


def _is_limitup(prev_close, close, high, thr) -> bool:
    """收盘涨停：涨幅达标 且 close==high（封板）；复牌/首日异常（>阈值+12pp）剔除。"""
    if prev_close <= 0 or thr <= 0:
        return False
    pct = (close - prev_close) / prev_close
    if pct < thr * 0.995:
        return False
    if abs(close - high) > 1e-6:
        return False
    if pct > thr + 0.12:
        return False
    return True


def _consecutive(lu, i) -> int:
    """以 i 结尾的连续涨停天数。"""
    n = 1
    j = i - 1
    while j >= 0 and lu[j]:
        n += 1
        j -= 1
    return n


def limitup_phase(rows, date, code: str = "", name: str = "", thr=None) -> dict:
    """给定日线 rows（升序 dict 列表，含 date/open/close/high/low）+ 目标日期 → 阶段 dict。

    返回 {"phase", "board", "gap", "rebound"}：
      · phase ∈ 连板中 / 反包 / 今日首板 / 断板D+1 / 断板回踩 / 断板走远 / 无近期涨停 / 无数据；
      · board = 涨停日的连板数（连板中=当前板数，断板态=断板前那段连板数）；
      · gap   = 最近涨停日距今的天数（连板中/首板/反包=0，无近期涨停=None）；
      · rebound = 是否「断板后反包」（=phase=="反包"）。
    `date` 缺行或缓存停在更早 → 以最后一行作「今日」。纯函数、不抛。
    """
    if not rows:
        return {"phase": "无数据", "board": 0, "gap": None, "rebound": False}
    if thr is None:
        thr = _limit_pct(code, is_st=("ST" in str(name or "").upper()))
    thr = float(thr) / 100.0
    n = len(rows)
    try:
        closes = [float(r["close"]) for r in rows]
        highs = [float(r["high"]) for r in rows]
        dates = [str(r.get("date") or "") for r in rows]
    except Exception:
        return {"phase": "无数据", "board": 0, "gap": None, "rebound": False}

    # 目标日：最后一行 <= date 的那行；date 为空或缓存陈旧则取最后一行。
    ti = n - 1
    target = str(date or "")
    if target:
        for k in range(n - 1, -1, -1):
            if dates[k] <= target:
                ti = k
                break

    lo = max(1, ti - (_REBOUND_LOOKBACK + 1))
    lu = [False] * n
    for k in range(lo, n):
        lu[k] = _is_limitup(closes[k - 1], closes[k], highs[k], thr)

    lu_today = lu[ti] if ti >= 0 else False
    lu_yest = lu[ti - 1] if ti >= 1 else False

    if lu_today:
        board = _consecutive(lu, ti)
        if lu_yest:
            return {"phase": "连板中", "board": board, "gap": 0, "rebound": False}
        # 今日涨停但昨日非涨停：反包（断板后再次涨停） vs 首板（近期无涨停）
        prior = any(lu[k] for k in range(max(1, ti - _REBOUND_LOOKBACK), ti - 1))
        if prior:
            return {"phase": "反包", "board": board, "gap": 0, "rebound": True}
        return {"phase": "今日首板", "board": board, "gap": 0, "rebound": False}

    # 今日未涨停：找最近涨停日 L
    L = None
    for k in range(ti, max(-1, ti - _REBOUND_LOOKBACK - 1), -1):
        if lu[k]:
            L = k
            break
    if L is None:
        return {"phase": "无近期涨停", "board": 0, "gap": None, "rebound": False}
    gap = ti - L
    board = _consecutive(lu, L)
    if gap == 1:
        return {"phase": "断板D+1", "board": board, "gap": 1, "rebound": False}
    if gap <= 3:
        return {"phase": "断板回踩", "board": board, "gap": gap, "rebound": False}
    return {"phase": "断板走远", "board": board, "gap": gap, "rebound": False}
