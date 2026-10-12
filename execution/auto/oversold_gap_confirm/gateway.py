# -*- coding: utf-8 -*-
"""`OscGateway` —— 超跌跳空确认的下单通道 = 通用 `framework.GmGateway` 绑本策略 id。

（历史：原为 S1 执行器而建、类名 `_S1GmGateway` → 2026-10-12 改名 `_OscGmGateway` 并抽出
通用实现到 `framework/gateway.py` → 同日 2026-10-12 随策略包归拢移到这里。）

`STRATEGY_ID = "osc"` 同时是三件事（**必须一致**）：
    ① 事件桥 `order_type` 标签（多策略下孤儿闸按它判归属）
    ② 参数前缀（`osc_limit_slippage_bp`）
    ③ 归属表里的所有者名（`framework.ownership`）

归属硬锁（一票一策略，owner 2026-10-12 拍板）在通用实现里：被别的策略持有、或账户持有但
无人认领 ⇒ **不下单、不落 order 事件**，返回 `rejected` + `ownership_lock:*`。
"""
from __future__ import annotations

from framework.gateway import GmGateway


class OscGateway(GmGateway):
    STRATEGY_ID = "osc"

    def __init__(self, context, services, log=print):
        GmGateway.__init__(self, context, self.STRATEGY_ID, services, log=log)
