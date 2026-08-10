# -*- coding: utf-8 -*-
"""test_v121_unfreeze.py — V1.2.1 取消 sizing 冻结回归（2026-08-11 用户拍板：手动跟单，做T不考虑底仓）

覆盖：
1. 600176（t_qty=300）弱卖档 → 100（原 0，int(300×0.2)=60<100 冻结）
2. 600481（t_qty=300，满仓）买信号 → 100（原 0，max_buyable=0 早退冻结）
3. 000988（t_qty=100）卖出 → 100（原 0）
4. 588170 ETF 买卖正常值不受影响（卖 700 / 接回买 200）
5. 603667/002639（t_qty=0 纯底仓）买卖仍 0（08-05 战略拍板保留）
6. 底仓地板开关：sell_floor_enabled=False 可卖全量；=True 恢复 V1.30 钳制
7. 熔断场景（index_state=clear）买入仍 0（大盘风控保留）；clear 卖出=全量（既有行为）
8. 正常值回归：1000 股弱卖 200 不变（保底只影响 <100 场景）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from position_sizer import PositionSizer  # noqa: E402


def mk(t_qty, typ="stock"):
    return {"t_qty": t_qty, "qty": t_qty, "base": t_qty, "type": typ, "cost": 10.0}


def run():
    n = 0

    def check(name, got, want):
        nonlocal n
        assert got == want, f"{name}: got {got}, want {want}"
        n += 1
        print(f"  ✓ {name}: {got}")

    # 1) 600176 弱卖档保底（原 0）
    s = PositionSizer(params={})
    check("600176 t300 弱卖→保底100", s.calc_sell_qty("600176", mk(300), None, 40, 36), 100)

    # 2) 600481 满仓买放开（原 0）
    s = PositionSizer(params={})
    check("600481 t300 满仓买→保底100", s.calc_buy_qty("600481", mk(300), None, 40, 36), 100)

    # 3) 000988 t100 卖出保底（原 0）
    s = PositionSizer(params={})
    check("000988 t100 弱卖→100", s.calc_sell_qty("000988", mk(100), None, 40, 36), 100)

    # 4) 588170 ETF 正常值不受影响
    s = PositionSizer(params={})
    check("588170 t3000 强卖=700（不变）", s.calc_sell_qty("588170", mk(3000, "etf"), None, 46, 36), 700)
    vt = {"588170": {"SELL_HIGH": [{"qty": 1000}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt)
    check("588170 接回强买=200（不变）", s.calc_buy_qty("588170", mk(3000, "etf"), None, 46, 36), 200)

    # 5) 纯底仓 t_qty=0 恒 0（保留）
    s = PositionSizer(params={})
    check("603667 t0 买=0（纯底仓保留）", s.calc_buy_qty("603667", mk(0), None, 46, 36), 0)
    check("002639 t0 卖=0（纯底仓保留）", s.calc_sell_qty("002639", mk(0), None, 60, 55), 0)

    # 6) 底仓地板开关：默认关→清仓档可卖全量 1000；开→钳制 500
    s = PositionSizer(params={})
    check("地板关: t1000 三次强卖=1000", s.calc_sell_qty("600176", mk(1000), None, 46, 36, used_sells=2), 1000)
    s = PositionSizer(params={"sell_floor_enabled": True})
    check("地板开: t1000 三次强卖=500（V1.30 钳制）", s.calc_sell_qty("600176", mk(1000), None, 46, 36, used_sells=2), 500)

    # 7) 熔断风控保留
    s = PositionSizer(params={})
    check("clear 熔断买=0（风控保留）",
          s.calc_buy_qty("600481", mk(300), None, 40, 36, index_ctx={"index_circuit_state": "clear"}), 0)
    s = PositionSizer(params={})
    check("clear 熔断卖=全量300（既有行为）",
          s.calc_sell_qty("600481", mk(300), None, 60, 55, index_ctx={"index_circuit_state": "clear"}), 300)

    # 8) 正常值回归：保底只影响 <100 场景
    s = PositionSizer(params={})
    check("t1000 弱卖=200（不变）", s.calc_sell_qty("000988", mk(1000), None, 40, 36), 200)

    print(f"PASS: V1.2.1 取消冻结回归全绿（{n}/n 项）")


if __name__ == "__main__":
    run()
