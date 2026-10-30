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

import inspect
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

    def _get_orders_for_symbol(self, code: str, desc: str):
        """读取并**强制按 symbol 过滤**当日委托。

        新旧 GM 的 `get_orders` 签名不同：带 symbol 调用 TypeError 时回退全账户。
        两条路径都必须在本层再过滤——全账户回退尤其不能把其它票的在途单算进来；
        缺 symbol 的记录也不猜、不接纳。
        """
        gm = self._gm
        sym = self.sv.code_to_gm(code)
        try:
            orders = self.sv.sdk_call("%s_%s" % (self.strategy_id, desc),
                                      partial(gm.get_orders, symbol=sym))
        except TypeError:
            orders = self.sv.sdk_call("%s_%s_all" % (self.strategy_id, desc), gm.get_orders)
        rows = orders if isinstance(orders, list) else [orders]
        out = []
        for od in rows:
            try:
                osym = (od.get("symbol") if isinstance(od, dict)
                        else getattr(od, "symbol", None))
                if str(osym or "") == str(sym):
                    out.append(od)
            except Exception:
                continue
        return out

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
        # 同步回报状态语义：3 全成 / 5 已撤 / 6 待撤 / 8 拒单 / 12 过期。
        # status=3 也必须有真实成交量才可记 filled；filled_volume 优先，volume 仅在
        # 全成状态可作总量。否则保持 submitted，绝不以 0 股伪报全成或让 pending 消失。
        _oid, _sync_status, _raw_status = None, "submitted", None
        _filled_qty, _filled_price, _sync_message = 0, None, ""
        for _od in (_o if isinstance(_o, list) else [_o]):
            if not isinstance(_od, dict):
                continue
            _oid = _oid or (_od.get("id") or _od.get("order_id"))
            _st = int(_od.get("status") or 0)
            if _st == 3:
                _fq = int(_od.get("filled_volume") or _od.get("volume") or 0)
            else:
                # 非全成状态的 volume 是委托量，不能当作成交量。
                _fq = int(_od.get("filled_volume") or _od.get("filled_qty") or 0)
            if _fq > 0:
                _filled_qty = _fq
                _filled_price = float(_od.get("filled_vwap") or _od.get("vwap")
                                      or _od.get("filled_price") or 0) or None
            if _st == 3:
                _raw_status = _st
                if _fq >= int(qty) and int(qty) > 0:
                    _sync_status, _sync_message = "filled", ""
                elif _fq > 0:
                    _sync_status = "partial"
                    _sync_message = "同步回报 status=3 但成交量小于委托量"
                else:
                    _sync_status = "submitted"
                    _sync_message = "同步回报 status=3 但成交量缺失"
            elif _st == 5:
                _sync_status, _raw_status = "cancelled", _st
            elif _st == 6:
                _sync_status, _raw_status = "pending_cancel", _st
            elif _st == 12:
                _sync_status, _raw_status = "expired", _st
            elif _st in self.sv.reject_statuses:
                _sync_status, _raw_status = "rejected", _st
            elif _st == 2 and _fq > 0:
                _sync_status, _raw_status = "partial", _st
        if _oid:
            _ack = getattr(self.sv, "write_order_ack", None)
            if _ack:
                _ack(str(now), code, side, int(qty), px,
                     order_id=str(_oid), order_type=self.strategy_id)
        if _sync_status != "submitted":
            return {"order_id": _oid, "status": _sync_status, "raw_status": _raw_status,
                    "filled_qty": _filled_qty, "filled_price": _filled_price,
                    "message": _sync_message or "同步回报 status=%s" % _raw_status}
        return {"order_id": _oid, "status": "submitted", "raw_status": _raw_status,
                "filled_qty": 0, "filled_price": None, "message": _sync_message}

    def poll_order(self, order_id, code: str) -> dict:
        """轮询单票委托；3/5/6/8/12 分语义返回，并保留 raw_status。"""
        _orders = self._get_orders_for_symbol(code, "get_orders")
        for _od in (_orders or []):
            try:
                if str(_od.get("id") or _od.get("order_id") or "") != str(order_id):
                    continue
                _st = int(_od.get("status") or 0)
                _fq = int(_od.get("filled_volume") or _od.get("volume") or 0)
                _fp = float(_od.get("filled_vwap") or _od.get("vwap")
                            or _od.get("price") or 0) or None
                if _st == 3:
                    return {"order_id": order_id, "status": "filled", "raw_status": _st,
                            "filled_qty": _fq, "filled_price": _fp, "message": ""}
                if _st == 5:
                    return {"order_id": order_id, "status": "cancelled", "raw_status": _st,
                            "filled_qty": _fq, "filled_price": _fp, "message": "status=5"}
                if _st == 6:                       # 撤单在途：钱**未**解冻
                    return {"order_id": order_id, "status": "pending_cancel", "raw_status": _st,
                            "filled_qty": _fq, "filled_price": _fp, "message": "status=6"}
                if _st == 12:
                    return {"order_id": order_id, "status": "expired", "raw_status": _st,
                            "filled_qty": _fq, "filled_price": _fp, "message": "status=12"}
                if _st in self.sv.reject_statuses:
                    return {"order_id": order_id, "status": "rejected", "raw_status": _st,
                            "filled_qty": _fq, "filled_price": _fp,
                            "message": "status=%s" % _st}
                if _fq > 0:
                    return {"order_id": order_id, "status": "partial", "raw_status": _st,
                            "filled_qty": _fq, "filled_price": _fp,
                            "message": "status=%s" % _st}
                return {"order_id": order_id, "status": "submitted", "raw_status": _st,
                        "filled_qty": 0, "filled_price": None,
                        "message": "status=%s" % _st}
            except Exception:
                continue
        return {"order_id": order_id, "status": "submitted", "raw_status": None,
                "filled_qty": 0, "filled_price": None, "message": "order_not_found"}

    # ── 撤单（2026-10-14 新增：买不进替补机制要用）────────────────
    def _find_order_record(self, order_id: str, code: str):
        """按 order_id / cl_ord_id 找回**完整委托记录**（含 cl_ord_id/account_id）。

        2026-10-19 生产事故：GM 的真实 `order_cancel(wait_cancel_orders)` 只认
        `{"cl_ord_id", "account_id"}`，而策略侧保存的是**柜台 order_id** ⇒
        `order_cancel(order_id=...)` 直接 TypeError，撤单失败、限价买单一直挂着、资金冻结。
        本方法把两边对上；`get_orders()` 在新版 GM 上**不接受 symbol 参数**（旧调用会
        TypeError），故保留"带参 → 无参"两级降级。
        """
        try:
            _orders = self._get_orders_for_symbol(code, "get_orders_find")
        except Exception:
            return None
        for _od in (_orders if isinstance(_orders, list) else [_orders]):
            if not isinstance(_od, dict):
                continue
            _oid = str(_od.get("order_id") or _od.get("id") or "")
            _cid = str(_od.get("cl_ord_id") or "")
            if order_id and (str(order_id) == _oid or str(order_id) == _cid):
                return _od
        return None

    def _resolve_open_buy_order_id(self, code: str):
        """按 symbol 找回唯一在途买单；6 始终在途，同票多单绝不猜测。"""
        try:
            _orders = self._get_orders_for_symbol(code, "get_orders_resolve")
        except Exception:
            return None, "resolve_error"
        # 终态不继承历史 reject_statuses（其中曾误含 6）；5/8/12 与 3 才可清，6 始终在途。
        terminal = {3, 4, 5, 8, 12}
        cands = []
        for _od in (_orders if isinstance(_orders, list) else [_orders]):
            try:
                _st = int((_od.get("status") if isinstance(_od, dict)
                           else getattr(_od, "status", 0)) or 0)
                _side = int((_od.get("side") if isinstance(_od, dict)
                             else getattr(_od, "side", 0)) or 0)
                if _st in terminal or _side != 1:
                    continue
                _id = ((_od.get("id") if isinstance(_od, dict)
                        else getattr(_od, "id", None))
                       or (_od.get("order_id") if isinstance(_od, dict)
                           else getattr(_od, "order_id", None)))
                cands.append(_id)
            except Exception:
                continue
        if not cands:
            return None, "no_open_order"           # 已成交/已拒/已撤 ⇒ 不在途
        if len(cands) > 1:
            return None, "ambiguous_open_orders"   # 多在途买单 ⇒ 不猜，交人工
        if not cands[0]:
            return None, "order_id_unavailable"    # 找到了在途单但它也没 id ⇒ 撤不了
        return cands[0], ""

    def cancel_order(self, order_id, code: str) -> dict:
        """撤单。返回 `{ok, order_id, message}`；**任何失败都不抛**（原语缺席/异常都降级）。

        ⚠️ 本机 `gm` 是**空命名空间桩**（`site-packages/gm` 无成员）⇒ 撤单原语的**确切函数名
        未经本机验证**，生产侧靠 `gm_main._GmPrimitives` 的 getattr 防御式绑定兜住。
        原语缺席 ⇒ `{ok: False, message: "cancel_unavailable"}`，上层据此"不做补单"
        （**绝不会**误下单、误释放额度），只在日志里报一行。

        `order_id` 为空（GM 仿真同步回报无 id 是常态）⇒ 先经 `_resolve_open_buy_order_id`
        按 symbol 找回在途买单 id 再撤；找回失败按 reason 降级：
          · `no_open_order` ⇒ 返回带 `not_found=True`（单已不在途：成交/拒/撤），
            上层据此归入"未确认"而**不是**"撤单失败"（不触发补单中止）；
          · 其余 ⇒ `{ok: False}`，上层按"撤单失败 ⇒ 本轮不补单"处理。

        ⚠️ 撤单**不写 `write_order`**：那是孤儿闸 `_strategy_ordered_today` 判"本策略今日是否对
        code 下过单"的判据，撤单不是新委托；写了会让孤儿闸误判归属。
        """
        if not order_id:
            order_id, _why = self._resolve_open_buy_order_id(code)
            if not order_id:
                return {"ok": False, "order_id": None, "message": _why,
                        "not_found": _why == "no_open_order"}
        gm = self._gm
        _oc = getattr(gm, "order_cancel", None)
        if _oc is None:
            return {"ok": False, "order_id": order_id, "message": "cancel_unavailable"}
        try:
            # 2026-10-19：**两种 SDK 形态都要兼容**。
            #  · 旧/假实现：`order_cancel(order_id=...)`（测试桩、部分券商适配层）
            #  · GM 真实实现：`order_cancel(wait_cancel_orders)`，每项必须是
            #    `{"cl_ord_id": ..., "account_id": ...}` —— 传 order_id 直接 TypeError
            #    （今日 002368 撤单失败、99450 资金冻结的直接原因）。
            try:
                _params = inspect.signature(_oc).parameters
            except Exception:
                _params = {}
            _kw_ok = ("order_id" in _params) or any(
                p.kind == p.VAR_KEYWORD for p in _params.values())
            if _kw_ok:
                _call = partial(_oc, order_id=order_id)
            else:
                _od = self._find_order_record(str(order_id), code)
                _cid = (_od or {}).get("cl_ord_id")
                if not _cid:
                    return {"ok": False, "order_id": order_id,
                            "message": "cl_ord_id_unavailable"}
                _call = partial(_oc, {"cl_ord_id": _cid,
                                      "account_id": _od.get("account_id") or ""})
            _rep = self.sv.sdk_call("%s_cancel" % self.strategy_id, _call)
        except Exception as _e:
            return {"ok": False, "order_id": order_id,
                    "message": "%s: %s" % (type(_e).__name__, str(_e)[:80])}
        # 掘金撤单同步返回形态未定：列表/字典/对象都容忍，取到"有没有明确失败"即可；
        # 真正的终态由 `poll_order` 确认（见 backfill.cancel_and_confirm）。
        _bad = None
        for _od in (_rep if isinstance(_rep, list) else [_rep]):
            _st = None
            if isinstance(_od, dict):
                _st = _od.get("status")
            else:
                _st = getattr(_od, "status", None)
            if _st is not None and int(_st) != 6 and int(_st) in self.sv.reject_statuses:
                _bad = int(_st)
        if _bad is not None:
            return {"ok": False, "order_id": order_id, "message": "cancel_rejected:status=%s" % _bad}
        return {"ok": True, "order_id": order_id, "message": ""}
