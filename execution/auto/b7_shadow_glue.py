# -*- coding: utf-8 -*-
"""b7_shadow_glue.py — B7 影子通道可测纯逻辑（守卫链求值 / skip reason 优先级 / 结算配对）。

2026-09-15 B7影子施工4/4（接入专员_B7）：从 gm_main 影子挂钩中抽离的纯逻辑。
gm_main 依赖 GM SDK 不可直接 import，故影子判定/结算中的可抽离部分集中在本模块，
供 t_io/validation/b7_shadow/ 离线测试直接 import。本模块不 import gm.api，纯标准库。

方案：doc/solutions/2026-09-15_B7尾盘反T通道施工方案.md §1/§3/§4。
"""
from __future__ import annotations

SIGNAL_THRESHOLD = 0.01          # S1：尾盘30min涨幅 > 1%（与 overnight_reverse_t 同口径）

# skip reason 全集（拍板枚举）；guard_decision 按下标顺序求值优先级
# 2026-09-15 B7实单施工：追加 "no_available"（实单专用——T+1 可用量钳制后不足 100 股，
# 影子通道不下单永不产生；仅追加不改既有六项顺序，影子行为零变化）。
SKIP_REASONS = ("breaker_tripped", "has_awaiting_buyback", "pos_below_base",
                "protect_sell_today", "in_b7_chain", "qty_below_100",
                "no_available")

# 实单次日接回重试上限：>30 次（逐 bar 约半小时）仍失败 → 链转 void（复盘红色项）
BUYBACK_MAX_RETRY = 30


def dispatch_mode(live_enabled, shadow_enabled):
    """B7 通道互斥分派（2026-09-15 B7实单施工）：live 严格优先于 shadow；全关 → "off"。

    14:55 挂钩点与次日首根 bar 结算挂钩点共用一个口径，保证任一时刻只有一条通道
    在评估/结算（实单模式下影子仅作台账数据源，不再做信号评估与虚拟结算）。"""
    if live_enabled:
        return "live"
    if shadow_enabled:
        return "shadow"
    return "off"


def clamp_available_qty(qty, *, pos_qty, available, inflight):
    """实单 T+1 可用量钳制（sell_channels TAIL 归位同口径，2026-09-15 B7实单施工）。

    qty 不得超过 GM 持仓可用量（available=None 时按 pos_qty 计）减去在途冻结 inflight，
    结果整百向下取整。返回 (clamped_qty, reason)：≥100 → (q, "")；不足 → (0, "no_available")。"""
    avail = int(pos_qty) if available is None else int(available)
    cap = max(0, min(int(pos_qty), avail) - int(inflight or 0))
    q = min(int(qty), cap) // 100 * 100
    return (q, "") if q >= 100 else (0, "no_available")


def buyback_retry_step(retry_count, order_ok, max_retry=BUYBACK_MAX_RETRY):
    """实单次日接回重试决策（2026-09-15 B7实单施工）。

    order_ok=True  → ("settle", 0)：下单成功，链可结算，计数清零；
    失败且未超上限 → ("retry", n+1)：保留链 armed，下一 bar 重试；
    失败且超上限   → ("void", n+1)：转 void reason="buyback_order_failed"（红色留痕）。"""
    if order_ok:
        return "settle", 0
    n = int(retry_count) + 1
    return ("void", n) if n > int(max_retry) else ("retry", n)


def guard_decision(*, tail30, breaker_tripped, has_awaiting_buyback,
                   pos_qty, base_ref, protect_sold_today, in_b7_chain,
                   threshold=SIGNAL_THRESHOLD):
    """B7 影子卖出守卫链求值（14:55 bar 调用一次）。

    返回 (decision, reason)，decision ∈ {"none", "skip", "go"}：
      none — tail30 为 None 或 ≤ 阈值：未触发，什么都不记（不是 skip，不留痕）；
      skip — 触发但被守卫拦截：reason 按拍板枚举的固定优先级求值
             breaker_tripped > has_awaiting_buyback > pos_below_base
             > protect_sell_today > in_b7_chain；
      go   — 全过：进入 detect_signal 复核与虚拟量闸（qty_gate）。
    """
    if tail30 is None or tail30 <= threshold:
        return "none", ""
    if breaker_tripped:
        return "skip", "breaker_tripped"
    if has_awaiting_buyback:
        return "skip", "has_awaiting_buyback"
    if not (int(pos_qty) >= int(base_ref) > 0):
        return "skip", "pos_below_base"
    if protect_sold_today:
        return "skip", "protect_sell_today"
    if in_b7_chain:
        return "skip", "in_b7_chain"
    return "go", ""


def qty_gate(virtual_qty):
    """虚拟卖出量闸：<100 股不开链 → ("skip", "qty_below_100")；否则 ("go", "")。"""
    return ("go", "") if int(virtual_qty) >= 100 else ("skip", "qty_below_100")


def armed_chain_id(chains, code):
    """该 code 是否已有 armed B7 链 → chain_id 或 None（同票同时只允许一条 B7 链）。"""
    for cid, ch in (chains or {}).items():
        if ch.get("code") == code and ch.get("status", "armed") == "armed":
            return cid
    return None


def find_due_chains(chains, code, today_str):
    """该 code 在 today_str 应结算的 armed 链（次日开盘接回口径：bar 日期 > sell_date）。

    返回 [(chain_id, chain)]，按 sell_date 升序；同票同日唯一链，正常至多一条。
    当日 armed 的链（sell_date == today_str）不结算；settled/voided 终态链忽略。
    """
    due = [(cid, ch) for cid, ch in (chains or {}).items()
           if ch.get("code") == code
           and ch.get("status", "armed") == "armed"
           and str(ch.get("sell_date", ""))
           and str(today_str) > str(ch.get("sell_date"))]
    due.sort(key=lambda kv: str(kv[1].get("sell_date", "")))
    return due


def sell_entry_from_chain(chain):
    """armed 链 → ledger.record_virtual_buyback 所需的 sell_entry（结算配对）。"""
    return {"code": chain.get("code"),
            "qty": int(chain.get("qty", 0) or 0),
            "sell_px": float(chain.get("sell_px", 0) or 0),
            "sell_date": str(chain.get("sell_date", "")),
            "chain_id": str(chain.get("chain_id", ""))}
