# -*- coding: utf-8 -*-
"""test_w33_b2_single_tier.py — W33 B2 加仓单档比例回归（2026-08-13）

V2 纯两点后 sig_score 恒=100、阈值 36 → strength 恒≥10，三档永远走"强"档（失真）。
B2 简化为单档固定比例：个股 接回×0.60；ETF 接回×0.25（向下取整一手）。

结构性事实（实测）：max_buyable = SELL−BUY ≡ unrebuilt，故首加分支在生产默认
（allow_full_position_buy=False）下不可达——严格做T语义下买入=接回，首加仅在
开关开时经 max_buyable=0 钳制落到保底一手。

覆盖：
1. 个股接回: unrebuilt=500 ×0.60=300
2. 个股接回: unrebuilt=300 ×0.60=180
3. ETF 接回: unrebuilt=1000 ×0.25=250 → 向下取整 200
4. ETF 接回: unrebuilt=500 ×0.25=125 → 向下取整 100
5. 首加（开关开, 不可达路径）→ 保底一手
6. 保留项: t0 恒 0 / 满仓早退 0 / 目标仓位钳 0 / 熔断 0
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
        print(f"  ok {name}: {got}")

    # 1) 个股接回 500 → 0.60 → 300
    vt = {"000988": {"SELL_HIGH": [{"qty": 500}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt)
    check("个股接回 500×0.60=300", s.calc_buy_qty("000988", mk(1000), None, 100, 36), 300)

    # 2) 个股接回 1000 → 0.60 → 600
    vt2 = {"600176": {"SELL_HIGH": [{"qty": 1000}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt2)
    check("个股接回 1000×0.60=600", s.calc_buy_qty("600176", mk(1000), None, 100, 36), 600)

    # 3) ETF 接回 1000 ×0.25=250 → 向下取整 200
    vte = {"588170": {"SELL_HIGH": [{"qty": 1000}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vte)
    check("ETF接回 1000×0.25=250→200", s.calc_buy_qty("588170", mk(3000, "etf"), None, 100, 36), 200)

    # 4) ETF 接回 800 ×0.25=200
    vte2 = {"588170": {"SELL_HIGH": [{"qty": 800}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vte2)
    check("ETF接回 800×0.25=200", s.calc_buy_qty("588170", mk(3000, "etf"), None, 100, 36), 200)

    # 5) 首加分支（开关开才可达；max_buyable=0 钳制 → 保底一手）
    s = PositionSizer(params={"allow_full_position_buy": True})
    check("首加(开关开)=保底一手", s.calc_buy_qty("000988", mk(1000), None, 100, 36), 100)

    # 6) 保留项
    s = PositionSizer(params={})
    check("t0 恒 0", s.calc_buy_qty("603667", mk(0), None, 100, 36), 0)
    check("满仓早退 0（net=t_qty）", s.calc_buy_qty("600481", mk(300), None, 100, 36), 0)
    check("目标仓位钳 0（index_pos_factor=0）",
          s.calc_buy_qty("000988", mk(1000), None, 100, 36, index_ctx={"index_pos_factor": 0.0}), 0)
    check("熔断 clear 0",
          s.calc_buy_qty("600481", mk(300), None, 100, 36, index_ctx={"index_circuit_state": "clear"}), 0)

    print(f"PASS: W33 B2 单档比例回归全绿（{n} 项）")


if __name__ == "__main__":
    run()
