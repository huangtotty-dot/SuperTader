# -*- coding: utf-8 -*-
"""探针遗留仓位清退（standalone 手动后备，2026-09-30 起已集成进主策略）。

背景：09-29 探针误在生产仿真账户买入 300166×8400 / 603629×1000（约 19.7 万），
T+1 当日不可卖。方案 A 拍板后探针废弃，此脚本把这倆票市价卖出归还现金。

⚠️ 2026-09-30 起：主策略 gm_main.py 已内置 `_maybe_unwind_probe_leftover`
（每日 09:31 后首根 bar 自检自跑），**正常无需手动运行本脚本**；
本脚本仅作为自动链路失效时的手动后备（需掘金终端在线、09:31 后）：
  "C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" scripts/unwind_probe_positions.py

fail-closed：只卖 UNWIND 清单内的票、只卖 available；结果落
t_io/validation/slippage_probe/unwind_report_{date}.json
"""
import json
import os
import sys
import time
from datetime import datetime

ROOT = r"E:\superTrader"
sys.path.insert(0, os.path.join(ROOT, "execution", "auto", "_gm"))
sys.stdout.reconfigure(encoding="utf-8")

STRATEGY_ID = "6786d88d-bbac-11f1-88f2-98fa9b8df5e7"   # 探针策略（绑生产账户，用于清退）
UNWIND = {"SZSE.300166": 8400, "SHSE.603629": 1000}   # 遗留底仓
REPORT = os.path.join(ROOT, "t_io", "validation", "slippage_probe",
                      f"unwind_report_{datetime.now():%Y-%m-%d}.json")


def init(context):
    context._done = False


def on_bar(context, bars):
    if context._done:
        return
    now = context.now
    if (now.hour, now.minute) < (9, 31):
        return
    context._done = True
    from gm.api import order_volume, OrderSide_Sell, OrderType_Market, PositionEffect_Close
    rep = {"ts": str(now), "actions": []}
    pos = context.account().positions()
    held = {p["symbol"]: p for p in (pos or []) if isinstance(p, dict)}
    for sym, max_q in UNWIND.items():
        p = held.get(sym)
        if not p or int(p.get("volume", 0) or 0) <= 0:
            rep["actions"].append({"symbol": sym, "action": "skip_no_position"})
            continue
        q = min(int(p.get("available", 0) or 0), max_q)
        if q < 100:
            rep["actions"].append({"symbol": sym, "action": "skip_no_available",
                                   "volume": int(p.get("volume", 0)),
                                   "available": int(p.get("available", 0) or 0)})
            continue
        try:
            r = order_volume(symbol=sym, volume=q, side=OrderSide_Sell,
                             order_type=OrderType_Market, position_effect=PositionEffect_Close)
            rep["actions"].append({"symbol": sym, "action": "sell", "qty": q, "ret": str(r)[:200]})
            print(f"[unwind] SELL {sym} x{q} 已委托")
        except Exception as e:
            rep["actions"].append({"symbol": sym, "action": "sell_fail", "err": str(e)})
            print(f"[unwind] SELL {sym} 失败: {e}")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    json.dump(rep, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[unwind] 报告: {REPORT}")
    time.sleep(5)
    os._exit(0)


if __name__ == "__main__":
    from gm.api import run, MODE_LIVE
    from utils.gm_token import load_token
    print(f"[unwind] 清退 {list(UNWIND)}（仅卖 available，T+1 约束自查）")
    run(strategy_id=STRATEGY_ID, filename=os.path.basename(__file__),
        mode=MODE_LIVE, token=load_token())
