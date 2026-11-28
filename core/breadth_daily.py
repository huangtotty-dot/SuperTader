# -*- coding: utf-8 -*-
"""每日「上涨 / 下跌 / 平盘」家数（2026-11-25 owner：市场情绪面板加一张涨跌家数图，5 日均线平滑，
并类比画出强势区/平稳区/弱势区）。

口径
  · 逐只读本地日线缓存（`t_io/cache/daily_kline/*.json`，前复权收盘），
    `close > prev_close` 记上涨、`<` 记下跌、`=` 记平盘；
  · 同一天先按**股票数**聚合，再按覆盖家数 `n` 做样本闸（<2000 只的日期丢弃，
    早期缓存不全/退市造成的空壳日不算）；
  · 默认排除今天的 forming bar；只有显式 EOD 调用才可包含 as_of，且始终截断未来；

SWR / 成本
  · 全量扫 5500+ 只（每只 800 行）单线程约 5~10s ⇒ 放**后台单飞**线程（3 线程 + 每只让
    GIL 5ms），结果落 `t_io/cache/breadth_daily.json`（schema + built_at）；
  · `get_cached()` 只做磁盘读 + 必要时踢后台重建，**绝不在调用线程上扫盘**；
  · 缓存按自然日判新鲜（一天重建一次），旧档照常下发（前端先画旧线，重建完成后下一拍替换）。
"""
from __future__ import annotations

import json
import os
import threading as _threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FP = os.path.join(_ROOT, "t_io", "cache", "breadth_daily.json")
_DAILY_DIR = os.path.join(_ROOT, "t_io", "cache", "daily_kline")
_SCHEMA = 1
_MIN_COVER = 2000        # 覆盖家数下限（与 sentiment_local 的样本闸一致）
_WORKERS = 3             # 4 物理核：给 UI 留一核

_state = {"data": None, "ts": 0.0, "running": False}
_lock = _threading.Lock()


def is_stock(code: str) -> bool:
    """与 `scripts/ops/build_sentiment_local.py::is_stock` 同口径（A 股个股，排除基金/可转债）。"""
    c = str(code)
    if len(c) != 6 or not c.isdigit() or c[0] in "15":
        return False
    if c[:2] in ("51", "56", "58", "50", "52", "11", "12", "13"):
        return False
    return c[:2] in ("60", "00", "30", "68") or c[0] in "48" or c[:3] == "920"


def count_rows(rows, skip_date: str | None = None, end_date: str | None = None):
    """单只股票的日线 → {date: (up, dn, flat)}（首行没有前收盘，不计）。

    ``end_date`` 是历史 as-of 硬边界；``skip_date`` 用于排除未收盘的当日行。
    两者都只做本地过滤，不触网。
    """
    out = {}
    prev = None
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        try:
            c = float(r.get("close") or 0)
        except (TypeError, ValueError):
            prev = None
            continue
        if (prev and prev > 0 and c > 0 and len(d) == 10 and d != skip_date
                and (end_date is None or d <= end_date)):
            if c > prev:
                out[d] = (1, 0, 0)
            elif c < prev:
                out[d] = (0, 1, 0)
            else:
                out[d] = (0, 0, 1)
        prev = c
    return out


def scan_file(fp: str, skip_date: str, end_date: str | None = None):
    """单只 → {date: (up, dn, flat)}；文件坏/无行 → {}。"""
    try:
        with open(fp, "r", encoding="utf-8") as f:
            d = json.load(f)
        rows = d.get("rows") if isinstance(d, dict) else d
    except Exception:
        return {}
    return count_rows(rows, skip_date, end_date)


def build(daily_dir: str = _DAILY_DIR, workers: int = _WORKERS,
          as_of: str | None = None, include_as_of: bool = False):
    """全池扫描 → {date: [up, dn, flat, n]}（已过样本闸）。

    默认 ``as_of=今天`` 且排除当日，保持盘前/盘中 forming-bar 防护。
    只有调用方已完成今日行修复时，才可显式 ``include_as_of=True`` 构建 EOD；
    历史构建无论是否包含 as_of，都会丢弃其后的所有行。
    """
    as_of = str(as_of or datetime.now().strftime("%Y-%m-%d"))[:10]
    skip_date = None if include_as_of else as_of
    try:
        files = [os.path.join(daily_dir, n) for n in os.listdir(daily_dir)
                 if n.endswith(".json") and is_stock(n[:-5])]
    except OSError:
        return {}
    agg: dict = {}

    def _one(fp):
        m = scan_file(fp, skip_date, as_of)
        _time.sleep(0.005)            # 每只让一次 GIL（与 pb-struct 同一纪律）
        return m

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers or 1), 4))) as ex:
        for m in ex.map(_one, files):
            for d, (u, dn, fl) in m.items():
                v = agg.get(d)
                if v is None:
                    agg[d] = v = [0, 0, 0, 0]
                v[0] += u
                v[1] += dn
                v[2] += fl
                v[3] += 1
    return {d: v for d, v in agg.items() if v[3] >= _MIN_COVER}


# ---------------- 缓存 / SWR ----------------
def _read_disk() -> dict:
    try:
        with open(_CACHE_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("series"), dict):
            return d
    except Exception:
        pass
    return {}


def _write_disk(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FP), exist_ok=True)
        tmp = _CACHE_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_FP)
    except Exception:
        pass


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _kick_build(expected_date: str | None = None, include_as_of: bool = False,
                eod_verified: bool = False) -> None:
    """后台单飞重建。

    历史日可由普通缓存刷新包含；**当日**只有 ``refresh_eod()`` 在今日行修复成功后
    才能传 ``eod_verified=True``。普通 SWR 即使发现当日缺口，也不能借时钟绕过 forming 闸。
    """
    as_of = str(expected_date or datetime.now().strftime("%Y-%m-%d"))[:10]
    today_iso = datetime.now().strftime("%Y-%m-%d")
    include = bool(include_as_of and (as_of < today_iso or
                                      (eod_verified and as_of == today_iso)))

    def _bg():
        try:
            series = build(as_of=as_of, include_as_of=include)
            if series:
                d = {"schema": _SCHEMA, "date": _today(),
                     "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "as_of": as_of, "includes_as_of": include, "series": series}
                _write_disk(d)
                with _lock:
                    _state.update({"data": d, "ts": _time.time()})
        except Exception:
            pass
        finally:
            with _lock:
                _state["running"] = False
    _threading.Thread(target=_bg, daemon=True, name="breadth-daily").start()


def refresh_eod(as_of: str) -> bool:
    """今日行修复成功后的唯一当日 EOD 构建入口；返回是否已启动后台单飞。"""
    day = str(as_of or "")[:10]
    if len(day) != 10:
        return False
    with _lock:
        if _state.get("running"):
            return False
        _state["running"] = True
    _kick_build(day, include_as_of=True, eod_verified=True)
    return True


def _in_session() -> bool:
    """交易时段（09:15~15:05 工作日）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return "09:15" <= now.strftime("%H:%M") <= "15:05"


def _series_covers(d: dict, expected_date: str | None) -> bool:
    """目标交易日必须存在、覆盖达样本闸，且序列末端不能落后于它。"""
    if not expected_date:
        return True
    s = (d or {}).get("series") or {}
    row = s.get(expected_date)
    try:
        covered = bool(row) and int(row[3]) >= _MIN_COVER
    except (IndexError, TypeError, ValueError):
        covered = False
    series_end = max(s) if s else ""
    return covered and series_end >= expected_date


def get_cached(ttl_sec: float = 86400.0, expected_date: str | None = None) -> dict:
    """js_api 用：只读缓存 + 必要时踢后台重建。

    新鲜度同时要求缓存是自然日新档、``expected_date`` 存在且覆盖达标、序列末端不
    落后。``expected_date`` 应来自权威情绪行的最后一个**已完成**交易日；默认构建仍
    排除今天，盘中旧档照返且不为日频指标抢 UI 的 GIL。
    """
    expected_date = str(expected_date)[:10] if expected_date else None
    now = _time.time()
    with _lock:
        d = _state.get("data")
        if d is None or (now - float(_state.get("ts") or 0.0)) >= ttl_sec:
            d = _read_disk() or None
            if d is not None:
                _state.update({"data": d, "ts": now})
    if d is not None and int(d.get("schema") or 0) != _SCHEMA:
        d = None
    stale = ((d is None) or str(d.get("date") or "") != _today()
             or not _series_covers(d, expected_date))
    if stale:
        with _lock:
            if not _state.get("running") and (not _in_session() or d is None):
                _state["running"] = True
                if expected_date:
                    _kick_build(expected_date, include_as_of=True)
                else:
                    _kick_build()
    if d is None:
        return {"series": {}, "built_at": "", "date": None, "pending": True,
                "stale": True, "data_status": "insufficient_data",
                "expected_date": expected_date, "series_end": None, "n_days": 0}
    s = d.get("series") or {}
    return {"series": s, "built_at": d.get("built_at") or "",
            "date": d.get("date"), "pending": False, "stale": stale,
            "data_status": "stale" if stale else "ready",
            "expected_date": expected_date, "series_end": max(s) if s else None,
            "n_days": len(s)}


def merge_into(rows, ttl_sec: float = 86400.0) -> dict:
    """合并涨跌家数；以最后一个早于今天的有效情绪行作为完成日锚点。"""
    today_iso = datetime.now().strftime("%Y-%m-%d")
    completed = sorted({str(r.get("date") or "")[:10] for r in rows or []
                        if len(str(r.get("date") or "")) >= 10
                        and str(r.get("date") or "")[:10] < today_iso})
    expected_date = completed[-1] if completed else None
    data = get_cached(ttl_sec, expected_date=expected_date)
    s = data.get("series") or {}
    for r in rows or []:
        v = s.get(str(r.get("date") or ""))
        if v:
            r["up"], r["dn"], r["flat"], r["n"] = int(v[0]), int(v[1]), int(v[2]), int(v[3])
        else:
            r.setdefault("up", None)
            r.setdefault("dn", None)
            r.setdefault("flat", None)
            r.setdefault("n", None)
    return data
