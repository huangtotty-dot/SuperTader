# -*- coding: utf-8 -*-
"""W37 修复仿真验证（逻辑级，跑真实代码路径；2026-09-11）

A) P0-1 `_buyback_cap_qty`：回补量封顶 armed sell_qty + `buyback_capped` 留痕；非回补路径不变。
B) Fix C `_poll_pending_recon`：MODE_LIVE 门控下 age≥90s 轮询补记（合成 order 喂 on_order_status +
   `fill_recovered_by_poll` + closed）；<90s 不触发；非 LIVE 不登记/不轮询。

真实终端实单验证（仿真盘，周一开盘前）另行执行：
  ① 触发一次回补（卖后价回落至目标）→ 下单量应 == armed sell_qty + 审计出 buyback_capped；
  ② 一笔成交后若回调缺失 → ≤90s 应出现 fill_recovered_by_poll 补记。
运行：python t_io/validation/auto/test_w37_fixes_sim.py
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_A = os.path.join(_ROOT, "execution", "auto")
for _p in (_ROOT, _A, os.path.join(_A, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gm_main  # noqa: E402


def test_buyback_cap():
    audits = []
    assert gm_main._buyback_cap_qty(21000, {"sell_qty": 1100}) == 1100, "588170 帽失败"
    assert gm_main._buyback_cap_qty(2600, {"sell_qty": 900}) == 900, "600481 帽失败"
    assert gm_main._buyback_cap_qty(900, {"sell_qty": 1100}) == 900, "小量不得放大"
    assert gm_main._buyback_cap_qty(50, {"sell_qty": 1100}) == 50, "不足一手不动"
    assert gm_main._buyback_cap_qty(21000, None) == 21000, "非回补必须不变"
    gm_main._buyback_cap_qty(21000, {"sell_qty": 1100}, audits.append, "588170")
    assert audits and audits[-1]["event"] == "buyback_capped" and audits[-1]["qty_after"] == 1100
    print("A) P0-1 回补帽 OK")


def test_poll_live_gate():
    class Ctx:
        mode = gm_main.MODE_LIVE
    called = {}
    ctx = Ctx()
    gm_main._mark_pending_recon(ctx, "000988", "SZSE.000988", "SELL", 100, 10.0, [{"id": "OID1"}])
    assert ctx._pending_recon.get("SZSE.000988"), "LIVE 下应登记"
    _orig_go, _orig_oos, _orig_wr = gm_main.get_orders, gm_main.on_order_status, gm_main.write_risk
    try:
        gm_main.get_orders = lambda **kw: [{"id": "OID1", "symbol": "SZSE.000988", "status": 3,
                                            "volume": 100, "filled_vwap": 10.0, "side": 2}]
        gm_main.on_order_status = lambda c, o: called.setdefault("o", o)
        gm_main.write_risk = lambda *a, **k: called.setdefault("risk", a[1] if len(a) > 1 else "")
        ctx._pending_recon["SZSE.000988"]["ts_dt"] = datetime.now()
        gm_main._poll_pending_recon(ctx, datetime.now())
        assert "o" not in called, "age<90s 不应轮询"
        ctx._pending_recon["SZSE.000988"]["ts_dt"] = datetime.now() - timedelta(seconds=120)
        gm_main._poll_pending_recon(ctx, datetime.now())
        assert called.get("o", {}).get("id") == "OID1", "未合成 order"
        assert called.get("risk") == "fill_recovered_by_poll", "缺补记留痕"
        assert ctx._pending_recon["SZSE.000988"]["closed"] is True
    finally:
        gm_main.get_orders, gm_main.on_order_status, gm_main.write_risk = _orig_go, _orig_oos, _orig_wr
    # 非 LIVE 不登记
    ctx2 = Ctx()
    ctx2.mode = 0
    gm_main._mark_pending_recon(ctx2, "x", "SZSE.000988", "SELL", 100, 1.0, [])
    assert not getattr(ctx2, "_pending_recon", {}), "非 LIVE 不应登记"
    print("B) Fix C 轮询 + LIVE 门控 OK")


if __name__ == "__main__":
    test_buyback_cap()
    test_poll_live_gate()
    print("W37 逻辑级仿真验证 全部通过")
