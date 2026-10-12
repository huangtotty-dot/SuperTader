# -*- coding: utf-8 -*-
"""`Runtime` —— 宿主（gm_main）注入给本策略包的**运行时句柄**。

为什么要有它：本包不得 import `gm.api`（仓规），所以掘金原语、事件桥、参数表、时钟、
风控/审计、归属锁、KILL_SWITCH 全部由宿主注入。`GmServices`（framework）是**多策略共用**的
能力层；`Runtime` 是**本策略专属**的一层（绑账本目录 + 便捷方法 + 网关工厂）。

干跑/单测：给一个假 services 即可，不需要 gm 环境（见
`tests/phase3/test_framework_ownership_gateway.py` 的 `FakeGm` 用法）。
"""
from __future__ import annotations


class Runtime(object):
    __slots__ = ("services", "book_dir", "is_live", "log")

    def __init__(self, services, book_dir: str, is_live=None, log=print):
        """
        services : framework.services.GmServices（必需）
        book_dir : 本策略账本目录（`positions.json` / `fills_*` / `sells_*`）
        is_live  : callable(context) -> bool，默认「context.mode == MODE_LIVE」由宿主给
        log      : 控制台日志出口（gm 终端靠 stdout 落盘，默认 print）
        """
        self.services = services
        self.book_dir = book_dir
        self.is_live = is_live or (lambda ctx: True)
        self.log = log

    # ── 便捷代理（包内代码只认这几个）────────────────────────
    @property
    def gm(self):
        """掘金原语命名空间（history / history_n / order_volume / ADJUST_PREV …）。"""
        return self.services.gm

    def sdk_call(self, desc, fn, *a, **k):
        return self.services.sdk_call(desc, fn, *a, **k)

    def param(self, key, default=None):
        """读 PARAMS（键名带 `osc_` 前缀）。"""
        return self.services.param(key, default)

    def code_to_gm(self, code: str) -> str:
        return self.services.code_to_gm(code)

    def audit(self, record: dict) -> None:
        if self.services.audit:
            self.services.audit(record)

    def risk(self, now, kind, detail, code="") -> None:
        if self.services.risk:
            self.services.risk(str(now), kind, detail, code=code)

    def kill_switch(self) -> bool:
        """人工红按钮（存在即禁新开仓）。宿主未注入 ⇒ False（并只在首次告警）。"""
        fn = getattr(self.services, "kill_switch", None)
        if fn is None:
            return False
        return bool(fn())

    def gateway(self, context):
        """本策略的下单通道（策略 id 绑 `osc`）。"""
        from .gateway import OscGateway
        return OscGateway(context, self.services, log=self.log)
