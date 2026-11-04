# -*- coding: utf-8 -*-
"""飞书播报的「只发日线多头」闸（2026-11-04 owner）。

owner：「飞书播报的信息目前仅限于多头的股票」。

口径 = 日线三态里的 **多头**（`core.position_structure.stage_daily`：
MA60 的 20 交易日变化率 > +3%）。数据取**本地日线缓存**，零网络。

设计要点：
  · 热点路径友好：直接算「最后 81 个收盘」的两个 MA60（不建 DataFrame、不 import pandas），
    结果按 `(code, 缓存文件 mtime)` 进程内缓存 ⇒ 突破预警每拍查也不重复算；
  · 口径与 `stage_daily` 逐点一致由 `tests/phase3/test_stage_gate.py` 交叉验证守卫；
  · **fail-open**：缓存缺失/日线不足/算不出来 ⇒ **放行**。宁可多一条，也不要因为本地数据
    缺失把真正的告警吞掉（调用方会记「过滤 N 只」的日志，方便事后核对）；
  · 只做「播报筛选」，**不碰**交易执行/成交回执/系统异常告警 —— 那些不是选股播报。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("stage_gate")

BASE = Path(__file__).resolve().parents[1]
DAILY_DIR = BASE / "t_io" / "cache" / "daily_kline"
# 与 core/position_structure.MA60_FLAT_PCT 同值（单测钉住，改一处必须同步另一处）
MA60_FLAT_PCT = 0.03
# (code, mtime_ns) -> 阶段标签（None = 判不了）
_CACHE: dict = {}


def _norm_code(code) -> str:
    s = str(code or "").strip().upper()
    for suf in (".SH", ".SZ", ".BJ", "_SH", "_SZ", "_BJ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.split("_")[0] if "_" in s else s


def _stage_of_closes(closes: list) -> str | None:
    """与 `position_structure.stage_daily` 同口径的快捷算法（只要最后 81 个收盘）。"""
    if len(closes) < 81:
        return None
    try:
        m60 = sum(closes[-60:]) / 60.0
        m60_20ago = sum(closes[-80:-20]) / 60.0
    except (TypeError, ValueError):
        return None
    if not (m60_20ago > 0):                      # NaN/0/负 ⇒ 判不了
        return None
    s20 = m60 / m60_20ago - 1.0
    if s20 > MA60_FLAT_PCT:
        return "多头"
    if s20 < -MA60_FLAT_PCT:
        return "空头"
    return "震荡"


def stage_of(code) -> str | None:
    """本地日线缓存 → 三态标签（"多头"/"震荡"/"空头"）；判不了 ⇒ None。"""
    c = _norm_code(code)
    if not c:
        return None
    fp = DAILY_DIR / f"{c}.json"
    try:
        mt = fp.stat().st_mtime_ns
    except OSError:
        return None
    key = (c, mt)
    if key in _CACHE:
        return _CACHE[key]
    lab = None
    try:
        rows = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
        closes = [float(r["close"]) for r in rows]
        lab = _stage_of_closes(closes)
    except Exception as e:                       # noqa: BLE001
        log.debug("stage_of(%s) 失败: %s: %s", c, type(e).__name__, str(e)[:120])
    if len(_CACHE) > 20000:                      # 防长跑无限增长（全池 ~5000 只）
        _CACHE.clear()
    _CACHE[key] = lab
    return lab


def is_bull(code) -> bool:
    """是否日线多头。**判不了 ⇒ True（fail-open）**，理由见模块头。"""
    lab = stage_of(code)
    return True if lab is None else lab == "多头"


def filter_bull(items, key: str = "code"):
    """按多头过滤 [(dict 或裸 code)]；返回 (保留, 被滤掉)。"""
    kept, dropped = [], []
    for it in items or []:
        code = it.get(key) if isinstance(it, dict) else it
        (kept if is_bull(code) else dropped).append(it)
    return kept, dropped


def clear_cache() -> None:
    """测试缝：清进程内缓存。"""
    _CACHE.clear()
