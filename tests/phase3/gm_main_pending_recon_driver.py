# -*- coding: utf-8 -*-
"""隔离回放 _poll_pending_recon：全账户回退下的 symbol/order_id 双重归属。

只 import 真 gm_main 并替换全部 SDK/回调为假件；不 init、不连接终端、不下单。
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime

ROOT = os.environ.get("SMOKE_ROOT") or r"E:\superTrader"
TMP = tempfile.mkdtemp(prefix="gm_pending_recon_p1_")
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
    raise AssertionError("本回放不得触发真实 SDK 调用")


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

ROWS = [
    # A：其他票的 status=3 绝不能喂给 600001。
    {"symbol": "SHSE.600009", "id": "A", "status": 3, "volume": 100, "side": 1},
    {"symbol": "SHSE.600001", "id": "A", "status": 0, "volume": 100, "side": 1},
    # B：同票但其他 order_id 的 status=3 也不能认领；只接受 B。
    {"symbol": "SHSE.600002", "id": "OTHER", "status": 3, "volume": 100, "side": 1},
    {"symbol": "SHSE.600002", "id": "B", "status": 3, "volume": 200, "side": 1,
     "filled_vwap": 10.5},
    # C：当前委托无 ID，同票有两条可能成交记录，归属不唯一 ⇒ 不得猜测 closed。
    {"symbol": "SHSE.600003", "id": "C-OTHER", "status": 3, "volume": 100, "side": 1},
    {"symbol": "SHSE.600003", "id": "C-MAYBE", "status": 3, "volume": 100, "side": 1},
    # D：当前委托无 ID，但同票只有唯一成交候选 ⇒ 允许恢复。
    {"symbol": "SHSE.600004", "id": "D-UNIQUE", "status": 3, "volume": 100, "side": 1},
]
CALLS = []


def _get_orders(symbol=None):
    CALLS.append(symbol)
    if symbol is not None:
        raise TypeError("legacy get_orders takes no symbol")
    return list(ROWS)


GM.get_orders = _get_orders
GM._sdk_call = lambda desc, fn, *args, **kwargs: fn(*args, **kwargs)
SEEN = []
GM.on_order_status = lambda context, order: SEEN.append({
    "symbol": order.get("symbol"), "id": order.get("id") or order.get("order_id"),
    "volume": order.get("volume"),
})
GM.write_risk = lambda *args, **kwargs: None

ctx = types.SimpleNamespace(
    mode=GM.MODE_LIVE,
    _pending_recon={
        "SHSE.600001": {"code": "600001", "side": "BUY", "qty": 100, "px": 10.0,
                         "ts_dt": datetime(2026, 10, 30, 9, 0),
                         "order_ids": ["A"], "closed": False},
        "SHSE.600002": {"code": "600002", "side": "BUY", "qty": 200, "px": 10.0,
                         "ts_dt": datetime(2026, 10, 30, 9, 0),
                         "order_ids": ["B"], "closed": False},
        "SHSE.600003": {"code": "600003", "side": "BUY", "qty": 100, "px": 10.0,
                         "ts_dt": datetime(2026, 10, 30, 9, 0),
                         "order_ids": [], "closed": False},
        "SHSE.600004": {"code": "600004", "side": "BUY", "qty": 100, "px": 10.0,
                         "ts_dt": datetime(2026, 10, 30, 9, 0),
                         "order_ids": [], "closed": False},
    },
)
GM._poll_pending_recon(ctx, datetime(2026, 10, 30, 10, 10))
print("RESULT " + json.dumps({
    "seen": SEEN,
    "closed": {key: bool(rec.get("closed")) for key, rec in ctx._pending_recon.items()},
    "calls": CALLS,
}, ensure_ascii=False))
GM._audit_close()
