# -*- coding: utf-8 -*-
"""W45 委托价/滑点隔离回放：只读生产事件，所有输出写临时目录。"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime

ROOT = os.environ.get("SMOKE_ROOT") or r"E:\superTrader"
TMP = tempfile.mkdtemp(prefix="gm_w45_audit_")
BOOK = os.path.join(TMP, "osc_book")
os.makedirs(BOOK, exist_ok=True)
os.environ["GM_BRIDGE_DIR"] = TMP
os.environ["SUPERTRADER_ROOT"] = ROOT

gm_api = types.ModuleType("gm.api")
for key, value in {
    "MODE_LIVE": 1, "MODE_BACKTEST": 2,
    "ADJUST_PREV": 1, "ADJUST_NONE": 0,
    "OrderSide_Buy": 1, "OrderSide_Sell": 2,
    "OrderType_Market": 1, "OrderType_Limit": 2,
    "PositionEffect_Open": 1, "PositionEffect_Close": 2,
    "PositionSide_Long": 1,
}.items():
    setattr(gm_api, key, value)


def _no_sdk(*args, **kwargs):
    raise AssertionError("W45 回放不得触发真实 SDK")


for name in ("order_volume", "get_instruments", "run"):
    setattr(gm_api, name, _no_sdk)
gm_api.subscribe = lambda **kwargs: None
gm_api.history_n = lambda **kwargs: []
gm_api.history = lambda **kwargs: []
gm_api.current = lambda *args, **kwargs: []
gm_api.get_orders = lambda *args, **kwargs: []
sys.modules["gm.api"] = gm_api
sys.path.insert(0, os.path.join(ROOT, "execution", "auto"))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "gm_main_w45", os.path.join(ROOT, "execution", "auto", "gm_main.py"))
GM = importlib.util.module_from_spec(spec)
sys.modules["gm_main_w45"] = GM
spec.loader.exec_module(GM)
import atexit  # noqa: E402
try:
    atexit.unregister(GM._gm_atexit_banner)
except Exception:
    pass
GM._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace.jsonl")
GM._AUDIT_MIRROR_PATH = os.path.join(TMP, "auto_backtrace.jsonl")
GM._OSC_BOOK_DIR = BOOK

source = []
with open(os.path.join(ROOT, "t_io", "bridge", "events_20261106.jsonl"),
          encoding="utf-8") as f:
    for line in f:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("code") == "688185" and rec.get("event") in ("order", "fill"):
            source.append(rec)
order_intent = next(r for r in source if r["event"] == "order")
source_fill = next(r for r in source if r["event"] == "fill")

# 不动生产账本；空 open + 哨兵 closed 可验证审计修复不会重写真实闭环。
book = {"updated_at": "", "open": {}, "closed": {"sentinel": {"qty": 7, "price": 1.23}}}
with open(os.path.join(BOOK, "positions.json"), "w", encoding="utf-8") as f:
    json.dump(book, f, ensure_ascii=False)

ctx = types.SimpleNamespace(
    mode=GM.MODE_LIVE, now=datetime(2026, 11, 6, 14, 50, 9),
    executed_orders={"SHSE.688185": {"qty": 1100, "available": 1100, "cost": 80.0}},
    manual_position={}, _fills_done=set(), rejected_order_count=0,
    _pending_recon={"SHSE.688185": {
        "code": "688185", "side": "SELL", "qty": 1100,
        "px": float(order_intent["price"]), "order_price_kind": "market_protection",
        "order_ids": [source_fill["order_id"]], "closed": False}},
)
GM.write_order(str(order_intent["time"]), "688185", "SELL", 1100,
               float(order_intent["price"]), order_type="osc")
GM.on_order_status(ctx, {
    "symbol": "SHSE.688185", "status": 3, "volume": source_fill["qty"], "side": 2,
    "order_id": source_fill["order_id"], "filled_vwap": source_fill["fill_vwap"],
    "price": source_fill["order_price"], "filled_commission": source_fill["fee"],
})

# 第二笔没有 pending_recon：即使柜台价和 VWAP 都在，滑点也必须为 null。
GM.write_order("2026-11-06 14:51:00", "600001", "SELL", 100, 10.0, order_type="osc")
GM.on_order_status(ctx, {
    "symbol": "SHSE.600001", "status": 3, "volume": 100, "side": 2,
    "order_id": "no-reference", "filled_vwap": 10.1, "price": 9.0,
    "filled_commission": 0.2,
})

events = []
for name in os.listdir(TMP):
    if not (name.startswith("events_") and name.endswith(".jsonl")):
        continue
    with open(os.path.join(TMP, name), encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") == "fill":
                events.append(rec)
with open(os.path.join(BOOK, "positions.json"), encoding="utf-8") as f:
    out_book = json.load(f)
print("RESULT " + json.dumps({"events": events, "source_fill": source_fill,
                              "book": out_book}, ensure_ascii=False))
GM._audit_close()
