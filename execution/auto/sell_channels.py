# -*- coding: utf-8 -*-
"""sell_channels.py — P0-P6 卖出通道 + _sell_arbiter（P4-1 迁入，逻辑与 goldminer main.py 逐字一致）。

★独立边界：止盈止损与执行管理保持独立（方案 §5.5）。本模块不 import gm.api；
gm 符号与 gm_main 模块级名字经 `_bind_gm(gm)` 注入（由 gm_main 在 import 后调用），
保持函数体与迁移前完全一致。
"""
import os
import sys
from datetime import timedelta

_PROJ = os.path.dirname(os.path.abspath(__file__))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)
_GM_DIR = os.path.join(_PROJ, "_gm")
if _GM_DIR not in sys.path:
    sys.path.insert(0, _GM_DIR)

from data.indicators import Signal  # noqa: E402
from gm_bridge.writer import write_order, write_risk  # noqa: E402
from sell_state import _sell_state_persist  # noqa: E402

# 由 gm_main 注入（gm_main import 后调用 _bind_gm(gm)）——保持「唯一 import gm.api 在 gm_main」
PARAMS = None
STOCK_PARAMS = None
STOCK_NAMES = None
_audit_write = None
order_volume = None
OrderSide_Sell = None
OrderType_Market = None
PositionEffect_Close = None


def _bind_gm(gm):
    """从 gm_main 绑定卖出所需符号。可调用项（order_volume/_audit_write）用委托包装——
    运行时读 gm 模块当前值，测试 patch gm_main.order_volume 即可同时拦截 BUY(gm_main) 与 SELL(本模块)。"""
    global PARAMS, STOCK_PARAMS, STOCK_NAMES, _audit_write
    global order_volume, OrderSide_Sell, OrderType_Market, PositionEffect_Close
    PARAMS = gm.PARAMS
    STOCK_PARAMS = gm.STOCK_PARAMS
    STOCK_NAMES = gm.STOCK_NAMES

    def _order_volume(**kw):
        return gm.order_volume(**kw)

    def _audit_write_impl(entry):
        return gm._audit_write(entry)

    order_volume = _order_volume
    _audit_write = _audit_write_impl
    OrderSide_Sell = gm.OrderSide_Sell
    OrderType_Market = gm.OrderType_Market
    PositionEffect_Close = gm.PositionEffect_Close


def _sell_arbiter(context, code, sig, pos_qty, cp, now, holding, threshold,
                  stock_params, gm_sym):
    """执行卖出：地板检查 + sizer + 下单 + 持仓更新 + 审计。
    返回 True=已执行, False=被拦截/跳过。"""
    sc = context.daily_sell_count.get(code, 0)
    max_sells = stock_params.get("max_sell_times_per_stock", 3)

    # F9: 在途卖单守卫（2026-07-31 PANIC 0.33秒内连发4单、5单超可用持仓事故）
    # 成交回报到达前冷却未生效（P0-2设计），在途期间禁止再发任何卖单
    _inflight = int(getattr(context, "_inflight_sell", {}).get(gm_sym, 0) or 0)
    if _inflight >= 100:
        _audit_write({"event": "inflight_skip", "code": code, "action": sig.action,
                      "inflight": _inflight, "time": str(now)})
        return False

    # 地板检查
    base_ref = getattr(context, f"_base_ref_{code}", pos_qty)
    setattr(context, f"_base_ref_{code}", base_ref)
    _is_protection = sig.action in ("PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "MA5_EXIT")
    sell_floor_ratio = 0.0 if _is_protection else float(PARAMS.get("sell_floor_ratio", 0.5))
    min_hold = int(base_ref * sell_floor_ratio)
    if pos_qty - 100 < min_hold:
        # WP-B15: 地板拦截去重——同持仓状态重复拦截静默，状态(pos/min_hold)变化再写（O-03 风格）
        _floor_key = f'_floor_logged_{code}_{sig.action}'
        _floor_state = f'{now.strftime("%Y-%m-%d")}_{pos_qty}_{min_hold}'
        if getattr(context, _floor_key, '') != _floor_state:
            setattr(context, _floor_key, _floor_state)
            try:
                write_risk(str(now), "floor_protection",
                           f"base_ref={base_ref} min_hold={min_hold} pos_qty={pos_qty} action={sig.action}", code=code)
            except Exception: pass
            _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                          "score": sig.score, "reason": "floor",
                          "base_ref": base_ref, "min_hold": min_hold, "pos_qty": pos_qty, "time": str(now)})
        # WP-B15: TARGET 地板可预见拦截 → mute 同源信号（事件层去重；决策/执行不受影响）
        if sig.action == "TARGET_SELL":
            setattr(context, f'_sig_muted_{code}_TARGET_SELL', now.strftime("%Y-%m-%d"))
        if sig.action == "PANIC_SELL":
            context.engine.record_trade_action(code, "PANIC_SELL", 0, cp)
        return False

    # 阈值检查
    if sig.score < threshold:
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "threshold",
                      "threshold": threshold, "time": str(now)})
        return False

    # 日计数检查（保护类卖出豁免）
    if not _is_protection and sc >= max_sells:
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "daily_count",
                      "sc": sc, "max_sells": max_sells, "time": str(now)})
        return False

    # sizer 计算卖出量
    if sig.action == "MA5_EXIT":
        # WP-B19 a: 全离（可用量，非 sizer 定量；遵守 T+1——当日买入锁定部分次日破位续卖）
        _avail_raw = holding.get("available")
        _avail = pos_qty if _avail_raw is None else int(_avail_raw)
        qty = max(0, min(pos_qty, _avail) - _inflight)
    else:
        qty = context.sizer.calc_sell_qty(code, holding, sig.score, threshold, used_sells=sc)
        if qty < 100:
            qty = min(300, pos_qty)
        # F13: TREND_EXIT 量封顶到超 base_ref 部分——设计语义"只卖利润仓/超额仓，
        # 底仓本体永不触发"(WP-F8)，sizer 定量可能越界(WP-B回放包fix4实证:
        # pos600/base_ref500/excess100 却卖出200→底仓被啃100)
        if sig.action == "TREND_EXIT":
            qty = min(qty, max(0, pos_qty - base_ref))
        # N25-3: T+1 可用量检查 — 区分 None(缺key兜底) 与 0(合法当日全锁)
        _avail_raw = holding.get("available")
        _avail = pos_qty if _avail_raw is None else int(_avail_raw)
        qty = min(qty, pos_qty, _avail)
        # F9: 扣除在途量，委托总量不得超可用持仓
        qty = min(qty, max(0, min(pos_qty, _avail) - _inflight))
    if qty < 100:
        _audit_write({"event": "sell_skip", "code": code, "action": sig.action,
                      "score": sig.score, "reason": "qty",
                      "qty": qty, "pos_qty": pos_qty, "time": str(now)})
        return False

    # 下单 + 持仓更新 + 审计
    try:
        write_order(str(now), code, "SELL", qty, cp)
    except Exception: pass
    try:
        order_volume(symbol=gm_sym, volume=qty,
                     side=OrderSide_Sell,
                     order_type=OrderType_Market,
                     position_effect=PositionEffect_Close)
        # F9: 登记在途量（fill/reject 回调释放）
        if not hasattr(context, "_inflight_sell") or context._inflight_sell is None:
            context._inflight_sell = {}
        context._inflight_sell[gm_sym] = _inflight + qty
        # F9: 下单即计虚冷却——成交回调 record_trade_action 再确认；
        # 拒单不补冷却属保守可接受（当日不再卖，次日 D1 重置）
        try:
            if not hasattr(context.engine, "sell_cooldown") or context.engine.sell_cooldown is None:
                context.engine.sell_cooldown = {}
            _cd = int(context.engine._get_params(code).get("cooldown_minutes", 30))
        except Exception:
            _cd = 30
        context.engine.sell_cooldown[code] = now + timedelta(minutes=_cd)
        context.daily_sell_count[code] = sc + 1
        context.total_trade_count += 1
        new_pos = max(0, pos_qty - qty)
        if gm_sym in context.manual_position:
            context.manual_position[gm_sym]["qty"] = new_pos
            context.manual_position[gm_sym]["available"] = new_pos
            context.manual_position[gm_sym]["t_qty"] = new_pos
        # WP-B14: TARGET 下单即置 pending 落盘——防下单→成交回调间竞态重复触发
        if sig.action == "TARGET_SELL" and gm_sym in context.manual_position:
            context.manual_position[gm_sym]["_target_l1_state"] = "pending"
            _sell_state_persist(context, code, gm_sym)
        context.engine.sell_count_per_stock[code] = context.daily_sell_count.get(code, 0)
        print(f"[{now:%H:%M:%S}] SELL {code} {qty}@{cp:.2f} score={sig.score:.0f} regime={context.last_index_regime}")
        # N28: 挂接通道信息，成交回调写入action/score
        if not hasattr(context, "_pending_sell_action"):
            context._pending_sell_action = {}
        context._pending_sell_action[gm_sym] = (sig.action, sig.score)
        return True
    except Exception as e:
        print(f"[{now:%H:%M:%S}] SELL {code} 失败: {e}")
        try: write_risk(str(now), "order_failed", f"SELL {qty}@{cp:.2f} err={e}", code=code)
        except Exception: pass
        return False


def _sell_channel_gate(context, code, gm_sym, cp, now, sig, pos_qty, holding, daily_ctx,
                       feats_cache, is_tail, morning_no_buy):
    """P0-P6 卖出通道门链生成卖出信号（与迁移前 gm_main.on_bar 逐字一致）。

    返回 (sig, tail_done)：tail_done=True 表示 TAIL 通道已执行、本 bar 后续逻辑应跳过
    （迁移前的 `continue` 语义）。其余通道只更新 sig/context 状态。
    """
    # ── WP-B19 a/b/d/e: MA5 破位全离通道（第四类保护，优先级最高，TRAIL 之前） ──
    # 静态 MA5（daily_ma5，前 5 日收盘均值，日内不变）；破位→全离（可用量）+ 禁一切买入
    _ma5_val = float(daily_ctx.get("daily_ma5", 0) or 0)
    _ma5_tol = float(STOCK_PARAMS.get(code, {}).get("ma5_break_tolerance", 0.0))
    if _ma5_val > 0 and cp < _ma5_val * (1 - _ma5_tol) and pos_qty > 0:
        from data.indicators import Signal as _Ma5Sig
        sig = _Ma5Sig(code=code, name=STOCK_NAMES.get(code, code),
                      action="MA5_EXIT", price=cp, score=80.0,
                      reasons=[f"MA5破位离场: cp={cp:.2f} < ma5={_ma5_val:.2f}×"
                               f"(1-{_ma5_tol})={_ma5_val*(1-_ma5_tol):.2f} ({cp/_ma5_val-1:.1%})"])
        # 破位日标记（供买侧禁令/留痕查询；破位状态每日由 price vs 当日 MA5 动态重建，无需落盘）
        if not hasattr(context, "_ma5_broken"):
            context._ma5_broken = {}
        context._ma5_broken[code] = now.strftime("%Y-%m-%d")

    # ── B1/T1: TRAIL_SELL 移动止盈 ──
    # TODO(PhaseD): 寻优 ACT_LINE/k/MIN_BACK/MAX_BACK
    _panic_on_cooldown = (code in context.engine.sell_cooldown
                          and now < context.engine.sell_cooldown.get(code, now))
    _profit = feats_cache.get("profit_pct", 0) if feats_cache else 0
    _trail_state = "INACTIVE"
    _trail_peak = 0.0
    if gm_sym in context.manual_position:
        _trail_state = context.manual_position[gm_sym].get("_trail_state", "INACTIVE")
        _trail_peak = context.manual_position[gm_sym].get("_trail_peak", 0.0)
    _trail_state0, _trail_peak0 = _trail_state, _trail_peak
    # 激活: 浮盈 > +8%
    if _trail_state == "INACTIVE" and _profit > 0.08:
        _trail_state = "ARMED"
        _trail_peak = max(cp, _trail_peak)
    # 跟踪: 更新峰值
    if _trail_state == "ARMED":
        _trail_peak = max(_trail_peak, cp)
        # N23/B1': TRAIL k×ATR带界 — 防止回撤阈值自解除
        _daily_atr = daily_ctx.get("daily_atr", 0.02) or 0.02
        _back = max(0.03, min(1.5 * _daily_atr, 0.08))  # TODO(PhaseD): MIN_BACK/k/MAX_BACK
        _drawdown = (_trail_peak - cp) / _trail_peak if _trail_peak > 0 else 0
        if _drawdown > _back and not _panic_on_cooldown and (sig is None or sig.action != "MA5_EXIT"):
            from data.indicators import Signal
            sig = Signal(code=code, name=STOCK_NAMES.get(code, code),
                         action="TRAIL_SELL", price=cp, score=80.0,
                         reasons=[f"移动止盈: peak={_trail_peak:.2f} dd={_drawdown:.1%}"])
            _trail_state = "COOLED"
        context.manual_position[gm_sym]["_trail_state"] = _trail_state
        context.manual_position[gm_sym]["_trail_peak"] = _trail_peak
    # 复位: 全仓清空
    if pos_qty <= 0 and gm_sym in context.manual_position:
        context.manual_position[gm_sym]["_trail_state"] = "INACTIVE"
        context.manual_position[gm_sym]["_trail_peak"] = 0.0
    # WP-B14: TRAIL 状态变更即落盘（跨日续接；仅实际变更才写，避免每 bar 刷盘）
    if gm_sym in context.manual_position and (
            _trail_state0 != context.manual_position[gm_sym].get("_trail_state")
            or _trail_peak0 != context.manual_position[gm_sym].get("_trail_peak")):
        _sell_state_persist(context, code, gm_sym)

    # ── D5/N4: 深度亏损 → PANIC_SELL ──
    # B2/T3: 趋势破坏止盈 TREND_EXIT
    _profit = feats_cache.get("profit_pct", 0)
    _base_ref = getattr(context, f'_base_ref_{code}', 0) or pos_qty
    # N20: 不得覆盖更高优先级信号(P1 PANIC/P2 TRAIL/P0 MA5_EXIT)
    if ((sig is None or sig.action not in ("PANIC_SELL", "TRAIL_SELL", "MA5_EXIT"))
            and _profit > 0 and not _panic_on_cooldown
            and daily_ctx.get("_stock_trend_state") in ("TREND_DOWN", "TREND_BREAKDOWN")):
        from data.indicators import Signal as _Sig
        _excess = max(0, pos_qty - _base_ref) if _base_ref else 0
        if _excess >= 100:
            sig = _Sig(code=code, name=STOCK_NAMES.get(code, code),
                       action="TREND_EXIT", price=cp, score=78.0,
                       reasons=[f"趋势破坏止盈: profit={_profit:.1%} trend={daily_ctx.get('_stock_trend_state')}"])

    # B4/T2: 分批目标止盈 TARGET_SELL
    # TODO(PhaseD): 寻优分档 L1/L2/L3 及批次比例
    if (_profit > 0.10 and not _panic_on_cooldown
            # N20: 不得覆盖更高优先级信号(P0 MA5_EXIT / P1/P2/P3)
            and not (sig and sig.action in ("PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "MA5_EXIT"))):
        # WP-B14: 三段式状态机——state is None 才生成；置位移到下单/成交回调
        # （被缓冲拦/地板拦/未成交的信号不耗档，条件满足后可再触发）
        _l1_state = (context.manual_position.get(gm_sym, {}).get("_target_l1_state")
                     if gm_sym in context.manual_position else None)
        if _l1_state is None:
            from data.indicators import Signal as _Sig
            sig = _Sig(code=code, name=STOCK_NAMES.get(code, code),
                       action="TARGET_SELL", price=cp, score=75.0,
                       reasons=[f"目标止盈L1: profit={_profit:.1%}"])
    # 复位: 全仓清空时重置目标位图（新持仓期重新计数）
    if pos_qty <= 0 and gm_sym in context.manual_position:
        if context.manual_position[gm_sym].get("_target_l1_state") is not None:
            context.manual_position[gm_sym]["_target_l1_state"] = None
            _sell_state_persist(context, code, gm_sym)

    if feats_cache.get("is_deep_loss") and not _panic_on_cooldown and (sig is None or sig.action != "MA5_EXIT"):
        from data.indicators import Signal
        sig = Signal(code=code, name=STOCK_NAMES.get(code, code),
                     action="PANIC_SELL", price=cp, score=75.0,
                     reasons=["深度亏损恐慌卖出", f"profit_pct={feats_cache.get('profit_pct', 0):.2%}"])
        print(f"[{now:%H:%M:%S}] PANIC_SELL {code} {feats_cache.get('hold_qty', 0)}@{cp:.2f} "
              f"profit_pct={feats_cache.get('profit_pct', 0):.2%}")

    # ── D5-c: 尾盘回转（14:50-15:00），超底仓部分强制卖出归位 ──
    if is_tail and pos_qty > getattr(context, '_base_ref_' + code, pos_qty) and not _panic_on_cooldown:
        if sig is None or sig.action not in ('SELL_HIGH', 'PANIC_SELL', 'MA5_EXIT'):
            target = getattr(context, '_base_ref_' + code, pos_qty)
            excess = pos_qty - target
            if excess >= 100:
                qty = (excess // 100) * 100
                # F14: T+1可用量封顶 + 在途扣除（与仲裁器同口径）
                _avail_raw = holding.get("available")
                _avail = pos_qty if _avail_raw is None else int(_avail_raw)
                _tif = int(getattr(context, "_inflight_sell", {}).get(gm_sym, 0) or 0)
                qty = min(qty, max(0, min(pos_qty, _avail) - _tif))
                if qty < 100:
                    return sig, True
                try:
                    write_order(str(now), code, "SELL", qty, cp, order_id="tail")
                except Exception:
                    pass
                try:
                    order_volume(symbol=gm_sym, volume=qty,
                                 side=OrderSide_Sell,
                                 order_type=OrderType_Market,
                                 position_effect=PositionEffect_Close)
                except Exception as e:
                    print(f'[{now:%H:%M:%S}] TAIL {code} 下单失败: {e}')
                    try:
                        write_risk(str(now), "order_failed", f"TAIL SELL {qty}@{cp:.2f} err={e}", code=code)
                    except Exception:
                        pass
                    return sig, True
                if not hasattr(context, "_inflight_sell") or context._inflight_sell is None:
                    context._inflight_sell = {}
                context._inflight_sell[gm_sym] = _tif + qty
                # F14: tail下单即计虚冷却，拒单后不再重试（与仲裁器同模式）
                try:
                    if not hasattr(context.engine, "sell_cooldown") or context.engine.sell_cooldown is None:
                        context.engine.sell_cooldown = {}
                    _cd = int(context.engine._get_params(code).get("cooldown_minutes", 30))
                except Exception:
                    _cd = 30
                context.engine.sell_cooldown[code] = now + timedelta(minutes=_cd)
                # 立即更新 manual_position 防下一分钟重复触发（仅下单成功后；拒单由 status=8 分支回滚）
                if gm_sym in context.manual_position:
                    new_pos = pos_qty - qty
                    context.manual_position[gm_sym]["qty"] = new_pos
                    context.manual_position[gm_sym]["available"] = new_pos
                    context.manual_position[gm_sym]["t_qty"] = new_pos
                print(f'[{now:%H:%M:%S}] TAIL {code} 尾盘归位 {qty}股 (pos={pos_qty}→{pos_qty-qty} target={target})')
                if not hasattr(context, "_pending_sell_action"):
                    context._pending_sell_action = {}
                context._pending_sell_action[gm_sym] = ("TAIL", 0)
                context.total_trade_count += 1
                context.daily_sell_count[code] = context.daily_sell_count.get(code, 0) + 1
                return sig, True

    # ── R3/B5': 开盘卖出缓冲 —— 09:35 前非保护类卖出延后 ──
    # 覆盖全部生成路径（引擎 SELL_HIGH + 门控链 TARGET_SELL）；
    # PANIC/TRAIL/TREND_EXIT/TAIL 保留即时性（fix5 B5 条款）
    if sig and sig.action in ("SELL_HIGH", "TARGET_SELL") and now.hour == 9 and now.minute <= 35:
        _audit_write({"event": "morning_sell_blocked", "code": code, "time": str(now),
                      "action": sig.action, "reason": "开盘缓冲延后"})
        sig = None

    # ── N6: 开盘5分钟拦截买入信号 ──
    if morning_no_buy and sig and sig.action in ('BUY_LOW', 'ADD_POS'):
        sig = None

    # B3/R2: SELL_HIGH 成本锚定 — 亏损单不由 SELL_HIGH 通道卖出
    _cost_anchor = 0.0  # TODO(PhaseD): 寻优 cost_anchor
    if (sig and sig.action == "SELL_HIGH"
            and feats_cache.get("profit_pct", 0) < _cost_anchor):
        sig = None  # 降级：交回 PANIC/TREND_EXIT 接管

    return sig, False
