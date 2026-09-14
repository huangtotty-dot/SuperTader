# -*- coding: utf-8 -*-
"""全链路离线回放：600176 中国巨石 × 2026-09-14 × 底仓 800 股（owner 2026-09-14 要求）。

目的：今天 600176 **实际零动作**（三次 SELL_HIGH 全被 floor_protection 拦下）。
本回放用**真实生产代码路径**重跑当天，看修完地板后系统会做什么。

驱动的真实代码（非复刻）：
  · core/t_decision.TDecisionEngine.evaluate   —— 做T决策核（Renko 触发）
  · execution/auto/sell_channels._sell_channel_gate —— 卖出门链（含 floor_protection）
  · execution/auto/sell_channels._sell_arbiter      —— 地板/阈值/数量/下单
  · execution/auto/sell_channels._force_tail_buyback —— 尾盘强制回补（本次新增）

仅 stub 掉券商 I/O（order_volume）与 gm SDK 调用；其余全走生产逻辑。

用法：python t_io/validation/auto/replay_600176_20260914.py [--pos 800] [--base 1600]
"""
import argparse
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (_ROOT, os.path.join(_ROOT, "execution", "auto"), os.path.join(_ROOT, "execution", "auto", "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402

import gm_main  # noqa: E402
import sell_channels  # noqa: E402
from core.t_decision import TDecisionEngine  # noqa: E402

CODE, GM_SYM = "600176", "SHSE.600176"
DATE = "2026-09-14"


# ---------------- 上下文 stub ----------------
class _Engine:
    def __init__(self):
        self.awaiting_buyback = {}
        self.sell_cooldown = {}
        self.sell_count_per_stock = {}
        self.buy_count_per_stock = {}

    def _get_params(self, code):
        return {**(gm_main.PARAMS or {}), **(gm_main.STOCK_PARAMS.get(code, {}) or {})}

    def record_trade_action(self, *a, **k):
        return {}

    def arm_awaiting_buyback(self, code, price, qty, action="SELL_HIGH"):
        """镜像 t_engine_auto.arm_awaiting_buyback（实盘在 on_order_status 成交回调里调用；
        本回放无券商回调，故在下单成交处等价调用）。"""
        price, qty = float(price or 0), int(qty or 0)
        if price <= 0 or qty <= 0:
            return None
        rec = {"sell_price": price, "sell_time": None, "sell_qty": qty,
               "sell_action": action, "target_price": round(price * 0.998, 2),
               "expire_date": "2026-09-17"}
        self.awaiting_buyback[code] = rec
        return rec


class _Ctx:
    def __init__(self, pos, base):
        self.engine = _Engine()
        self.sizer = gm_main.PositionSizer()
        self.manual_position = {GM_SYM: {"qty": pos, "available": pos, "t_qty": pos,
                                         "cost": 42.96, "name": "中国巨石"}}
        self._base_ref_600176 = base
        self.daily_sell_count = {}
        self.daily_buy_count = {}
        self._inflight_sell = {}
        self._inflight_buy = {}
        self._pending_recon = {}
        self._pending_sell_action = {}
        self._hard_stop_today = {}
        self.total_trade_count = 0
        self.last_index_regime = "range"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", type=int, default=800)
    ap.add_argument("--base", type=int, default=1600)
    args = ap.parse_args()

    fp = os.path.join(_ROOT, "t_io", "minute_snapshots", "2026", "09", f"{CODE}_{DATE}.json")
    raw = json.load(open(fp, encoding="utf-8"))["bars"]
    df = pd.DataFrame([{"time": pd.to_datetime(b["time"]), "open": float(b["open"]),
                        "high": float(b["high"]), "low": float(b["low"]),
                        "close": float(b["close"]), "volume": float(b["volume"])}
                       for b in raw]).sort_values("time").reset_index(drop=True)

    orders, events = [], []
    _orig_sdk = sell_channels._sdk_call
    _orig_ov = sell_channels.order_volume
    sell_channels._sdk_call = lambda desc, fn, *a, **k: fn()

    def _fake_ov(**kw):
        orders.append({"side": "BUY" if kw.get("side") == gm_main.OrderSide_Buy else "SELL",
                       "qty": int(kw.get("volume") or 0)})
        return {"id": f"sim{len(orders)}"}

    sell_channels.order_volume = _fake_ov
    sell_channels.write_order = lambda *a, **k: None
    sell_channels.write_risk = lambda *a, **k: None
    sell_channels._audit_write = lambda e: events.append(e)
    sell_channels._sell_state_persist = lambda *a, **k: None

    ctx = _Ctx(args.pos, args.base)
    eng = TDecisionEngine()
    print(f"[回放] {CODE} {DATE} 底仓实际 {args.pos} 股 / 目标底仓 {args.base} 股")
    print(f"[回放] MIRROR(引擎读到的目标)={gm_main.MIRROR_HOLDINGS.get(CODE)}")
    print("-" * 100)
    print(f"{'时间':8}{'价':>8}  引擎决策 / 门链结果")
    print("-" * 100)

    try:
        for i in range(len(df)):
            sub = df.iloc[:i + 1]
            row = df.iloc[i]
            now = row["time"].to_pydatetime()
            t_val = now.hour * 100 + now.minute
            cp = float(row["close"])
            vwap = float((sub["close"] * sub["volume"]).sum() / max(sub["volume"].sum(), 1))
            today_ret = cp / float(df.iloc[0]["open"]) - 1
            is_tail = now.hour == 14 and now.minute >= 50

            # 0) 真实尾盘强制回补（生产 on_bar 里置于卖出门链之前）
            if is_tail:
                h0 = dict(ctx.manual_position[GM_SYM])
                if sell_channels._force_tail_buyback(ctx, CODE, GM_SYM, cp, now, h0):
                    print(f"{now:%H:%M}  {cp:>8.2f}  ⟲ 尾盘强制回补 {orders[-1]['qty']} 股"
                          f"（台账 {h0['qty']}→{ctx.manual_position[GM_SYM]['qty']}）")

            # 1) 真实决策核
            sig, _bs, _ss, reason, _meta = eng.evaluate(
                CODE, "中国巨石", sub, cp, t_val, vwap, today_ret, "range", DATE)

            if sig is not None:
                events.append({"event": "signal", "time": str(now), "action": sig.action,
                               "price": round(cp, 3), "reason": reason})
                print(f"{now:%H:%M}  {cp:>8.2f}  ▲ 信号 {sig.action}  ({reason})")
                # 2a) 买入侧：真实 sizer 定量 → stub 下单 → 台账加仓（否则持仓口径不自洽）
                if sig.action in ("BUY_LOW", "ADD_POS"):
                    h = dict(ctx.manual_position[GM_SYM])
                    thr = float(gm_main.PARAMS.get("notify_buy_threshold", 55))
                    q = (int(ctx.sizer.calc_buy_qty(CODE, h, float(sig.score), thr) or 0) // 100) * 100
                    if q >= 100:
                        orders.append({"side": "BUY", "qty": q})
                        ctx.manual_position[GM_SYM]["qty"] += q
                        ctx.manual_position[GM_SYM]["t_qty"] += q
                        print(f"{'':10}{'':8}   → 下单 BUY {q} 股（台账 {h['qty']}→{ctx.manual_position[GM_SYM]['qty']}）")
                    else:
                        print(f"{'':10}{'':8}   → ✗ 买入 sizer 定量 {q} < 100，不下单")
                # 2b) 真实卖出门链（含 floor_protection）
                if sig.action in ("SELL_HIGH", "TARGET_SELL", "TREND_EXIT"):
                    h = dict(ctx.manual_position[GM_SYM])
                    feats = {"price": cp, "profit_pct": (cp / 42.96 - 1),
                             "hold_qty": h["qty"], "is_deep_loss": False}
                    before = len(orders)
                    _s2, tail_done = sell_channels._sell_channel_gate(
                        ctx, CODE, GM_SYM, cp, now, sig, h["qty"], h, {}, feats, is_tail, False)
                    if _s2 is not None:
                        sell_channels._sell_arbiter(ctx, CODE, _s2, h["qty"], cp, now, h,
                                                    float(gm_main.PARAMS.get("notify_sell_threshold", 55)),
                                                    gm_main.STOCK_PARAMS.get(CODE, {}), GM_SYM)
                    if len(orders) > before:
                        o = orders[-1]
                        ctx.engine.arm_awaiting_buyback(CODE, cp, o["qty"], _s2.action)  # 成交即武装回补
                        print(f"{'':10}{'':8}   → 下单 {o['side']} {o['qty']} 股"
                              f"（已武装回补 {o['qty']}@{round(cp*0.998,2)}）")
                    else:
                        blk = [e for e in events if e.get("event") == "sell_skip"][-1:]
                        why = blk[-1].get("reason") if blk else ("tail_done" if tail_done else "未穿透门链")
                        print(f"{'':10}{'':8}   → ✗ 被拦（{why}）"
                              + (f" base_ref={blk[-1].get('base_ref')} min_hold={blk[-1].get('min_hold')}"
                                 f" pos={blk[-1].get('pos_qty')}" if blk and blk[-1].get("reason") == "floor" else ""))

        # 4) 收盘补一次回补（若当天最后一根仍在 14:50 前）
        last = df.iloc[-1]
        now = last["time"].to_pydatetime()
        h = dict(ctx.manual_position[GM_SYM])
        if sell_channels._force_tail_buyback(ctx, CODE, GM_SYM, float(last["close"]), now, h):
            print(f"{now:%H:%M}  {float(last['close']):>8.2f}  ⟲ 收盘强制回补 {orders[-1]['qty']} 股")
    finally:
        sell_channels._sdk_call, sell_channels.order_volume = _orig_sdk, _orig_ov

    print("-" * 100)
    print(f"[结果] 信号 {sum(1 for e in events if e.get('event')=='signal')} 个 | "
          f"订单 {len(orders)} 笔 | 最终持仓 {ctx.manual_position[GM_SYM]['qty']} 股")
    for o in orders:
        print(f"        {o['side']} {o['qty']}")
    blocked = [e for e in events if e.get("event") == "sell_skip"]
    if blocked:
        print(f"[被拦] {len(blocked)} 次:")
        for b in blocked:
            print(f"        {b.get('time')} {b.get('action')} reason={b.get('reason')} "
                  f"base_ref={b.get('base_ref')} min_hold={b.get('min_hold')} pos={b.get('pos_qty')}")


if __name__ == "__main__":
    main()
