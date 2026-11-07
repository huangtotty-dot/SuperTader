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
            if st in ("rejected", "expired", "cancelled", "skipped"):
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


def _execution_limit(rec) -> float | None:
    """只读真实委托限价；参考价和旧 exchange_limit_up 都不是执行限价。"""
    try:
        value = float((rec or {}).get("execution_limit_price") or 0)
        if math.isfinite(value) and value > 0:
            return value
        ceiling = float((rec or {}).get("execution_ceiling_price") or 0)
        if math.isfinite(ceiling) and ceiling > 0 \
                and (rec or {}).get("budget_price_kind") != "exchange_limit_up":
            return ceiling
    except (TypeError, ValueError):
        pass
    return None


def _known_filled_notional(rec):
    """返回真实成交额；量有价缺失时返回 None，绝不拿参考价/限价冒充成交。"""
    qty = max(0, int((rec or {}).get("filled_qty") or 0))
    if qty <= 0:
        return 0.0
    explicit = (rec or {}).get("filled_notional")
    if explicit is not None:
        try:
            value = float(explicit)
            if math.isfinite(value) and value > 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        px = float((rec or {}).get("filled_price") or 0)
        if math.isfinite(px) and px > 0:
            return qty * px
    except (TypeError, ValueError):
        pass
    return None


def filled_notional(rec) -> float:
    """一笔买单的真实成交额；缺真实 VWAP 时返回 0，由 committed 另行保守计提。"""
    try:
        value = _known_filled_notional(rec)
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def actual_buy_notional(fills) -> float:
    """当日所有已知真实 BUY fill 的累计成交额（不含缺价成交与未成交预留）。"""
    return sum(filled_notional(r) for r in ((fills or {}).get("buys") or []))


def record_reserved_notional(rec, released=False) -> float:
    """未成交量 × 实际限价；旧在途缺真实限价直接抛错，调用方必须 fail-closed。"""
    qty_placed = max(0, int((rec or {}).get("qty_placed") or 0))
    filled_qty = min(qty_placed, max(0, int((rec or {}).get("filled_qty") or 0)))
    terminal = str((rec or {}).get("status") or "") in (
        "rejected", "expired", "cancelled", "skipped")
    remaining = max(0, qty_placed - filled_qty)
    if terminal or released or remaining <= 0:
        return 0.0
    limit = _execution_limit(rec)
    if limit is None:
        raise ValueError("missing_execution_limit:%s:%s" % (
            (rec or {}).get("code"), (rec or {}).get("order_id")))
    return remaining * limit


def reserved_notional(fills) -> dict:
    """逐 code 汇总仍在途的限价预留，不含任何已成交数量。"""
    out = {}
    for rec in ((fills or {}).get("buys") or []):
        code = str((rec or {}).get("code") or "")
        if not code:
            continue
        value = record_reserved_notional(rec)
        out[code] = out.get(code, 0.0) + value
    return {k: v for k, v in out.items() if v > 0}


def record_committed_notional(rec, released=False) -> float:
    """真实成交（缺价时按实际限价保守计提）+ 未成交实际限价预留。"""
    filled_qty = max(0, int((rec or {}).get("filled_qty") or 0))
    known = _known_filled_notional(rec)
    if known is None:
        limit = _execution_limit(rec)
        if limit is None:
            raise ValueError("missing_execution_limit_for_priceless_fill:%s:%s" % (
                (rec or {}).get("code"), (rec or {}).get("order_id")))
        known = filled_qty * limit
    return float(known or 0.0) + record_reserved_notional(rec, released=released)


def committed_notional(fills, released_codes=()) -> dict:
    """逐 code 的额度占用，全程保留原始精度；无法证明限价时抛出并闭锁。"""
    rel = set(released_codes or ())
    out = {}
    for rec in ((fills or {}).get("buys") or []):
        code = str((rec or {}).get("code") or "")
        if not code:
            continue
        value = record_committed_notional(rec, released=code in rel)
        out[code] = out.get(code, 0.0) + value
    return {k: v for k, v in out.items() if v > 0}


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

# 只有 5=Canceled 是“撤单确认释放”。4/8=rejected、12=expired 是其它终态；
# 6=PendingCancel 永远在途，3=Filled 是撤单竞态成交。
CANCEL_RELEASED = (5,)
CANCEL_PENDING = (6,)
CANCEL_LOST_RACE = (3,)
CANCEL_TERMINAL = (4, 5, 8, 12)


def _raw_order_status(rep):
    raw = (rep or {}).get("raw_status")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        pass
    return {"rejected": 8, "cancelled": 5, "expired": 12,
            "pending_cancel": 6, "filled": 3}.get((rep or {}).get("status"))


def cancel_and_confirm(gateway, pending, poll_rounds=3, poll_sleep=2.0, sleep_fn=None, log=print):
    """逐委托撤单并确认；released/terminal 均携带 order_id，绝不按 code 批量结算。"""
    import time as _t
    sleep_fn = sleep_fn or _t.sleep
    res = {"released": [], "filled": [], "unconfirmed": [], "failed": [],
           "terminal_orders": [], "released_orders": []}
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
        oid = (rep or {}).get("order_id") or oid
        if not (rep or {}).get("ok"):
            if (rep or {}).get("not_found"):
                log(f"[OSC] ℹ️ {code} 无在途买单（已成交/已终态）⇒ 不按撤成处理")
                res["unconfirmed"].append(code)
            else:
                log(f"[OSC] ⚠️ 撤单未成功 {code} {oid}: {(rep or {}).get('message')}")
                res["failed"].append(code)
            continue
        st, filled_qty, filled_px = None, 0, None
        last = {}
        for _ in range(int(poll_rounds)):
            try:
                last = gateway.poll_order(oid, code) or {}
            except Exception:
                break
            st = _raw_order_status(last)
            filled_qty = int(last.get("filled_qty") or 0)
            filled_px = last.get("filled_price")
            if st in CANCEL_TERMINAL or st in CANCEL_LOST_RACE:
                break
            try:
                sleep_fn(float(poll_sleep))
            except Exception:
                break
        detail = {"code": code, "order_id": oid, "raw_status": st,
                  "qty_placed": int(p.get("qty_placed") or p.get("qty") or 0),
                  "filled_qty": filled_qty, "filled_price": filled_px,
                  "message": last.get("message") or ""}
        if st in CANCEL_LOST_RACE or (rep and rep.get("_filled")):
            res["filled"].append({"code": code, "qty": filled_qty, "price": filled_px,
                                  "order_id": oid, "raw_status": 3})
        elif st in CANCEL_TERMINAL:
            detail["status"] = {4: "rejected", 8: "rejected",
                                5: "cancelled", 12: "expired"}[st]
            res["terminal_orders"].append(detail)
            if st in CANCEL_RELEASED:
                res["released"].append(code)
                res["released_orders"].append(dict(detail))
        else:
            res["unconfirmed"].append(code)
    return res


# ════════════════════════ 一轮补单的编排 ════════════════════════

def run_backfill_exec(gateway, date, fills, pending, spares, prev_close, quotes,
                      positions=None, params=None, dry_run=False,
                      sleep_fn=None, log=print, book_dir=None) -> dict:
    """一轮补单：逐委托撤单/终态结算后，才以原数量和真实限价安排新 BUY。"""
    import copy
    from . import executor as _ex                      # 函数级 import，避免与 executor 循环
    cfg = {**_ex.DEFAULTS, **(params or {})}
    lot = int(cfg["min_lot"])
    single = float(cfg["single_budget"])
    daily = float(cfg["daily_budget"])
    headroom = float(cfg["cash_headroom"])
    poll_rounds = int(cfg["order_poll_rounds"])
    poll_sleep = float(cfg["order_poll_sleep_sec"])
    sleep_fn = sleep_fn or time.sleep

    out = {"date": str(date), "cancel": None, "committed": {}, "reserved": {},
           "cash_budget": 0.0, "placed": [], "skipped": [], "released": [], "note": "",
           "budget_bound_breaches": []}

    cres = cancel_and_confirm(gateway, pending, poll_rounds, poll_sleep, sleep_fn, log)
    out["cancel"] = cres
    out["released"] = list(cres.get("released") or [])

    # 独立预算视图按 order_id + raw_status 逐单更新；同 code 的其他委托绝不受牵连。
    budget_view = copy.deepcopy(fills or {"buys": []})
    budget_view.setdefault("buys", [])
    match_issues = []

    def _match(code, oid):
        candidates = [r for r in budget_view["buys"]
                      if str(r.get("code") or "") == str(code)
                      and str(r.get("status") or "") not in _ex._TERMINAL_ORDER_STATUS]
        if oid:
            for rec in reversed(candidates):
                if str(rec.get("order_id") or rec.get("id") or "") == str(oid):
                    return rec
            blank = [r for r in candidates if not str(r.get("order_id") or r.get("id") or "")]
            return blank[0] if len(blank) == 1 else None
        return candidates[0] if len(candidates) == 1 else None

    for terminal in (cres.get("terminal_orders") or []):
        code, oid = terminal.get("code"), str(terminal.get("order_id") or "")
        hit = _match(code, oid)
        if hit is None:
            match_issues.append({"kind": "terminal_order_unmatched", "code": code,
                                 "order_id": oid, "raw_status": terminal.get("raw_status")})
            continue
        _ex._settle_buy_budget_record(hit, {
            "order_id": oid or hit.get("order_id"), "status": terminal.get("status"),
            "raw_status": terminal.get("raw_status"),
            "filled_qty": int(terminal.get("filled_qty") or 0),
            "filled_price": terminal.get("filled_price"),
            "message": terminal.get("message") or ""})
    for race in (cres.get("filled") or []):
        code, oid = race.get("code"), str(race.get("order_id") or "")
        hit = _match(code, oid)
        if hit is None:
            match_issues.append({"kind": "race_order_unmatched", "code": code, "order_id": oid})
            continue
        _ex._settle_buy_budget_record(hit, {
            "order_id": oid or hit.get("order_id"), "status": "filled", "raw_status": 3,
            "filled_qty": int(race.get("qty") or 0), "filled_price": race.get("price"),
            "message": "撤单竞态成交"})

    bound_issues = match_issues + _ex._budget_bound_issues(budget_view, prev_close)
    out["budget_bound_breaches"] = list(bound_issues)
    try:
        committed = committed_notional(budget_view)
        out["committed"] = committed
        out["reserved"] = reserved_notional(budget_view)
    except (TypeError, ValueError) as exc:
        bound_issues.append({"kind": "missing_execution_limit", "error": str(exc)})
        out["budget_bound_breaches"] = list(bound_issues)
        committed = {}

    if cres.get("failed"):
        out["note"] = "cancel_failed_abort"
        log(f"[OSC] ⛔ 撤单失败 {cres['failed']} ⇒ 本轮中止补单（挂单保持不动，等下轮/15:00 拒单）")
        return out
    if bound_issues:
        out["note"] = "budget_bound_breach"
        log(f"[OSC] ⛔ 在途/成交上界不可证明 ⇒ 本轮禁止新 BUY: {bound_issues}")
        return out

    try:
        cash_free = float(gateway.get_cash() or 0)
    except Exception:
        cash_free = 0.0
    cash_budget, _single = budget_for_round(daily, single, committed, cash_free, headroom)
    out["cash_budget"] = round(cash_budget, 2)
    if cash_budget < lot * 1:
        out["note"] = "budget_exhausted"
        return out

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
                      "price": float(q.get("price") or 0)})
    if not cands:
        out["note"] = "no_eligible_spare"
        return out

    positions = positions if positions is not None else (gateway.get_positions() or {})
    try:
        positions = {_ex.code_of(k) or str(k): v for k, v in positions.items()}
    except Exception:
        positions = {}
    plan = _ex.build_buy_plan(cands, positions, cash_budget, single,
                              min_lot=lot, cash_headroom=headroom,
                              code_committed=committed)
    records, issues = _ex._execute_buy_plan_with_budget(
        gateway, plan, prev_close, budget_view, cash_free, daily, single, cfg,
        dry_run=dry_run, sleep_fn=sleep_fn, log=log, book_dir=book_dir, date=date)
    out["budget_bound_breaches"] = list(issues)
    for rec in records:
        if int(rec.get("qty_placed") or 0) <= 0:
            out["skipped"].append({"code": rec.get("code"),
                                   "reason": rec.get("defer_reason") or "qty_zero"})
            continue
        out["placed"].append(dict(rec, qty=int(rec["qty_placed"])))
    if issues:
        out["note"] = "budget_bound_breach"
    if not out["placed"]:
        out["note"] = out["note"] or "nothing_placed"
    return out
