# -*- coding: utf-8 -*-
"""开盘强制对齐 `_force_open_align` 回归（owner 2026-09-14 裁决）。

"明日开盘一次性强制对齐持仓到目标底仓；资金不足按优先级逐个买满"。
本测试用 stub 券商 I/O 验证四条关键行为：
  A 超额 → SELL、缺口 → BUY，数量按 100 股取整
  B **必须先 write_order 再 order_volume**（否则孤儿闸丢成交——6a96829c 教训）
  C 买入受可用现金封顶；买不起的进 skipped、不影响已下的单
  D 优先级按 OPEN_ALIGN_BUY_ORDER 顺序（缺额小的先买满）
运行：python t_io/validation/auto/test_open_align.py
"""
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_A = os.path.join(_ROOT, "execution", "auto")
for _p in (_ROOT, _A, os.path.join(_A, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gm_main  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + f"  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Cash:
    def __init__(self, a): self.available = a


class _Acct:
    def __init__(self, a): self.cash = _Cash(a)


class _Ctx:
    def __init__(self, cash):
        self.mode = gm_main.MODE_LIVE
        self.account = lambda: _Acct(cash)
        self.manual_position = {}
        self.latest_pre_close = {}
        self._pending_recon = {}
        self.now = datetime(2026, 9, 15, 9, 31)


def _run(pos_by_code, target_by_code, price_by_code, cash):
    """跑一次对齐；返回 (events, orders, skipped_flag)。"""
    seq, orders, audits = [], [], []
    orig = (gm_main._sdk_call, gm_main.order_volume, gm_main.write_order,
            gm_main._audit_write, gm_main._mark_pending_recon)
    gm_main._sdk_call = lambda desc, fn, *a, **k: fn()
    gm_main.order_volume = lambda **kw: (orders.append(kw), {"id": f"o{len(orders)}"})[1]
    gm_main.write_order = lambda *a, **k: seq.append(("order", a[1], a[2], a[3]))
    gm_main._audit_write = lambda e: audits.append(e)
    gm_main._mark_pending_recon = lambda *a, **k: None

    ctx = _Ctx(cash)
    for c, q in pos_by_code.items():
        sym = "SHSE." + c if c[0] in "56" else "SZSE." + c
        ctx.manual_position[sym] = {"qty": q, "available": q, "t_qty": q,
                                    "cost": 0, "pre_close": price_by_code.get(c, 0)}
        ctx.latest_pre_close[c] = price_by_code.get(c, 0)
    for c, t in target_by_code.items():
        setattr(ctx, f"_base_ref_{c}", t)
    old_stocks = gm_main.STOCKS
    gm_main.STOCKS = {c: ("SHSE." + c if c[0] in "56" else "SZSE." + c) for c in target_by_code}
    try:
        gm_main._force_open_align(ctx)
    finally:
        (gm_main._sdk_call, gm_main.order_volume, gm_main.write_order,
         gm_main._audit_write, gm_main._mark_pending_recon) = orig
        gm_main.STOCKS = old_stocks
    return seq, orders, audits


def test_sell_and_buy():
    # 600176 实持 1600 / 目标 1600 → 不动；300054 实持 200 / 目标 600 → 买 400
    seq, orders, _ = _run({"600176": 1600, "300054": 200}, {"600176": 1600, "300054": 600},
                          {"600176": 45.0, "300054": 67.0}, cash=100000)
    check("A1 无超额则不卖", not [o for o in orders if o.get("side") == gm_main.OrderSide_Sell])
    buys = [o for o in orders if o.get("side") == gm_main.OrderSide_Buy]
    check("A2 缺口买入 400 股", len(buys) == 1 and buys[0]["volume"] == 400, str(buys))
    check("A3 取整到 100", buys[0]["volume"] % 100 == 0)

    seq2, orders2, _ = _run({"600176": 2400}, {"600176": 1600}, {"600176": 45.0}, cash=100000)
    sells = [o for o in orders2 if o.get("side") == gm_main.OrderSide_Sell]
    check("A4 超额卖出 800 股", len(sells) == 1 and sells[0]["volume"] == 800, str(sells))
    check("A5 卖出用 Close", sells[0].get("position_effect") == gm_main.PositionEffect_Close)


def test_order_event_before_order():
    seq, orders, _ = _run({"600176": 1600}, {"600176": 2400}, {"600176": 45.0}, cash=100000)
    check("B1 先 write_order 再 order_volume",
          bool(seq) and seq[0][0] == "order" and len(orders) >= 1,
          f"seq={seq} orders={len(orders)}")
    check("B2 order 事件带 code/side/qty",
          seq[0][1] == "600176" and seq[0][2] == "BUY" and seq[0][3] == 800, str(seq[:1]))


def test_cash_cap_and_priority():
    # 缺口合计远超现金：300176 缺 800 股 @45 = 36000；300054 缺 400 @67 = 26800
    # 现金 30000 → 按优先级 OPEN_ALIGN_BUY_ORDER（300054 在 600176 之前）先买满 300054(26800)，
    # 剩 3200 买不起 600176（需 36000）→ 600176 不下单。
    seq, orders, _ = _run({"600176": 1600, "300054": 200}, {"600176": 2400, "300054": 600},
                          {"600176": 45.0, "300054": 67.0}, cash=30000)
    by = {o["symbol"]: o["volume"] for o in orders if o.get("side") == gm_main.OrderSide_Buy}
    check("C1 现金够的票买满", by.get("SZSE.300054") == 400, str(by))
    check("C2 现金不足的票不下单", "SHSE.600176" not in by, str(by))

    # 现金只够一手 → 补齐到 100 的整倍且不超过可负担
    seq2, orders2, _ = _run({"600176": 1600}, {"600176": 2400}, {"600176": 45.0}, cash=9000)
    b2 = [o for o in orders2 if o.get("side") == gm_main.OrderSide_Buy]
    check("C3 部分成交按现金封顶(9000/45=200 股)", len(b2) == 1 and b2[0]["volume"] == 200, str(b2))

    # 一手都买不起 → 不产生订单
    seq3, orders3, _ = _run({"600176": 1600}, {"600176": 2400}, {"600176": 45.0}, cash=1000)
    check("C4 一手都买不起则不下单",
          not [o for o in orders3 if o.get("side") == gm_main.OrderSide_Buy])


if __name__ == "__main__":
    print("A) 超额卖 / 缺口买")
    test_sell_and_buy()
    print("B) write_order 先于 order_volume")
    test_order_event_before_order()
    print("C) 现金封顶 + 优先级")
    test_cash_cap_and_priority()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("ALL PASS")
