# -*- coding: utf-8 -*-
"""`GmGateway` —— **通用**掘金下单通道（策略无关；原 `gm_main._OscGmGateway` 平移）。

一个策略一个实例：`GmGateway(context, strategy_id="osc", services=SERVICES)`。
`strategy_id` 同时充当三件事，**必须一致**：

    ① 事件桥 `order_type` 标签（孤儿闸按它判归属）
    ② 参数前缀（`<id>_limit_slippage_bp`）
    ③ 归属表里的所有者名（`framework.ownership`）

## 与旧版（`_OscGmGateway`）的差异 —— 只有两处

1. **新增归属硬锁**（owner 2026-10-12 拍板「一票一策略」）：买单发出**之前**先问
   `ownership.can_open()`（判据 = 各策略账本 × 账户实时持仓，见 `framework/ownership.py`）；
   被别的策略持有（或账户持有但无人认领）⇒ **不下单、不落 order 事件**，
   返回 `status="rejected", message="ownership_lock:held_by:xxx"`。选 `rejected` 是因为它已是
   执行器的终态语义（不入账本、计入 `rejected_count`、留痕可查）。
   ⚠️ 被拒订单**刻意不写 write_order**：写了会让孤儿闸以为"本策略今天对这只票下过单"。
2. `order_type` / 参数前缀 / SDK 调用描述名由策略 id 生成（原先硬编码 `"OSC"`/`osc_*`）。

其余（市价单风格、先 `write_order` 再 `order_volume`、`_mark_pending_recon` 待对账、
同步拒单码元组、`poll_order` 轮询）**逐行保持原样**，行为等价由
`tests/phase3/test_framework_ownership_gateway.py` + `test_gm_main_osc_hooks.py` 钉住。
"""
from __future__ import annotations

from datetime import datetime
from functools import partial


class GmGateway(object):
    """掘金下单 Gateway（仅 MODE_LIVE 实例化）。

    标的**不限于 subscribe 池**（选股范围可超出），故取价走 `current()` 实时快照。
    """

    def __init__(self, context, strategy_id: str, services, log=print):
        self.context = context
        self.strategy_id = str(strategy_id)
        self.sv = services
        self.log = log

    # ── 便捷 ────────────────────────────────────────────────
    @property
    def _gm(self):
        return self.sv.gm

    def _now(self):
        return self.context.now if hasattr(self.context, "now") else datetime.now()

    def _param(self, suffix: str, default=0):
        return self.sv.param("%s_%s" % (self.strategy_id, suffix), default)

    # ── 账户 ────────────────────────────────────────────────
    def get_cash(self) -> float:
        try:
            _acct = self.sv.sdk_call("%s_cash" % self.strategy_id, self.context.account)
            _c = getattr(_acct, "cash", None)
            if _c is not None:
                _c = _c() if callable(_c) else _c
                if isinstance(_c, dict):
                    return float(_c.get("available") or _c.get("available_cash")
                                 or _c.get("cash") or _c.get("total") or 0)
                return float(_c or 0)
        except Exception as _e:
            self.log("[%s] ⚠️ get_cash 失败（按 0，买单将全部截断）: %s" % (self.strategy_id, _e))
        return 0.0

    def get_positions(self) -> dict:
        out = {}
        try:
            _acct = self.sv.sdk_call("%s_account" % self.strategy_id, self.context.account)
            for _p in (_acct.positions() or []):
                _sym = (_p.get("symbol") if isinstance(_p, dict)
                        else getattr(_p, "symbol", None))
                if not _sym:
                    continue
                _vol = int((_p.get("volume") if isinstance(_p, dict)
                            else getattr(_p, "volume", 0)) or 0)
                if _vol <= 0:
                    continue
                _av = int((_p.get("available") if isinstance(_p, dict)
                           else getattr(_p, "available", 0)) or 0)
                _cost = float((_p.get("vwap") if isinstance(_p, dict)
                               else getattr(_p, "vwap", 0)) or 0)
                out[self.sv.raw_code(str(_sym))] = {"qty": _vol, "available": _av, "cost": _cost}
        except Exception as _e:
            self.log("[%s] ⚠️ get_positions 失败（按空持仓——买单将按 0 额度截断）: %s"
                     % (self.strategy_id, _e))
        return out

    def get_open_price(self, code: str):
        """当日开盘价：current() 快照（含 today open）；失败回退 history_n 60s 首根。"""
        gm = self._gm
        sym = self.sv.code_to_gm(code)
        now = self._now()
        try:
            _cur = self.sv.sdk_call("%s_current" % self.strategy_id, partial(gm.current, symbols=sym))
            if _cur:
                _c0 = _cur[0] if isinstance(_cur, (list, tuple)) else _cur
                _op = float((_c0.get("open") if isinstance(_c0, dict)
                             else getattr(_c0, "open", 0)) or 0)
                if _op > 0:
                    return _op
        except Exception:
            pass
        try:
            _his = self.sv.sdk_call("%s_open_bar" % self.strategy_id, partial(
                gm.history_n, symbol=sym, frequency="60s", count=8,
                end_time=now, fields="symbol,eob,open",
                fill_missing="Previous", adjust=gm.ADJUST_PREV))
            _today = str(now.date())
            for _b in (_his or []):
                _eob = str((_b.get("eob") if isinstance(_b, dict)
                            else getattr(_b, "eob", "")) or "")
                _op = float((_b.get("open") if isinstance(_b, dict)
                             else getattr(_b, "open", 0)) or 0)
                if _eob[:10] == _today and _op > 0:
                    return _op
        except Exception:
            pass
        return None

    # ── 归属硬锁（新增，2026-10-12）────────────────────────────
    def _lock_check(self, code: str):
        held = 0
        try:
            pos = self.get_positions() or {}
            held = int((pos.get(str(code)) or {}).get("qty") or 0)
        except Exception:
            held = 0
        return self.sv.ownership.can_open(code, self.strategy_id, held_qty=held)

    def _report_lock(self, code, qty, px, now, why):
        det = ("%s 归属锁定⇒不下单 code=%s qty=%s px=%s owner=%s（一票一策略，owner 2026-10-12）"
               % (self.strategy_id, code, qty, px, why))
        self.log("[%s] ⛔ %s" % (self.strategy_id, det))
        try:
            if self.sv.risk:
                self.sv.risk(str(now), "strategy_lock_conflict", det, code=str(code))
        except Exception:
            pass
        try:
            if self.sv.audit:
                self.sv.audit({"event": "strategy_lock_conflict", "strategy": self.strategy_id,
                               "code": str(code), "side": "BUY", "qty": int(qty or 0),
                               "price": float(px or 0), "reason": why, "time": str(now)})
        except Exception:
            pass

    # ── 下单 ────────────────────────────────────────────────
    def place_order(self, code: str, side: str, qty: int, price) -> dict:
        gm = self._gm
        sym = self.sv.code_to_gm(code)
        now = self._now()
        px = float(price or 0)

        # ⓪ 归属硬锁（仅开仓）：被别的策略持有 ⇒ 直接拒，且**不落 order 事件**
        if side == "BUY":
            ok, why = self._lock_check(code)
            if not ok:
                self._report_lock(code, qty, px, now, why)
                return {"order_id": None, "status": "rejected", "filled_qty": 0,
                        "filled_price": None, "message": "ownership_lock:%s" % why}

        # ① 先落 order 事件 —— **硬要求**：孤儿闸 `_strategy_ordered_today` 靠
        #    「当日 events 里有没有本策略对该 code 的 order」区分自营单与终端手工单，
        #    不落这条就认不出自己的成交（6a96829c 前科：5/6 底仓成交被当孤儿丢弃）。
        #    `order_type` = 策略 id ⇒ 多策略下孤儿闸按 (code, order_type) 判归属。
        self.sv.write_order(str(now), code, side, int(qty), px, order_type=self.strategy_id)
        _slip_bp = int(self._param("limit_slippage_bp", 0) or 0)
        if _slip_bp > 0 and px > 0:
            # 限价口径：开盘价±滑点容忍（买+/卖-），0 则沿用 gm_main 市价风格
            _lp = px * (1 + _slip_bp / 1e4) if side == "BUY" else px * (1 - _slip_bp / 1e4)
            _kw = dict(order_type=gm.OrderType_Limit, price=round(_lp, 2))
        else:
            _kw = dict(order_type=gm.OrderType_Market)
        _o = self.sv.sdk_call("%s_order_%s" % (self.strategy_id, side.lower()), partial(
            gm.order_volume, symbol=sym, volume=int(qty),
            side=gm.OrderSide_Buy if side == "BUY" else gm.OrderSide_Sell,
            position_effect=(gm.PositionEffect_Open if side == "BUY"
                             else gm.PositionEffect_Close),
            **_kw))
        self.sv.mark_pending_recon(self.context, code, sym, side, int(qty), px, _o)
        # 同步返回里直接带拒单（status∈4/5/6/8/12）的情形与既有口径对齐
        _oid, _rej = None, None
        for _od in (_o if isinstance(_o, list) else [_o]):
            if isinstance(_od, dict):
                _oid = _oid or (_od.get("id") or _od.get("order_id"))
                if _rej is None and int(_od.get("status") or 0) in self.sv.reject_statuses:
                    _rej = "status=%s" % _od.get("status")
        if _rej:
            return {"order_id": _oid, "status": "rejected", "filled_qty": 0,
                    "filled_price": None, "message": "同步回报拒单 %s" % _rej}
        return {"order_id": _oid, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": ""}

    def poll_order(self, order_id, code: str) -> dict:
        """get_orders 轮询终态（掘金拒单/成交异步，与 _poll_pending_recon 同款渠道）。"""
        gm = self._gm
        sym = self.sv.code_to_gm(code)
        try:
            _orders = self.sv.sdk_call("%s_get_orders" % self.strategy_id,
                                       partial(gm.get_orders, symbol=sym))
        except TypeError:
            _orders = self.sv.sdk_call("%s_get_orders_all" % self.strategy_id, gm.get_orders)
        for _od in (_orders or []):
            try:
                if str(_od.get("id") or _od.get("order_id") or "") != str(order_id):
                    continue
                _st = int(_od.get("status") or 0)
                _fq = int(_od.get("filled_volume") or _od.get("volume") or 0)
                _fp = float(_od.get("filled_vwap") or _od.get("vwap")
                            or _od.get("price") or 0) or None
                if _st == 3:
                    return {"order_id": order_id, "status": "filled",
                            "filled_qty": _fq, "filled_price": _fp, "message": ""}
                if _st in self.sv.reject_statuses:
                    return {"order_id": order_id, "status": "rejected",
                            "filled_qty": 0, "filled_price": None,
                            "message": "status=%s" % _st}
                if _fq > 0:
                    return {"order_id": order_id, "status": "partial",
                            "filled_qty": _fq, "filled_price": _fp,
                            "message": "status=%s" % _st}
                return {"order_id": order_id, "status": "submitted",
                        "filled_qty": 0, "filled_price": None,
                        "message": "status=%s" % _st}
            except Exception:
                continue
        return {"order_id": order_id, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": "order_not_found"}
