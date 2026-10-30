# -*- coding: utf-8 -*-
"""`GmServices` —— 策略侧能看到的**全部**掘金/宿主能力（唯一注入点）。

设计意图：`framework/` 内不得 `import gm.api`（仓规：只有 gm_main 能）。所有掘金原语与
宿主服务（下单事件桥、待对账登记、参数表、时钟、归属锁、风控/审计）都由 `gm_main` 在模块级
构造一份 `GmServices` 传进来。因此：

  · 本包可**离线单测**（注入假 services）；`tests/phase3/test_framework_ownership_gateway.py`
  · 也可换**纸面/回放网关**做干跑（与 `oversold_gap_confirm/executor.py` 同一套注入式思路）

字段一览（全部必需，`gm` 是掘金原语的命名空间）：

    gm.sdk_call(desc, fn, *a, **k)          带硬超时的 SDK 调用（gm_main._sdk_call）
    gm.order_volume / get_orders / current / history_n
    gm.OrderSide_Buy / OrderSide_Sell / OrderType_Market / OrderType_Limit
    gm.PositionEffect_Open / PositionEffect_Close / ADJUST_PREV
    write_order(t, code, side, qty, px, order_type=)  事件桥「委托发出」登记（**孤儿闸依赖**）
    mark_pending_recon(context, code, sym, side, qty, px, orders)
    code_to_gm(code) / raw_code(symbol)
    param(key, default)                     读 PARAMS（策略参数以 <strategy_id>_ 前缀命名）
    clock(context)                          → datetime（实盘=context.now）
    reject_statuses                         同步回报里的拒单终态码元组
    ownership                               framework.ownership.Ownership
    risk(now, kind, detail, code=)          write_risk 包装（可 None ⇒ 静默）
    audit(record)                           _audit_write 包装（可 None ⇒ 静默）
    kill_switch()                           → bool：人工红按钮文件是否存在（可 None ⇒ 视为不存在）
"""
from __future__ import annotations


class GmServices(object):
    """掘金能力的只读句柄。gm_main 构造一次，全局复用。"""

    __slots__ = ("gm", "sdk_call", "write_order", "write_order_ack", "mark_pending_recon",
                 "code_to_gm", "raw_code", "param", "clock", "reject_statuses", "ownership",
                 "risk", "audit", "kill_switch")

    def __init__(self, *, gm, sdk_call, write_order, mark_pending_recon, code_to_gm,
                 raw_code, param, clock, reject_statuses, ownership,
                 risk=None, audit=None, kill_switch=None, write_order_ack=None):
        self.gm = gm
        self.sdk_call = sdk_call
        self.write_order = write_order
        self.write_order_ack = write_order_ack
        self.mark_pending_recon = mark_pending_recon
        self.code_to_gm = code_to_gm
        self.raw_code = raw_code
        self.param = param
        self.clock = clock
        self.reject_statuses = tuple(reject_statuses)
        self.ownership = ownership
        self.risk = risk
        self.audit = audit
        # 人工红按钮（存在即禁新开仓）。宿主注入 `gm_bridge.writer.check_kill_switch`；
        # 未注入 ⇒ 策略侧按"不存在"处理（测试/干跑友好）。
        self.kill_switch = kill_switch
