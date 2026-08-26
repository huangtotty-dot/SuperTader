# -*- coding: utf-8 -*-
"""test_v122_full_position_buy.py — V1.2.2 满仓买入建议开关化回归（2026-08-11 用户拍板："满仓保底买先不用保留"）

背景：V1.2.1 取消 max_buyable<=0 早退后，08-11 实盘 600481 满仓状态仍推 3 笔买入建议（与卖单互抵微盈 +0.25）。
V1.2.2 将满仓保底买开关化：allow_full_position_buy=False（默认）→ 满仓 return 0；True → 走 V1.2.1 保底 100。

覆盖：
1. 满仓 + 开关关（默认）→ 返回 0（恢复 V1.2.0 早退语义，不产生买入建议）
2. 满仓 + 开关开 → 保底 100（V1.2.1 行为保留）
3. 非满仓接回买 → 正常数量（开关不影响正常 sizing）
4. 大盘目标仓位钳到 0（index_pos_factor 压缩）视同满仓：开关关 → 0；开关开 → 保底 100
5. V1.2.1 保留项不受影响：非满仓不足一手保底 100 / 卖侧弱档保底 100 / 纯底仓 t_qty=0 恒 0 / 熔断买 0
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.position_sizer import PositionSizer  # noqa: E402


def mk(t_qty, typ="stock"):
    return {"t_qty": t_qty, "qty": t_qty, "base": t_qty, "type": typ, "cost": 10.0}


def run():
    n = 0

    def check(name, got, want):
        nonlocal n
        assert got == want, f"{name}: got {got}, want {want}"
        n += 1
        print(f"  ✓ {name}: {got}")

    # 1) 满仓 + 开关关（默认）→ 0（08-11 600481 满仓买×3 场景封堵）
    s = PositionSizer(params={})
    check("满仓+默认关: 600481 t300 买=0", s.calc_buy_qty("600481", mk(300), None, 40, 36), 0)
    s = PositionSizer(params={"allow_full_position_buy": False})
    check("满仓+显式关: 600481 t300 买=0", s.calc_buy_qty("600481", mk(300), None, 40, 36), 0)

    # 2) 满仓 + 开关开 → 保底 100（V1.2.1 行为保留，回测对照可用）
    s = PositionSizer(params={"allow_full_position_buy": True})
    check("满仓+开关开: 600481 t300 买=保底100", s.calc_buy_qty("600481", mk(300), None, 40, 36), 100)

    # 3) 非满仓接回买 → 正常数量（t1000 卖 400 未接回，strength=6 → rebuild_base 0.50 → int(400×0.5)=200）
    vt = {"000988": {"SELL_HIGH": [{"qty": 400}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt)
    check("非满仓接回买(默认关)=200（正常sizing）", s.calc_buy_qty("000988", mk(1000), None, 42, 36), 200)
    s = PositionSizer(params={"allow_full_position_buy": True}, virtual_trades=vt)
    check("非满仓接回买(开关开)=200（不受影响）", s.calc_buy_qty("000988", mk(1000), None, 42, 36), 200)

    # 4) 大盘目标仓位钳到 0 视同满仓（t1000 无虚拟交易满仓，index_pos_factor=0.3 → target_cap=300 < net=1000）
    ctx = {"index_pos_factor": 0.3}
    s = PositionSizer(params={})
    check("目标仓位钳0+默认关: 买=0", s.calc_buy_qty("000988", mk(1000), None, 40, 36, index_ctx=ctx), 0)
    s = PositionSizer(params={"allow_full_position_buy": True})
    check("目标仓位钳0+开关开: 买=保底100", s.calc_buy_qty("000988", mk(1000), None, 40, 36, index_ctx=ctx), 100)

    # 5) V1.2.1 保留项不受影响
    # 非满仓接回弱买不足一手：t300 卖 100 未接回，strength=2 → rebuild_weak 0.30 → int(100×0.3)=30 → 保底 100
    vt2 = {"600176": {"SELL_HIGH": [{"qty": 100}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt2)
    check("非满仓不足一手保底100（V1.2.1 保留）", s.calc_buy_qty("600176", mk(300), None, 38, 36), 100)
    s = PositionSizer(params={})
    check("卖侧弱档保底100（V1.2.1 保留）", s.calc_sell_qty("600176", mk(300), None, 40, 36), 100)
    s = PositionSizer(params={})
    check("纯底仓 t0 买=0（保留）", s.calc_buy_qty("603667", mk(0), None, 46, 36), 0)
    s = PositionSizer(params={})
    check("纯底仓 t0 卖=0（保留）", s.calc_sell_qty("002639", mk(0), None, 60, 55), 0)
    s = PositionSizer(params={"allow_full_position_buy": True})
    check("熔断 clear 买=0（风控保留，优先于保底）",
          s.calc_buy_qty("600481", mk(300), None, 40, 36, index_ctx={"index_circuit_state": "clear"}), 0)

    print(f"PASS: V1.2.2 满仓买入开关化回归全绿（{n} 项）")


if __name__ == "__main__":
    run()
