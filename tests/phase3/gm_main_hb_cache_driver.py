# -*- coding: utf-8 -*-
"""隔离驱动：真 import gm_main，验证 P0-1「fill 到达 ⇒ _HB_CACHE 立即失效」。

只 import + 调 on_order_status；不 init、不连终端、不下单。
由 `test_hb_cache_fill_invalidation.py` 以子进程调用，避免 gm.api 桩污染其它测试。
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime

ROOT = os.environ.get("SMOKE_ROOT") or r"E:\superTrader"
TMP = tempfile.mkdtemp(prefix="gm_hb_cache_p0_")
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

# 孤儿闸走真实实现：先写本策略 order 意向，让 fill 归属检查放行。
GM.write_order(str(datetime.now()), "600001", "BUY", 100, 10.0, order_type="osc")

ctx = types.SimpleNamespace(
    mode=GM.MODE_LIVE, now=datetime(2026, 11, 20, 10, 0, 0),
    executed_orders={}, manual_position={}, _fills_done=set(),
    rejected_order_count=0, _pending_recon={},
)

fresh = datetime.now()
FILL = {"symbol": "SHSE.600001", "status": 3, "volume": 100, "side": 1,
        "order_id": "hb-fill-1", "filled_vwap": 10.5, "price": 10.0}

# ① 缓存新鲜（刚做过 300s 快照）⇒ fill 到达后必须立即失效
GM._HB_CACHE["ts"] = fresh
GM._HB_CACHE["positions"] = {"600001": {"qty": 100, "cost": 10.0}}
GM.on_order_status(ctx, FILL)
fill_invalidated = GM._HB_CACHE["ts"] is None

# ② 重复成交回调（fill 防重闸命中、早退）同样保持失效（幂等，不会"复活"缓存）
GM._HB_CACHE["ts"] = fresh
GM.on_order_status(ctx, dict(FILL))
dup_invalidated = GM._HB_CACHE["ts"] is None

# ③ status=6（待撤，非成交）不得失效缓存
GM._HB_CACHE["ts"] = fresh
GM.on_order_status(ctx, {"symbol": "SHSE.600001", "status": 6, "volume": 100,
                         "side": 1, "id": "pc-1", "price": 10.0})
pending_kept = GM._HB_CACHE["ts"] == fresh

print("RESULT " + json.dumps({
    "fill_invalidated": fill_invalidated,
    "dup_invalidated": dup_invalidated,
    "pending_kept": pending_kept,
}, ensure_ascii=False))
GM._audit_close()
