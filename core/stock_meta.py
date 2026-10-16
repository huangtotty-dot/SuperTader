# -*- coding: utf-8 -*-
"""core/stock_meta.py — 股票「名称 / 所属行业」查询的**单一真源**（2026-10-16）。

owner 2026-10-16：「发送的飞书消息，要有股票代码同时要有股票的中文名称以及所属行业」。

## 真源

`t_io/rotation/industry_map.csv`（列：`代码` / `名称` / `行业名称`，约 5.6k 行，
由 `scripts/p2_aux/fetch_industry.py` 产出）。**与 `prescreen.full_market_meta()` 和
GUI 的行业列读的是同一个文件** —— 不另建一份、不手工维护代码表。

⚠️ 该表是**静态快照**：未收录 / 新上市的码取不到 ⇒ `stock_label` **如实退回纯代码，
不编造**（本仓忌讳"把无数据画成有数据"）。

## 为什么是 stdlib

只读 csv + 一个 dict 缓存，**不 import pandas**。这样 `gm_bridge/watcher.py` 那种
独立长驻进程也能用（见下）。导入成本和内存都可忽略。

## ⚠️ 与 `gm_bridge/watcher.py` 里的同名实现是**刻意重复**，别顺手去重

watcher 由 `ops_guard.ensure_watcher()` 以 `python <abs>/watcher.py` 拉起，
`sys.path[0]` 只有它自己的 `gm_bridge/` 目录（该文件 docstring 明确要求它是纯通知进程、
依赖仅 stdlib），**拿不到仓根** ⇒ 它自带一份同样逻辑的实现。两边的**行为契约**是：
`名称(代码｜行业)` / 取不到行业只给 `名称(代码)` / 都没有给纯代码。
改动其一请同步另一个（`tests/phase3/test_bridge_push_label.py` 钉 watcher 侧）。
"""
from __future__ import annotations

import csv
import os

_TTL_SEC = 300          # 表几乎不变；读失败时**保留旧缓存**（别把已知名字清空成裸码）
_META: dict = {}        # 6 位码 -> (名称, 行业)
_META_AT = 0.0


def _default_path() -> str:
    """行业表默认路径（仓根自解析，勿硬编码本机路径）。"""
    root = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "t_io", "rotation", "industry_map.csv")


def load_meta(path: str | None = None, ttl: float = _TTL_SEC) -> dict:
    """读行业表 → `{6位码: (名称, 行业)}`。带 TTL 缓存。

    读失败 / 文件缺失 ⇒ **保留旧缓存**并返回（不清空）；首次就失败则返回 `{}`，
    调用方退化为"只给代码"（降级但不崩，也不编造）。
    """
    global _META, _META_AT
    import time
    _now = time.time()
    if _META and (_now - _META_AT) < ttl:
        return _META
    out: dict = {}
    try:
        with open(path or _default_path(), encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = str(row.get("代码") or "").strip().zfill(6)
                if not (code.isdigit() and len(code) == 6):
                    continue
                out[code] = (str(row.get("名称") or "").strip(),
                             str(row.get("行业名称") or "").strip())
    except Exception:
        pass
    if out:
        _META = out
        _META_AT = _now
    return _META


def name_of(code: str, fallback: str = "") -> str:
    """股票中文名。表里没有 ⇒ 返回 `fallback`（调用方常已持有名称，别覆盖成空）。"""
    _c = str(code or "").strip()
    nm = (load_meta().get(_c) or ("", ""))[0]
    return nm or (fallback or "")


def industry_of(code: str) -> str:
    """所属行业（申万口径）。取不到返回空串（**不编造**）。"""
    return (load_meta().get(str(code or "").strip()) or ("", ""))[1]


def stock_label(code: str, name: str = "") -> str:
    """飞书里统一的股票标识 → `名称(代码｜行业)`。

    降级链（**不编造**）：
      · 名称 + 行业 → `名称(代码｜行业)`
      · 只有名称   → `名称(代码)`
      · 连名称都没有 → 纯 6 位码
    `name` 传入时优先用它（调用方手里的名字比表新，例如刚改名的票）。
    """
    _c = str(code or "").strip()
    if not _c:
        return ""
    _nm = str(name or "").strip() or name_of(_c)
    _ind = industry_of(_c)
    if _nm and _ind:
        return f"{_nm}({_c}｜{_ind})"
    if _nm:
        return f"{_nm}({_c})"
    return _c
