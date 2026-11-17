# -*- coding: utf-8 -*-
"""两市成交额「昨日同期」分母：按秒插值（2026-11-17）。

**为什么需要这个模块**：成交额的分子（实时快照）是 10 秒级，分母（昨日同期累计）只有
5 分钟粒度，而 5min bar 按**右端**打戳 ⇒ 拿「≤现在 的最后一个点」当分母 = 拿"今天 10:05:29"
去比"昨天 10:00"，早盘一根 5min bar 就是 400~500 亿。实测 2026-11-17 10:05：
分母 4,878.7 亿 vs 真实同期 5,355.9 亿 ⇒ 浮窗显示 "-61 亿 / -1.24%"，真实 "-538 亿 / -10.0%"
（财联社当时 -12.2%）。

消费方 = 多窗口看盘的成交额小卡（`gui.Api.load_turnover_live`）。原桌面浮窗
`scripts/turnover_widget.py` 已于 2026-11-17 删除，其专用的 `choose_base` 一并移除。

纯函数、无 IO、无时钟依赖（`now` 可注入），故可全离线测。
"""
from __future__ import annotations

import time


def _hms(now):
    """(时, 分, 秒) —— 同时吃 `time.struct_time`（浮窗）和 `datetime`（gui）。"""
    for a, b, c in (("tm_hour", "tm_min", "tm_sec"), ("hour", "minute", "second")):
        if hasattr(now, a):
            return int(getattr(now, a)), int(getattr(now, b)), int(getattr(now, c))
    raise TypeError(f"不支持的时间对象: {type(now)!r}")


def interp_prev_base(cum, now=None):
    """在上一交易日的 **5min 累计曲线** 上按**当前秒**插值，作为「昨日同期」分母。

    `cum` = [[HH:MM, 元], …]（升序；乱序也容忍）。不足两点 / 解析失败 ⇒ None
    （调用方退回 5min 边界值 `amount_prev_same_hm`）。

    钳位而不外推：早于首点取首点、晚于末点取末点 —— 宁可少算"这根刚走的部分"，
    也不把误差放大。
    """
    now = now or time.localtime()
    pts = []
    for item in cum or []:
        try:
            hm, v = str(item[0]), float(item[1])
            h, m = hm.split(":")[:2]
            pts.append((int(h) * 60 + int(m), v))
        except Exception:
            continue
    if len(pts) < 2:
        return None
    pts.sort()
    hh, mm, ss = _hms(now)
    now_min = hh * 60 + mm + ss / 60.0
    if now_min <= pts[0][0]:
        return pts[0][1]
    if now_min >= pts[-1][0]:
        return pts[-1][1]
    for (m1, v1), (m2, v2) in zip(pts, pts[1:]):
        if m1 <= now_min <= m2:
            return v2 if m2 == m1 else v1 + (v2 - v1) * (now_min - m1) / (m2 - m1)
    return pts[-1][1]
