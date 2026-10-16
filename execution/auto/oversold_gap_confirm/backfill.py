# -*- coding: utf-8 -*-
r"""backfill.py — 超跌跳空确认 · **买不进替补（顺移）**核（2026-10-14 施工）。

## 为什么有这个东西

2026-10-14 上线首日：top-4 里 gap 最大的两只（600097 开创国际 gap 10.03%、000823 超声电子
gap 9.99%）**全天封死涨停**，市价买单排在巨量封单之后，挂到 15:00 才以 `status=12` 过期，
**19.8 万冻结一整天**。根因是**结构性的**：OSC 按 gap **降序**取 top-N，而 gap 最大 = 跳得最高
= 最接近/已经在涨停价上 ⇒ **信号排序与成交概率天然负相关**。

本模块实现 owner 选定的方向：「买不进就换成能买进的」—— **受限顺移**：
    10:00 首单 → 到点仍有未成交 ⇒ 撤单 → 候补票**时效复检** → 通过才用释放的额度下单。
吸收方案 A/C 的教训做成**受限**：**封死涨停的票直接跳过不追**（A 的教训：追价对封板无效且追在尖上）、
用「涨停价上界」思想把不可买的状态排除掉（C 的思想）。

## 纪律（与 decision.py 同款，勿破坏）

- 本模块**纯函数 + 注入式编排**，**不做任何 IO**：不读文件、不直接调 gm、不写持仓。
  取数留 `hooks.py`（`fetch_current_snapshot`），下单经注入的 gateway 鸭子类型。
- **fail-closed**：任一量缺失/异常（现价、昨收、VWAP、盘口）⇒ 该候补**不通过**、不下单。
  宁可不买，也不在信息不全时下单。
- 本模块**不 import gm.api**（仓规：全仓只有 `gm_main.py` 能 import）。

## 参数口径

复检用「**当日累计** VWAP」（`cum_amount/cum_volume`），与决策核的 10:00 bar0 VWAP
（`decision.py:68` 的 `amount/volume`，只是 09:30~10:00 那根）**故意不同**：
复检问的是"现在这个价位相对今天所有成交划不划算"，不是"10:00 那根划不划算"。

## 量纲自检（必须保留）

`gm.current()` 的 `cum_volume` 单位**未经本机验证**（本机 `gm` 是空命名空间桩）。
若它实为「手」而 `cum_amount` 是「元」，则 `cum_amount/cum_volume` 会大 100×，
`现价 ≥ VWAP` 恒假 ⇒ 顺移**静默失效**（不报错、就是不买）。
故 `day_vwap` 做比值自检：VWAP/现价不在 `[0.5, 1.5]` 内即判**不可用** ⇒ 该票不通过。
"""
from __future__ import annotations

import math
import time

# ── 涨跌停幅度（按代码前缀分档）──
# 真源对照：`stock_hunter/modules/market_data.py:586-594`（唯一带 10/20/30cm 分档的实现，
# 但它**漏 ST 5%** 且属猎手层、不能跨包 import）⇒ 本包自备小助手。
# OSC 宇宙已在 `prescreen.full_market_meta()` 剔除 ST/退市/北交所/B股 ⇒ 实际只用到前两档；
# 仍保留第三档以备复用，**若复用务必补 ST 5% 分支**。
RATIO_MAIN = 1.10       # 主板（60x/00x）
RATIO_GEM = 1.20        # 创业板/科创板（300/301/688）
RATIO_BJ = 1.30         # 北交所（8x/9x）—— OSC 已剔除，保留以备复用

_VWAP_SANE_LO = 0.5     # VWAP/现价 合理区间下界（量纲自检用）
_VWAP_SANE_HI = 1.5
_LIMIT_EPS = 1e-6


def limit_ratio(code) -> float:
    """6 位码 → 涨停幅度比例。认不出/主板 → 1.10。"""
    c = str(code or "").split("_")[0].strip()
    if c[:3] in ("300", "301", "688"):
        return RATIO_GEM
    if c[:1] in ("4", "8", "9"):
        return RATIO_BJ
    return RATIO_MAIN


def limit_up_price(prev_close, code):
    """涨停价 = 昨收 × 幅度，**四舍五入到分**。无效输入返回 None。

    ⚠️ 用 half-up（`floor(x*100+0.5)/100`）而**不是** Python `round()`——
    后者是**银行家舍入**（round-half-to-even），`10.05*1.1=11.055` 会被舍成 `11.05` 而交易所口径是
    `11.06`。差 1 分就足以让「是否贴涨停」判错，进而误判封死/误判可买。
    """
    try:
        pc = float(prev_close)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pc) or pc <= 0:
        return None
    x = pc * limit_ratio(code)
    return math.floor(x * 100 + 0.5) / 100.0


def day_vwap(cum_amount, cum_volume):
    """当日累计 VWAP = cum_amount / cum_volume。量额缺失/非正 → None。"""
    try:
        a, v = float(cum_amount), float(cum_volume)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(a) and math.isfinite(v)) or v <= 0 or a <= 0:
        return None
    return a / v


def vwap_sane(vwap, price) -> bool:
    """VWAP 量纲自检：VWAP/现价 必须落在 [0.5, 1.5]。

    见模块 docstring「量纲自检」。**这是 fail-closed 的护栏**：量纲错了宁可不用顺移，
    也不能让「现价 ≥ VWAP」这条判据静默变成永假（那会让整个机制看起来"没生效"却不报错）。
    """
    try:
        v, p = float(vwap), float(price)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(v) and math.isfinite(p)) or v <= 0 or p <= 0:
        return False
    return _VWAP_SANE_LO <= (v / p) <= _VWAP_SANE_HI


def is_sealed(price, limit_up, ask_v) -> bool:
    """是否**涨停封死**（跳过、不追）。

    判据：现价贴涨停价 **且 卖一无量**。
    ⚠️ 盘口缺失（`ask_v is None`）时**降级为只看价位**（更保守 ⇒ 多跳过）——
    宁可漏掉一个能买的，也不去追一个可能买不进的。
    """
    try:
        p, lu = float(price), float(limit_up)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(p) and math.isfinite(lu)) or lu <= 0:
        return False
    if p < lu - _LIMIT_EPS:
        return False
    try:
        av = float(ask_v)
    except (TypeError, ValueError):
        return True                      # 盘口缺失 ⇒ 保守：算封死
    return av <= 0


def recheck_ok(price, prev_close, vwap, ask_v, limit_up) -> tuple:
    """顺移候补的**时效复检**（doc §3.2 的三条 + fail-closed）。

    通过条件（全部满足）：
      ① 现价 ≥ 昨收           —— 缺口仍未回补（信号没坏）
      ② 现价 ≥ 当日累计 VWAP  —— 现价仍在今日均价之上（没在弱势里接货）
      ③ 非涨停封死            —— 封死的票直接跳过（方案 A 的教训）

    返回 `(ok: bool, reason: str)`；reason ∈ {no_data, limit_sealed,
    below_prev_close, below_vwap, vwap_unavailable, ok}。
    """
    try:
        p = float(price)
        pc = float(prev_close)
    except (TypeError, ValueError):
        return False, "no_data"
    if not (math.isfinite(p) and math.isfinite(pc)) or p <= 0 or pc <= 0:
        return False, "no_data"
    if is_sealed(p, limit_up, ask_v):
        return False, "limit_sealed"
    if p < pc:
        return False, "below_prev_close"
    if vwap is None or not vwap_sane(vwap, p):
        return False, "vwap_unavailable"
    if p < float(vwap):
        return False, "below_vwap"
    return True, "ok"


# ════════════════════════ 候补与待补单（纯函数） ════════════════════════

def build_spares(signals, pick_codes, done_codes, bars=None) -> list:
    """从**全排序候选**里取顺移候补：signals 中 TRADE、不在 top-N 里、且今日没碰过的票。

    signals 由 `decision.evaluate()` 返回（gap 降序，**含被板块过滤的**）。
    ⚠️ 只取 `decision == "TRADE"` 的（被板块过滤的 `NO_TRADE` 不能顺移）。
    返回 [{code, gap, board, price}]，保持 gap 降序；`price` 取自 `bars[code]['close']`（10:00 收盘）。
    """
    from .decision import TRADE
    skip = set(pick_codes or ()) | set(done_codes or ())
    bars = bars or {}
    out = []
    for s in (signals or []):
        try:
            c = str(s.get("code") or "")
            if not c or c in skip or s.get("decision") != TRADE:
                continue
            b = bars.get(c) or {}
            px = float(b.get("close") or 0)
            if px <= 0:
                continue                     # 无 10:00 棒 ⇒ 无法折算股数 ⇒ fail-closed 跳过
            out.append({"code": c, "gap": s.get("gap"), "board": s.get("board", ""),
                        "price": px})
        except Exception:
            continue
    return out


def pending_from_fills(fills) -> list:
    """当日首轮 fills → **待补单列表**（非终态且仍有未成交量的买单）。

    返回 [{code, order_id, gap, price, qty_placed, filled_qty, qty_remaining, ref_price}]。
    `ref_price` = 成交均价优先、否则 10:00 收盘参考价（市价单常无 filled_price）。

    ⚠️ 2026-10-16 生产施工：`order_id` 缺失的买单**也保留**（原先直接丢弃）。
    GM 仿真终端同步下单回报里 order_id 恒为空（10-14/10-16 bridge events 无一非空），
    丢弃 ⇒ 整个替补机制在生产上**静默失效**。改为保留，撤单时由
    `gateway.cancel_order` 按 symbol 解析在途单号（解析不到 ⇒ 未确认/失败，不补单）。
    连带效果：fills json 里"已成交但 filled_qty 未回填"的票（失真前科）也会进 pending
    —— 解析时它们已不在在途委托里（`no_open_order` ⇒ 未确认，不释放不误撤），安全。
    """
    out = []
    for r in ((fills or {}).get("buys") or []):
        try:
            st = str(r.get("status") or "")
            if st in ("rejected", "expired", "cancelled", "error", "skipped"):
                continue
            qp = int(r.get("qty_placed") or 0)
            fq = int(r.get("filled_qty") or 0)
            rem = qp - fq
            if rem <= 0:
                continue
            ref = r.get("filled_price") or r.get("price") or 0
            out.append({"code": str(r.get("code") or ""), "order_id": r.get("order_id"),
                        "gap": r.get("gap"), "price": float(r.get("price") or 0),
                        "qty_placed": qp, "filled_qty": fq, "qty_remaining": rem,
                        "ref_price": float(ref or 0)})
        except Exception:
            continue
    return [p for p in out if p["code"] and p["ref_price"] > 0]


def committed_notional(fills, released_codes=()) -> dict:
    """逐 code 的**已占用名义** = 已成交 + 未终态且撤单**未确认**的冻结部分。

    `released_codes`：撤单已确认（status ∈ {5,4}）的 code —— 其冻结部分不再占用额度。
    落实 owner 决策 #4「撤单回报确认后再用释放额度」。
    """
    rel = set(released_codes or ())
    out = {}
    for r in ((fills or {}).get("buys") or []):
        try:
            c = str(r.get("code") or "")
            if not c:
                continue
            qp = int(r.get("qty_placed") or 0)
            fq = int(r.get("filled_qty") or 0)
            fp = float(r.get("filled_price") or 0)
            ref = float(r.get("price") or 0)
            filled_notional = fq * (fp or ref)
            st = str(r.get("status") or "")
            terminal_bad = st in ("rejected", "expired", "cancelled", "error")
            frozen = 0.0
            if not terminal_bad and c not in rel:
                frozen = max(0, qp - fq) * ref
            out[c] = out.get(c, 0.0) + filled_notional + frozen
        except Exception:
            continue
    # 只留**真正占用额度**的 code（0 不入表：调用方按"有没有这条"判占用，留 0 会误判）
    return {k: round(v, 2) for k, v in out.items() if v > 0}


def budget_for_round(daily_budget, single_budget, committed, cash_free, cash_headroom=0.95):
    """补单轮的可支配预算 + 单票上限。

    返回 `(cash_budget, single_cap)`：
      · `cash_budget = max(0, min(daily_remain, cash_free))`
        `daily_remain = max(0, daily_budget − Σcommitted)`
        `cash_free` 取自 `gateway.get_cash()`（**已扣在途冻结** ⇒ 天然不会超买）。
      · `single_cap = max(0, single_budget − committed.get(code, 0))`（逐票另算，见调用方）。
    """
    try:
        used = sum(float(v or 0) for v in (committed or {}).values())
        remain = max(0.0, float(daily_budget) - used)
        cash = max(0.0, float(cash_free or 0))
        return min(remain, cash), float(single_budget)
    except (TypeError, ValueError):
        return 0.0, 0.0


# ════════════════════════ 撤单（注入式，可干跑） ════════════════════════

# 撤单后「资金真的释放了」的终态：5=Canceled、4=DoneForDay。
# ⚠️ 6=PendingCancel **不算**（还没撤成、钱还冻着）；3=Filled 是竞态成交（撤单没赢）；
#    2=PartiallyFilled 只释放未成交部分。语义出处见
#    `doc/review/archive/goldminer/backlog/Phase-A_合成拒单注入验证方案.md:19-22`。
CANCEL_RELEASED = (5, 4)
CANCEL_PENDING = (6,)
CANCEL_LOST_RACE = (3,)


def cancel_and_confirm(gateway, pending, poll_rounds=3, poll_sleep=2.0, sleep_fn=None, log=print):
    """对**未成交的**买单发撤单并轮询确认。返回

        {"released": [code...],        # 撤单已确认（status∈{5,4}）⇒ 额度可用
         "filled":   [{code, qty, price}...],   # 竞态成交（撤单没赢）⇒ 按成交处理
         "unconfirmed": [code...],     # 撤单发出但没确认（含 6/2/超时）⇒ 额度**不**释放
         "failed": [code...]}          # 撤单下发失败（原语缺席/异常）

    ⚠️ **不抛异常**：撤单环节任何失败都只是"额度不释放"，绝不能因此中断主循环。
    """
    import time as _t
    sleep_fn = sleep_fn or _t.sleep
    res = {"released": [], "filled": [], "unconfirmed": [], "failed": []}
    for p in (pending or []):
        code, oid = p.get("code"), p.get("order_id")
        if not code:
            continue
        try:
            rep = gateway.cancel_order(oid, code)
        except Exception as e:
            log(f"[OSC] ⚠️ 撤单下发异常 {code} {oid}: {type(e).__name__}: {e}")
            res["failed"].append(code)
            continue
        # order_id 缺省时 gateway 会按 symbol 解析在途单（2026-10-16）⇒ 后续轮询用**解析后**的 id
        oid = (rep or {}).get("order_id") or oid
        if not (rep or {}).get("ok"):
            if (rep or {}).get("not_found"):
                # 该 code 已无在途买单（成交/已拒/已撤）：既不是"撤成了"也不是"撤单失败"，
                # 钱的去向不明 ⇒ 保守按**未确认**处理（额度不释放，但不中止本轮补单）。
                log(f"[OSC] ℹ️ {code} 无在途买单（已成交/已终态）⇒ 不按撤成处理")
                res["unconfirmed"].append(code)
            else:
                log(f"[OSC] ⚠️ 撤单未成功 {code} {oid}: {(rep or {}).get('message')}")
                res["failed"].append(code)
            continue
        # 轮询确认
        st, filled_qty, filled_px = None, 0, None
        for _ in range(int(poll_rounds)):
            try:
                rep2 = gateway.poll_order(oid, code) or {}
            except Exception:
                break
            raw = rep2.get("raw_status")
            st = raw if raw is not None else rep2.get("status")
            filled_qty = int(rep2.get("filled_qty") or 0)
            filled_px = rep2.get("filled_price")
            if st in CANCEL_RELEASED or st in CANCEL_LOST_RACE or rep2.get("status") == "filled":
                break
            try:
                sleep_fn(float(poll_sleep))
            except Exception:
                break
        if st in CANCEL_LOST_RACE or (rep and rep.get("_filled")):
            res["filled"].append({"code": code, "qty": filled_qty, "price": filled_px})
        elif st in CANCEL_RELEASED:
            res["released"].append(code)
        else:
            res["unconfirmed"].append(code)
    return res


# ════════════════════════ 一轮补单的编排 ════════════════════════

def run_backfill_exec(gateway, date, fills, pending, spares, prev_close, quotes,
                      positions=None, params=None, dry_run=False,
                      sleep_fn=None, log=print) -> dict:
    """**一轮**补单：撤单确认 → 额度重算 → 候补复检 → 顺移下单。

    参数
        fills      : 当日首轮（或上一轮）的 fills dict —— 额度账 `committed` 由它算
        pending    : `pending_from_fills(fills)` 的结果（待补单）
        spares     : `build_spares(...)` 的结果（顺移候补，gap 降序）
        prev_close : {code: 昨收}（复检"缺口未回补"与涨停价都要）
        quotes     : {code: {price, vwap, ask_v}}（hooks 取数；缺 → 该票 fail-closed 跳过）

    返回 `{cancel, committed, cash_budget, placed, skipped, released, note}`。
    **不抛**：任何异常都收窄成"这轮不补"，绝不中断主循环。
    """
    from . import executor as _ex                      # 函数级 import，避免与 executor 循环
    cfg = {**_ex.DEFAULTS, **(params or {})}
    lot = int(cfg["min_lot"])
    single = float(cfg["single_budget"])
    daily = float(cfg["daily_budget"])
    headroom = float(cfg["cash_headroom"])
    poll_rounds = int(cfg["order_poll_rounds"])
    poll_sleep = float(cfg["order_poll_sleep_sec"])
    sleep_fn = sleep_fn or time.sleep

    out = {"date": str(date), "cancel": None, "committed": {}, "cash_budget": 0.0,
           "placed": [], "skipped": [], "released": [], "note": ""}

    # ① 撤单并**确认**（未确认 ⇒ 额度不释放，见 ②）
    cres = cancel_and_confirm(gateway, pending, poll_rounds, poll_sleep, sleep_fn, log)
    out["cancel"] = cres
    out["released"] = list(cres.get("released") or [])

    # ② 额度账：撤单确认后才释放（owner 决策 #4）
    committed = committed_notional(fills, released_codes=cres.get("released"))
    out["committed"] = committed

    # ②′ 安全闸（2026-10-16 生产施工，owner 拍板）：**撤单失败 ⇒ 本轮不补单**。
    # 「撤单下发失败」意味着账户委托状态不可知（原语缺席/拒撤/解析不到单号）——
    # 此时账户里可能仍挂着有效买单，再补单就是拿"以为释放了、实际没释放"的额度超买。
    # 与 gateway.cancel_order 的防御式降级口径一致：`cancel_unavailable` ⇒ 不做补单。
    # 注意只闸 `failed`：`unconfirmed`（含 PendingCancel 钱未解冻、无在途单）**不**中止——
    # 它们只是"那笔额度不释放"，剩余额度内的补单仍允许（既有测试钉住此口径）。
    if cres.get("failed"):
        out["note"] = "cancel_failed_abort"
        log(f"[OSC] ⛔ 撤单失败 {cres['failed']} ⇒ 本轮中止补单（挂单保持不动，等下轮/15:00 拒单）")
        return out

    try:
        cash_free = float(gateway.get_cash() or 0)     # 已扣在途冻结 ⇒ 天然不超买
    except Exception:
        cash_free = 0.0
    cash_budget, _single = budget_for_round(daily, single, committed, cash_free, headroom)
    out["cash_budget"] = round(cash_budget, 2)
    if cash_budget < lot * 1:                          # 连一手都买不起 ⇒ 别白跑
        out["note"] = "budget_exhausted"
        return out

    # ③ 候补**时效复检**（封死跳过、跌破昨收/均价跳过、量纲异常跳过）
    cands = []
    for s in (spares or []):
        code = str(s.get("code") or "")
        if not code:
            continue
        q = (quotes or {}).get(code) or {}
        pc = (prev_close or {}).get(code)
        lu = limit_up_price(pc, code)
        ok, why = recheck_ok(q.get("price"), pc, q.get("vwap"), q.get("ask_v"), lu)
        if not ok:
            out["skipped"].append({"code": code, "reason": why})
            continue
        cands.append({"code": code, "gap": s.get("gap"),
                      # 入场参考价 = **复检时的现价**（不是 10:00 收盘）：顺移本就是"现在买"，
                      # 用 10:00 价折算股数会低估占用。这也是方案 B「隐性追价」的代价所在，
                      # 回测对齐（exp26）要按同一口径算。
                      "price": float(q.get("price") or 0)})
    if not cands:
        out["note"] = "no_eligible_spare"
        return out

    # ④ 下单（复用首轮的额度折算与下单原语，保持单一真源）
    positions = positions if positions is not None else (gateway.get_positions() or {})
    try:
        positions = {_ex.code_of(k) or str(k): v for k, v in positions.items()}
    except Exception:
        positions = {}
    plan = _ex.build_buy_plan(cands, positions, cash_budget, single,
                              min_lot=lot, cash_headroom=headroom,
                              code_committed=committed)
    for b in plan:
        if b["defer_reason"] or b["qty_place"] <= 0:
            out["skipped"].append({"code": b["code"],
                                   "reason": b["defer_reason"] or "qty_zero"})
            continue
        if dry_run:
            out["placed"].append({"code": b["code"], "qty": b["qty_place"],
                                  "price": b["price"], "status": "dry",
                                  "order_id": None, "filled_qty": 0})
            continue
        rep = _ex._place_and_poll(gateway, b["code"], "BUY", b["qty_place"], b["price"],
                                  poll_rounds, poll_sleep, sleep_fn, log)
        out["placed"].append({
            "code": b["code"], "qty": b["qty_place"], "price": b["price"],
            "order_id": rep.get("order_id"), "status": rep.get("status"),
            "filled_qty": int(rep.get("filled_qty") or 0),
            "filled_price": rep.get("filled_price"),
            "qty_remaining": max(0, b["qty_place"] - int(rep.get("filled_qty") or 0)),
            "message": rep.get("message") or ""})
        log(f"[OSC] 顺移 BUY {b['code']} {b['qty_place']}股@{b['price']:.3f} → "
            f"{rep.get('status')}")
    if not out["placed"]:
        out["note"] = out["note"] or "nothing_placed"
    return out
