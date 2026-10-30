# -*- coding: utf-8 -*-
"""隔离驱动：真 import gm_main，回放 2026-10-30 四种委托回报。

只使用临时事件桥/账本；不 init、不连接终端、不下单。由
`test_osc_order_lifecycle_p0.py` 以子进程调用，避免 gm.api 桩污染其它测试。
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime

ROOT = os.environ.get("SMOKE_ROOT") or r"E:\superTrader"
TMP = tempfile.mkdtemp(prefix="gm_order_status_p0_")
BOOK = os.path.join(TMP, "osc_book")
os.makedirs(BOOK, exist_ok=True)
os.environ["GM_BRIDGE_DIR"] = TMP
os.environ["SUPERTRADER_ROOT"] = ROOT

# gm_main 唯一允许 import 的外部 SDK，在这里全部换成无交易假件。
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
    raise AssertionError("本回放不得触发任何真实 SDK 调用")


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
    "gm_main", os.path.join(ROOT, "execution", "auto", "gm_main.py"))
GM = importlib.util.module_from_spec(spec)
sys.modules["gm_main"] = GM
spec.loader.exec_module(GM)

import atexit  # noqa: E402
try:
    atexit.unregister(GM._gm_atexit_banner)
except Exception:
    pass
GM._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace.jsonl")
GM._AUDIT_MIRROR_PATH = os.path.join(TMP, "auto_backtrace.jsonl")
GM._OSC_BOOK_DIR = BOOK

from oversold_gap_confirm import backfill  # noqa: E402

DATE = "2026-10-30"
CODES = ("600001", "600002", "600003", "600004")
IDS = {"600001": "fill-only", "600002": "pc-1",
       "600003": "rej-only", "600004": "exp-1"}

# 四笔都先处于「挂单即入账」状态；filled 的旧留痕刻意空 ID，验证真实回报认领。
buys = []
for code in CODES:
    buys.append({
        "code": code, "symbol": "SHSE." + code, "side": "BUY",
        "gap": 0.05, "price": 10.0, "qty_target": 100, "qty_placed": 100,
        "truncated": False, "defer_reason": None, "status": "submitted",
        "filled_qty": 0, "filled_price": None, "fee_est": 1.0,
        "message": "", "order_id": "" if code == "600001" else IDS[code],
        "qty_remaining": 100,
    })
fills = {"date": DATE, "mode": "live", "picks": [], "buys": buys, "summary": {}}
with open(os.path.join(BOOK, "fills_%s.json" % DATE), "w", encoding="utf-8") as f:
    json.dump(fills, f, ensure_ascii=False)
book = {"updated_at": "", "open": {
    code: {"entry_date": DATE, "entry_price": 10.0, "qty": 100,
           "dev20": None, "gap": 0.05, "board": "半导体",
           "days_held": 0, "last_check_date": None}
    for code in CODES}, "closed": {}}
with open(os.path.join(BOOK, "positions.json"), "w", encoding="utf-8") as f:
    json.dump(book, f, ensure_ascii=False)
day_state = {"date": DATE, "buy_done": True,
             "pending": backfill.pending_from_fills(fills), "spares": [],
             "done_codes": list(CODES), "backfill": {}}
with open(os.path.join(BOOK, "day_state_%s.json" % DATE), "w", encoding="utf-8") as f:
    json.dump(day_state, f, ensure_ascii=False)

# 孤儿闸仍走真实实现：先写本策略 order 意向，不能用 patch 绕过归属检查。
for code in CODES:
    GM.write_order(str(datetime.now()), code, "BUY", 100, 10.0, order_type="osc")

ctx = types.SimpleNamespace(
    mode=GM.MODE_LIVE, now=datetime(2026, 10, 30, 10, 0, 0),
    executed_orders={}, manual_position={}, _fills_done=set(),
    rejected_order_count=0, _pending_recon={},
)
orders = [
    {"symbol": "SHSE.600001", "status": 3, "volume": 100, "side": 1,
     "order_id": "fill-only", "filled_vwap": 10.5, "price": 10.0,
     "filled_commission": 0.16},
    {"symbol": "SHSE.600002", "status": 6, "volume": 100, "side": 1,
     "id": "pc-1", "price": 10.0},
    {"symbol": "SHSE.600003", "status": 8, "volume": 100, "side": 1,
     "order_id": "rej-only", "price": 10.0, "ord_rej_reason_detail": ""},
    {"symbol": "SHSE.600004", "status": 12, "volume": 100, "side": 1,
     "id": "exp-1", "price": 10.0, "ord_rej_reason_detail": ""},
]
for order in orders:
    GM.on_order_status(ctx, order)


def _read(name):
    with open(os.path.join(BOOK, name), encoding="utf-8") as f:
        return json.load(f)


events = []
for name in sorted(os.listdir(TMP)):
    if not (name.startswith("events_") and name.endswith(".jsonl")):
        continue
    with open(os.path.join(TMP, name), encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") in ("fill", "reject", "order_terminal", "risk"):
                events.append(rec)

out = {
    "fills": _read("fills_%s.json" % DATE),
    "day_state": _read("day_state_%s.json" % DATE),
    "book": _read("positions.json"),
    "events": events,
    "rejected_order_count": ctx.rejected_order_count,
    "service_ack_wired": callable(getattr(GM.SERVICES, "write_order_ack", None)),
}
print("RESULT " + json.dumps(out, ensure_ascii=False))
GM._audit_close()
