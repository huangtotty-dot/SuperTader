# -*- coding: utf-8 -*-
"""OSC BUY 委托意图的当日原子账本。

本模块只做 JSON 持久化，不接触柜台。执行器必须在 SDK 前先写入 ``reserved``；
崩溃恢复按 intent_id/order_id 对账，绝不按股票代码猜测新委托归属。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

VERSION = 1


def path_for(book_dir: str, date: str) -> str:
    return os.path.join(str(book_dir), "order_intents_%s.json" % str(date))


def new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load(book_dir: str, date: str, *, strict: bool = False) -> list:
    """读取当日意图；缺文件为空。损坏文件在 strict 模式必须让调用方闭锁。"""
    fp = path_for(book_dir, date)
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or not isinstance(payload.get("intents"), list):
            raise ValueError("order_intents 根结构非法")
        rows = payload["intents"]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("order_intents 含非对象记录")
        return rows
    except Exception:
        if strict:
            raise
        return []


def write_all(book_dir: str, date: str, rows: list) -> str:
    """tmp + flush/fsync + replace 原子覆盖整本当日意图账。"""
    fp = path_for(book_dir, date)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    payload = {"version": VERSION, "date": str(date), "updated_at": _now(),
               "intents": list(rows or [])}
    tmp = "%s.%s.tmp" % (fp, uuid.uuid4().hex)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2,
                               allow_nan=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, fp)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return fp


def from_fill_record(rec: dict, date: str, *, creating: bool = False) -> dict:
    """从委托留痕提取意图字段；未知成交价/额保持 None。"""
    now = _now()
    row = {
        "intent_id": str(rec.get("intent_id") or new_id()),
        "date": str(date), "code": str(rec.get("code") or ""),
        "side": str(rec.get("side") or "BUY").upper(),
        "qty": int(rec.get("qty_placed") or 0),
        "reference_price": rec.get("price"),
        "execution_limit_price": rec.get("execution_limit_price"),
        "reserved_notional": rec.get("reserved_notional"),
        "status": str(rec.get("status") or "reserved"),
        "order_id": (str(rec.get("order_id")) if rec.get("order_id") else None),
        "filled_qty": int(rec.get("filled_qty") or 0),
        "filled_price": rec.get("filled_price"),
        "filled_notional": rec.get("filled_notional"),
        "qty_remaining": int(rec.get("qty_remaining") or 0),
        "raw_status": rec.get("raw_status"),
        "updated_at": now,
    }
    for key in ("gap", "message", "budget_bound_breach", "fee_actual", "fee"):
        if rec.get(key) is not None:
            row[key] = rec.get(key)
    if creating:
        row["created_at"] = now
        row["reserved_at"] = now
    else:
        row["created_at"] = rec.get("intent_created_at") or now
    return row


def upsert_fill_record(book_dir: str, date: str, rec: dict,
                       *, creating: bool = False) -> dict:
    """按 intent_id 原子 upsert；返回持久化后的意图，写失败向调用方抛错。"""
    rows = load(book_dir, date, strict=True)
    incoming = from_fill_record(rec, date, creating=creating)
    wanted = incoming["intent_id"]
    hit = None
    for idx, old in enumerate(rows):
        if str(old.get("intent_id") or "") == wanted:
            hit = idx
            break
    if hit is None:
        if not creating:
            # 异步事件只能更新既有意图，不能在回报后凭空补造“SDK 前已预留”的证明。
            raise KeyError("order_intent_not_found:%s" % wanted)
        rows.append(incoming)
    else:
        old = rows[hit]
        preserved = {k: old.get(k) for k in ("created_at", "reserved_at", "ack_at")
                     if old.get(k) is not None}
        rows[hit] = {**old, **incoming, **preserved}
    write_all(book_dir, date, rows)
    return incoming


def acknowledge(book_dir: str, date: str, rec: dict) -> dict:
    """SDK 已返回后写回 order_id/状态；失败同样必须让后续 BUY 闭锁。"""
    row = upsert_fill_record(book_dir, date, rec)
    rows = load(book_dir, date, strict=True)
    for old in rows:
        if str(old.get("intent_id") or "") == str(rec.get("intent_id") or ""):
            old["ack_at"] = _now()
            break
    write_all(book_dir, date, rows)
    return row
