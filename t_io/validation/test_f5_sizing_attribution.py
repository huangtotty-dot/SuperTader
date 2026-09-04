# -*- coding: utf-8 -*-
"""test_f5_sizing_attribution.py — F-5 sizing 归因透出单测（2026-09-04）

calc_buy_qty 保持返回 int（契约遍布 main/replay/harness/auto/测试），拦截原因走旁路
`self._buy_state = {reason, max_buyable(钳制后真实值), qty}`。模块级便捷函数复制到
_LAST_BUY_STATE 供 main 读 last_buy_state()。

覆盖（F-5 验收三例）：
1. no_t_budget          — t_qty=0 恒 0
2. full_position        — net=t_qty（无未接回）raw max_buyable=0 早退
3. index_target_cap_clamp — raw>0 但 index_factor 目标仓钳制后 max_buyable=0（09-04 002451 案型）
附：成功路径 cap 真实值守卫——factor 钳制把"naive 360"收到 100，_buy_state.max_buyable=100
    （替代 trace 用 raw t_qty−net 的误导口径）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core.position_sizer import PositionSizer  # noqa: E402


def mk(t_qty, typ="stock"):
    return {"t_qty": t_qty, "qty": t_qty, "base": t_qty, "type": typ, "cost": 10.0}


def run():
    n = 0
    # 与 W33 B2 回归同：calc_buy_qty 内 timing_gate 打桩放行（与归因数学正交）
    import core.timing_gate as _timing_gate_mod
    _timing_gate_mod.timing_verdict = lambda code, _ctx=None: {"go": True}

    def check(name, got, want):
        nonlocal n
        assert got == want, f"{name}: got {got}, want {want}"
        n += 1
        print(f"  ok {name}: {got}")

    def check_state(name, st, reason_want, cap_want=None, qty_want=0):
        nonlocal n
        assert st.get("reason") == reason_want, f"{name}: reason {st.get('reason')!r}, want {reason_want!r}"
        if cap_want is not None:
            assert st.get("max_buyable") == cap_want, f"{name}: max_buyable {st.get('max_buyable')}, want {cap_want}"
        assert st.get("qty") == qty_want, f"{name}: qty {st.get('qty')}, want {qty_want}"
        n += 1
        print(f"  ok {name}: reason={reason_want} max_buyable={st.get('max_buyable')} qty={st.get('qty')}")

    # 1) no_t_budget
    s = PositionSizer(params={})
    q = s.calc_buy_qty("603667", mk(0), None, 100, 36)
    check("no_t_budget qty=0", q, 0)
    check_state("no_t_budget 归因", s._buy_state, "no_t_budget")

    # 2) full_position（net=t_qty，无未接回）
    s = PositionSizer(params={})
    q = s.calc_buy_qty("600481", mk(300), None, 100, 36)
    check("full_position qty=0", q, 0)
    check_state("full_position 归因", s._buy_state, "full_position", cap_want=0)

    # 3) index_target_cap_clamp（raw=300 通过满仓检查，factor 目标仓钳后归 0）
    #    t=1000 已卖 300 → net=700, raw=1000-700=300；factor=0.3 → target_cap=300 → 300-700<0 → 0
    vt = {"000988": {"SELL_HIGH": [{"qty": 300}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt)
    q = s.calc_buy_qty("000988", mk(1000), None, 100, 36,
                       index_ctx={"index_pos_factor": 0.3, "index_circuit_state": "normal"})
    check("index_target_cap_clamp qty=0", q, 0)
    check_state("index_target_cap_clamp 归因", s._buy_state, "index_target_cap_clamp", cap_want=0)

    # 4) 成功路径真实 cap（naive=600 会误写，factor 钳后上限=100）：
    #    t=1000 已卖 600 → net=400, raw=600；factor=0.5 → target_cap=500 → 500-400=100 → cap=100
    vt2 = {"600176": {"SELL_HIGH": [{"qty": 600}], "BUY_LOW": []}}
    s = PositionSizer(params={}, virtual_trades=vt2)
    q = s.calc_buy_qty("600176", mk(1000), None, 100, 36,
                       index_ctx={"index_pos_factor": 0.5, "index_circuit_state": "normal"})
    check("成功路径 qty=100（min(360,cap100)）", q, 100)
    check_state("成功路径 cap=真实钳后100", s._buy_state, None, cap_want=100, qty_want=100)

    print(f"PASS: F-5 sizing 归因全绿（{n} 项）")


if __name__ == "__main__":
    run()
