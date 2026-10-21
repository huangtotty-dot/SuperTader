# -*- coding: utf-8 -*-
"""预挂目标限价 · S0 影子探测（只记录，**不下单**）。

背景与判据：`doc/research/2026-10-19_预挂目标限价_paper实验设计.md`。
S0 的目的：在改任何委托之前，用 10 个交易日验证数据链路，并测出
「D+1 开盘前挂 entry×(1+3%) 卖限价」在真实行情里的触及率/触及时点/参照价。

本模块只做三件事：
  1. `build_plan(book_open, today)` —— 从账本挑出**当日可卖**的 D+1 持仓，算目标限价；
  2. `enrich(rows, bars)` —— 用 09:30~10:00 的 60s 棒回填：触及时点、10:00 参照价、
     是否"穿过"目标（保守成交口径）；
  3. `write/read` —— 原子落盘 `preplace_probe_{date}.json`（GUI 可能同时在读）。

⚠️ 本模块**绝不**调用 gateway/下单；`hooks` 里的两个时点钩子也只读行情。
"""
from __future__ import annotations

import datetime as _dt
import json
import os

import pandas as pd

TICK = 0.01          # A 股最小价位
TARGET_DEFAULT = 0.03


def build_plan(book_open: dict, today: str, target_pct: float = TARGET_DEFAULT) -> list:
    """账本 → 当日可卖（D+1）持仓的预挂计划。

    资格与 `executor.build_morning_sell_plan` 完全一致：`entry_date != today`
    且 `days_held == 0`（第一个可卖早盘）。价格 = `round(entry×(1+target), 2)`。
    """
    rows = []
    for code, rec in sorted((book_open or {}).items()):
        rec = rec or {}
        entry_date = str(rec.get("entry_date") or "")
        try:
            entry_price = float(rec.get("entry_price") or 0)
        except Exception:
            continue
        if entry_date == str(today):
            continue
        try:
            if int(rec.get("days_held") or 0) != 0:
                continue
        except Exception:
            continue
        if entry_price <= 0:
            continue
        rows.append({"code": str(code), "entry_date": entry_date,
                     "entry_price": round(entry_price, 3),
                     "qty": int(rec.get("qty") or 0),
                     "limit_px": round(entry_price * (1.0 + float(target_pct)), 2)})
    return rows


def _norm_bars(df) -> pd.DataFrame:
    """GM history(df=True) → 统一 `_code`(6位) / `_eob`(datetime)，按时间排序。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["_code", "_eob", "high", "low", "close"])
    d = df.copy()
    d["_code"] = d["symbol"].astype(str).str.split(".").str[-1]
    _eob = pd.to_datetime(d.get("eob"), errors="coerce")
    try:
        # GM 实盘返回带时区的 eob（datetime64[us, Asia/Shanghai]），墙钟值本身
        # 就是北京时间 ⇒ 去掉时区变 naive，才能和 naive 的 cutoff 比较
        # （2026-10-21 生产事故：tz-aware vs naive 比较 TypeError，完成钩子整日断流）
        _eob = _eob.dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass                                            # 本来就是 naive：无需处理
    d["_eob"] = _eob
    for c in ("high", "low", "close"):
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    return d.dropna(subset=["_eob"]).sort_values("_eob")


def enrich(rows: list, bars, cutoff=None) -> list:
    """用 60s 棒回填探测字段。`cutoff` = 只统计 eob ≤ cutoff 的 bar（通常 10:00:00）。"""
    d = _norm_bars(bars)
    if cutoff is not None:
        _cut = pd.Timestamp(cutoff)
        if _cut.tzinfo is not None:                     # 防御：cutoff 带时区同样剥掉
            _cut = _cut.tz_localize(None)
        d = d[d["_eob"] <= _cut]
    out = []
    for r in rows or []:
        row = dict(r)
        lim = float(row.get("limit_px") or 0)
        sub = d[d["_code"] == str(row.get("code"))]
        row.update({"touch_time": None, "px_10am_ref": None, "bar_high_to_10": None,
                    "conservative_fill": False, "would_trigger_control": False,
                    "n_bars": int(len(sub))})
        if len(sub) and lim > 0:
            row["bar_high_to_10"] = float(sub["high"].max())
            hit = sub[sub["high"] >= lim]
            if len(hit):
                row["touch_time"] = hit.iloc[0]["_eob"].strftime("%Y-%m-%d %H:%M:%S")
            row["would_trigger_control"] = bool(row["bar_high_to_10"] >= lim)
            # 保守口径：卖限价要成交，必须有**高于目标价**的打印（high ≥ limit+1 tick）；
            # 只触及（high == limit）可能卡在队列里 ⇒ 不算成交。
            row["conservative_fill"] = bool((sub["high"] >= lim + TICK).any())
            _cl = sub["close"].dropna()
            if len(_cl):
                row["px_10am_ref"] = float(_cl.iloc[-1])
        out.append(row)
    return out


def fp_for(book_dir, date) -> str:
    return os.path.join(str(book_dir), "preplace_probe_%s.json" % str(date))


def write(book_dir, date, phase: str, rows: list, target_pct: float,
          extra: dict | None = None) -> str:
    """原子写（tmp+replace）：GUI/其它进程可能正在读。返回文件路径。"""
    payload = {"date": str(date),
               "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "phase": str(phase), "target_pct": float(target_pct),
               "rows": list(rows or [])}
    if extra:
        payload.update(extra)
    fp = fp_for(book_dir, date)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, fp)
    return fp


def read(book_dir, date) -> dict | None:
    fp = fp_for(book_dir, date)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None
