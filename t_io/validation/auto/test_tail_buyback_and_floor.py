# -*- coding: utf-8 -*-
"""2026-09-14 owner 裁决两项的回归测试：

A) 地板参照 `min(目标底仓, 实际持仓)` —— 解开"实际低于目标一半 → 非保护类卖出永久冻结"。
   实证动机：600176 目标1600/实持800 → 旧式 min_hold=800 > pos-100=700，当日三次 SELL_HIGH 全灭。
   关键不变量：**实际持仓 == 目标底仓时，行为与旧式逐字一致**。

B) `_force_tail_buyback` 尾盘强制回补 —— "数量不变"硬约束（认亏也买回）。
   动机：09-14 收盘挂 5 笔未回补（002451 900+700 / 600176 500 / 600481 8600 / 300054 700）。

运行：python t_io/validation/auto/test_tail_buyback_and_floor.py
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
import sell_channels  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + f"  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ---------------- A) 地板 ----------------
def _min_hold(base_ref, pos_qty, new, ratio=0.5):
    """复刻 sell_channels._sell_channel_gate 的 min_hold（base_ref<200 豁免）。
    新式 = min(目标×ratio, 实持−1手)：**不能**用 min(目标,实持)×ratio —— 那会退化成
    `pos−100 < pos×0.5` ⇔ `pos<200`，地板完全失效（本轮已证伪）。"""
    if new:
        mh = min(int(base_ref * ratio), max(0, int(pos_qty) - 100))
    else:
        mh = int(base_ref * ratio)
    return 0 if base_ref < 200 else mh


def _blocks(base_ref, pos_qty, new):
    return (pos_qty - 100) < _min_hold(base_ref, pos_qty, new)


def test_floor():
    # 死锁解除
    check("600176 目标1600/实持800 旧式被拦", _blocks(1600, 800, False) is True)
    check("600176 目标1600/实持800 新式放行", _blocks(1600, 800, True) is False)
    check("000988 目标500/实持300 新式放行", _blocks(500, 300, True) is False)
    # 正常情形逐字不变（实持 >= 目标）
    for base, pos in ((1600, 1600), (1800, 1800), (70000, 70000), (500, 500), (1600, 5000)):
        check(f"实持>=目标 {base}/{pos} min_hold 不变",
              _min_hold(base, pos, True) == _min_hold(base, pos, False))
        check(f"实持>=目标 {base}/{pos} 判定不变",
              _blocks(base, pos, True) == _blocks(base, pos, False))
    # 小底仓豁免不受影响
    check("小底仓豁免 100/8700", _blocks(100, 8700, True) is False)
    check("小底仓豁免 100/800", _blocks(100, 800, True) is False)
    # 保护强度保留：新式只是"封顶到能卖一手"，并非取消地板
    check("600176 新 min_hold 封顶到 700（只放开一手）", _min_hold(1600, 800, True) == 700)
    check("600176 旧 min_hold 仍为 800 语义被保留", _min_hold(1600, 1600, True) == 800)
    check("极端 目标1600/实持150 新式封顶到 50", _min_hold(1600, 150, True) == 50)


# ---------------- B) 尾盘强制回补 ----------------
class _Eng:
    def __init__(self):
        self.awaiting_buyback = {}


class _Ctx:
    def __init__(self):
        self.engine = _Eng()
        self.manual_position = {}
        self._inflight_buy = {}
        self.total_trade_count = 0


def test_tail_buyback():
    orders, risks, audits = [], [], []
    orig = (sell_channels.order_volume, sell_channels._sdk_call,
            sell_channels.write_order, sell_channels.write_risk,
            sell_channels._audit_write, sell_channels._sell_state_persist)
    sell_channels.order_volume = lambda **kw: (orders.append(kw), {"id": "x"})[1]
    sell_channels._sdk_call = lambda desc, fn, *a, **k: fn()
    sell_channels.write_order = lambda *a, **k: None
    sell_channels.write_risk = lambda *a, **k: risks.append((a, k))
    sell_channels._audit_write = lambda e: audits.append(e)
    sell_channels._sell_state_persist = lambda *a, **k: None
    sell_channels.OrderSide_Buy = "BUY"
    sell_channels.OrderType_Market = "MKT"
    sell_channels.PositionEffect_Open = "OPEN"
    try:
        ctx = _Ctx()
        ctx.engine.awaiting_buyback["600176"] = {
            "sell_price": 43.808, "sell_qty": 500, "sell_action": "SELL_HIGH",
            "target_price": 43.72, "sell_time": datetime(2026, 9, 11, 13, 47)}
        ctx.manual_position["SHSE.600176"] = {"qty": 800, "available": 800, "t_qty": 800}
        now = datetime(2026, 9, 14, 14, 51)

        fired = sell_channels._force_tail_buyback(ctx, "600176", "SHSE.600176", 45.70, now, {})
        check("B1 有未回补 → 触发", fired is True)
        check("B2 下了 500 股买单", orders and orders[0].get("volume") == 500 and orders[0].get("side") == "BUY",
              str(orders[:1]))
        check("B3 用 Open 而非 Close", orders and orders[0].get("position_effect") == "OPEN")
        check("B4 awaiting_buyback 已清", "600176" not in ctx.engine.awaiting_buyback)
        check("B5 台账已加回 500", ctx.manual_position["SHSE.600176"]["qty"] == 1300)
        check("B6 留下 tail_buyback_forced 审计",
              any(a.get("event") == "tail_buyback_forced" for a in audits))
        check("B7 记录溢价（认亏也买）", audits and audits[-1].get("premium_pct") is not None)

        # 幂等：状态已清 → 同 bar 再调不再下单
        n = len(orders)
        again = sell_channels._force_tail_buyback(ctx, "600176", "SHSE.600176", 45.80, now, {})
        check("B8 幂等（已回补不再重复下单）", again is False and len(orders) == n)

        # 不足 100 股 → 不动
        ctx.engine.awaiting_buyback["600481"] = {"sell_price": 4.2, "sell_qty": 50}
        check("B9 <100 股不动",
              sell_channels._force_tail_buyback(ctx, "600481", "SHSE.600481", 4.2, now, {}) is False)

        # 无未回补 → 不动
        check("B10 无未回补不动",
              sell_channels._force_tail_buyback(ctx, "588170", "SHSE.588170", 0.9, now, {}) is False)
    finally:
        (sell_channels.order_volume, sell_channels._sdk_call,
         sell_channels.write_order, sell_channels.write_risk,
         sell_channels._audit_write, sell_channels._sell_state_persist) = orig


if __name__ == "__main__":
    print("A) 地板参照 min(目标, 实持)")
    test_floor()
    print("B) 尾盘强制回补")
    test_tail_buyback()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("ALL PASS")
