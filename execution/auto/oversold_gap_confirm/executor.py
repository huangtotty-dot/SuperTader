# -*- coding: utf-8 -*-
r"""executor.py — 超跌跳空确认 · 执行核（2026-10-10 施工；2026-10-12 归拢进策略包）。

信号（冻结，见同包 decision.py）：日线超跌 `昨收<MA20` + 盘中 10:00 跳空高开守住
（10:00 棒 low≥昨收 且 close>昨收 且 close>VWAP）⇒ 10:00 收盘买入，按 gap 降序取 top-N。

**买侧**：候选(盘前超跌)+10:00 棒 → 决策核 → top-N → 下单。
**卖侧（2026-10-10 接线，exp19 owner 拍板 SL5只深）**：持仓账本 `positions.json` 记
entry_date/entry_price/dev20/gap/board；每日收盘检查——深超跌(dev20<−7%)挂 −5% close 止损、
浅/中不挂、满 5 个交易日到期平仓（死拿5天 horizon）。卖出侧「次日早盘冲高卖」是下一步。

（历史：原与 S1 共用掘金模拟盘账户，故本执行器**不带独立净值链**；S1 已于 2026-10-12 整链删除，
账户现由本策略独占。）只落 fills/exec_report；每日名义总额度由 `osc_daily_budget` 封顶。

**不 import gm.api**——券商/仿真通道全部经注入的 Gateway 鸭子类型（与已退役的 s1_executor 同款，
gm_main 直接复用 `_OscGmGateway`），因此 gm_main / 干跑脚本 / 单测共用同一套执行逻辑。

Gateway 接口（鸭子类型，全部必需，poll_order 可选）::

    get_cash() -> float                       # 可用现金（元）
    get_positions() -> {code: {"qty": int, "available": int, "cost": float}}
    place_order(code, side, qty, price) -> dict   # side∈{"BUY","SELL"}；price 仅留痕参考
        {"order_id","status"∈{submitted,filled,partial,rejected,error},"filled_qty","filled_price","message"}
    poll_order(order_id, code) -> dict        # 同上结构；可选，缺失则不轮询

执行语义（对齐 exp 口径，刻意保持简单）：
  ① 决策核 evaluate 出 picks（已含板块过滤 + gap 排序 + top-N + fail-closed min_pool）；
  ② 每票规模 = min(single_budget, 剩余现金×0.95) / 10:00收盘价，取整到 100 股；
  ③ 逐票现金截断；T+1（当日已持 available=0）与 10:00 涨跌停钳 由调用方/gateway 兜底，
     本模块对 available=0 的票记 deferred 不下单；
  ④ 拒单不中断后续票；终态全部落 fills。

产物（幂等覆盖）：
  t_io/state/weak_strong_book/fills_{date}.json   — 逐单留痕（真源）
  t_io/state/weak_strong_book/exec_report_{date}.json — 当日执行摘要
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN

# 仓库根：本文件在 execution/auto/oversold_gap_confirm/ ⇒ 上溯 4 层
_ROOT = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


try:
    from core.cost_model import FEE_BUY_STOCK as _FEE_BUY, FEE_SELL_STOCK as _FEE_SELL
    _FEE_SRC = "core/cost_model.py"
except Exception:                                    # pragma: no cover - 兜底
    # ⚠️ 宿主 `gm_main.py` 负责把仓根 append 进 sys.path（见该文件顶部注释）；真拿不到就用同值常量。
    _FEE_BUY, _FEE_SELL = 0.0000954, 0.0005954
    _FEE_SRC = "fallback_constants(=core/cost_model 同值)"

# 控制台着色（2026-10-15）。默认关；宿主 gm_main 探测到控制台支持 ANSI 后调 `style.enable(True)`。
# 只影响显示：未开启时 `style.c()` 原样返回字符串。写日志文件那份由 ops_guard._TeeStream 剥码。
from . import style as _st                          # noqa: E402

# ⚠️ 目录名沿用历史的 `weak_strong_book`（**刻意不改**）：它是**在线账本**（positions.json），
#    改名会让在途持仓与卖侧失联。要改名须与「部署新代码」同刻做数据迁移。
BOOK_DIR_DEFAULT = os.path.join(_ROOT, "t_io", "state", "weak_strong_book")

DEFAULTS = {
    "top_n": 4,                     # 每日按 gap 取最强前 N（exp9 Sharpe 顶点）
    "single_budget": 100000.0,      # 单票名义上限（元，对齐 OGR 10 万/腿）
    "daily_budget": 200000.0,       # 每日名义总额度（元，首轮+补单共享）
    "cash_headroom": 0.95,          # 单票占用现金封顶比例
    "min_lot": 100,
    "order_poll_rounds": 3,
    "order_poll_sleep_sec": 2.0,
    "hold_days": 5,                 # 卖侧：持有满 N 交易日到期平仓（exp6/7 死拿5天 horizon）
    "stop_pct": 0.05,               # 卖侧：深超跌 close 止损线（−5%）
    "stop_deep_dev20": -0.07,       # 卖侧：dev20 < 此值才挂止损（SL5只深，owner 拍板）
    "pop_target_pct": 0.03,         # 卖侧：次日早盘冲高卖目标（+3%，exp11 说可 1~10% 调）
    "sell_enabled": True,           # 卖侧总闸（只动掘金模拟盘，随 paper_enabled 一起开关）
}


def code_of(symbol) -> str:
    """任意符号写法 → 6 位裸码。无法解析返回 ""。"""
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    for sep in (".", "_"):
        if sep in s:
            parts = [p for p in s.split(sep) if p]
            for p in parts:
                if p.isdigit() and len(p) == 6:
                    return p
            return ""
    return s if (s.isdigit() and len(s) == 6) else ""


def gm_symbol_of(code: str) -> str:
    return ("SHSE." if code[:1] in "569" else "SZSE.") + code


# ════════════════════════ 决策（委托同包 decision.py） ════════════════════════

def evaluate_picks(prev_close: dict, ma20: dict, bars: dict,
                   board: dict | None = None, allowed_boards=None,
                   top_n: int = DEFAULTS["top_n"]) -> list:
    """决策核入口：返回 picks 列表（[{code,gap,board,...}]，gap 降序、板块已过滤、top-N 已取）。"""
    from . import decision as _dec                        # 同包纯函数核
    r = _dec.evaluate(prev_close, ma20, bars, board=board,
                      allowed_boards=allowed_boards, top_n=top_n)
    return list(r["picks"])


def evaluate_full(prev_close: dict, ma20: dict, bars: dict,
                  board: dict | None = None, allowed_boards=None,
                  top_n: int = DEFAULTS["top_n"]) -> dict:
    """决策核入口（**整字典**）：返回 `{pool_n, signals, picks, rejected_board, n_tradable}`。

    与 `evaluate_picks` 的差别只有一处：**保留 `signals`（全排序候选）**。
    买不进替补机制靠它取"下一名候选"——`evaluate_picks` 把 signals 丢了，
    所以顺移此前无米下锅（2026-10-14）。
    """
    from . import decision as _dec
    return _dec.evaluate(prev_close, ma20, bars, board=board,
                         allowed_boards=allowed_boards, top_n=top_n)


# ════════════════════════ 计划构建（纯函数，可单测） ════════════════════════

def _dec(value, default="0") -> Decimal:
    """十进制预算真源：字符串进 Decimal，NaN/inf/非法值按 default。"""
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else Decimal(default)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _floor_lot(cap, price, lot) -> int:
    """金额/参考价/整数手全部向下取整；绝不使用 epsilon 松一格。"""
    px, lot_d = _dec(price), Decimal(int(lot))
    if px <= 0 or lot_d <= 0:
        return 0
    lots = (_dec(cap) / px / lot_d).to_integral_value(rounding=ROUND_DOWN)
    return max(0, int(lots * lot_d))


def execution_limit_price(reference_price) -> float | None:
    """BUY 实际限价 = 策略参考价向下到分；它不是涨停价，也不用于重算数量。"""
    px = _dec(reference_price)
    if px <= 0:
        return None
    return float((px * 100).to_integral_value(rounding=ROUND_DOWN) / 100)


def build_buy_plan(picks: list, positions: dict, cash_budget: float,
                   single_budget: float, min_lot: int = 100,
                   cash_headroom: float = 0.95, code_committed: dict | None = None) -> list:
    """按参考价和十进制金额构建原始数量；执行阶段不得再动态缩手数。

    `code_committed` 仍先扣单票已占用额。现金在票间顺序分配，数量按整数手向下；
    后续若日额度/现金/单票余量不足，执行器整笔跳过，不把原委托缩成另一笔委托。
    """
    plan, cash_left = [], _dec(cash_budget)
    _committed = code_committed or {}
    headroom = _dec(cash_headroom, "0.95")
    for p in picks:
        code = p["code"]
        px = _dec(p.get("price") or p.get("close") or 0)
        gap = p.get("gap")
        rec = {"code": code, "gap": gap, "price": float(px), "qty_target": 0,
               "qty_place": 0, "truncated": False, "defer_reason": None,
               "cash_left": None}
        held_qty = max(0, int((positions.get(code) or {}).get("qty") or 0))
        avail = max(0, int((positions.get(code) or {}).get("available") or 0))
        if px <= 0:
            rec["defer_reason"] = "no_price(10:00收盘缺失)"
            plan.append(rec)
            continue
        if held_qty > 0 and avail <= 0:
            rec["defer_reason"] = "t1_locked(当日已持无可卖,加仓暂缓)"
            plan.append(rec)
            continue
        used = max(Decimal(0), _dec(_committed.get(code) or 0))
        single_left = max(Decimal(0), _dec(single_budget) - used)
        cap = min(single_left, cash_left * headroom)
        qty_target = _floor_lot(cap, px, min_lot)
        rec["qty_target"] = qty_target
        afford = _floor_lot(cash_left, px, min_lot)
        qty_place = min(qty_target, afford)
        if qty_place < qty_target and qty_target >= min_lot:
            rec["truncated"] = True
        rec["qty_place"] = qty_place
        if qty_place < min_lot:
            rec["defer_reason"] = ("cash_insufficient(现金不足截断至0)"
                                   if qty_target >= min_lot else "below_min_lot(不足一手)")
        else:
            cash_left -= Decimal(qty_place) * px
        rec["cash_left"] = float(cash_left.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        plan.append(rec)
    return plan


# ════════════════════════ 下单与轮询 ════════════════════════

_TERMINAL_BAD = {"rejected", "expired", "cancelled"}


def _place_and_poll(gateway, code, side, qty, price, poll_rounds, poll_sleep, sleep_fn, log,
                    execution_limit=None, on_ack=None):
    """下单后立即持久化认领信息，再轮询；BUY 限价缺失时绝不回退市价。"""
    try:
        if side == "BUY" and execution_limit is not None:
            rep = gateway.place_order(code, side, qty, price,
                                      execution_limit_price=execution_limit)
        else:
            rep = gateway.place_order(code, side, qty, price)
    except Exception as e:
        # 异常不等于柜台未接单：结果未知时只能保留预留，等待恢复核验。
        failure = {"order_id": None, "status": "unresolved", "filled_qty": 0,
                   "filled_price": None,
                   "message": f"place_order异常:{type(e).__name__}:{e}"}
        if on_ack is not None:
            try:
                on_ack(failure)
            except Exception as persist_error:
                failure["intent_persist_error"] = str(persist_error)
        return failure
    rep = {"order_id": None, "status": "submitted", "filled_qty": 0,
           "filled_price": None, "message": "", **(rep or {})}
    if on_ack is not None:
        try:
            on_ack(rep)
        except Exception as e:
            # 柜台已经接单但 ID/状态没可靠落盘：标记未知并闭锁，重启继续核验。
            rep["status"] = "unresolved"
            rep["intent_persist_error"] = str(e)
            return rep
    if rep["status"] in ("submitted", "partial") and rep.get("order_id") \
            and hasattr(gateway, "poll_order") and poll_rounds > 0:
        for _ in range(int(poll_rounds)):
            if rep["status"] not in ("submitted", "partial"):
                break
            try:
                sleep_fn(float(poll_sleep))
                rep2 = gateway.poll_order(rep["order_id"], code)
            except Exception as e:
                log(f"[WS] poll_order {code} 异常（结果未知，保留预留）: {e}")
                rep["status"] = "unresolved"
                rep["message"] = "%s poll_order异常:%s:%s" % (
                    rep.get("message") or "", type(e).__name__, e)
                break
            if rep2:
                rep = {**rep, **rep2}
    return rep


# ════════════════════════ 买侧 · 成交上界预留与动态额度（W45 P0） ════════════════════════

_TERMINAL_ORDER_STATUS = {"rejected", "expired", "cancelled"}


def _positive_price(value):
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    return px if math.isfinite(px) and px > 0 else None


def _limit_of_record(rec):
    """读取真实委托限价；旧涨停预留价绝不升格为新限价。"""
    limit = _positive_price((rec or {}).get("execution_limit_price"))
    if limit is not None:
        return limit
    ceiling = _positive_price((rec or {}).get("execution_ceiling_price"))
    if ceiling is not None and (rec or {}).get("budget_price_kind") != "exchange_limit_up":
        return ceiling
    return None


def _settle_buy_budget_record(rec, rep=None):
    """按真实价量结算；缺 VWAP 的成交仅以实际限价保守占用。"""
    if rep is not None:
        raw = rep.get("raw_status")
        status = rep.get("status") or "error"
        try:
            status = {4: "rejected", 8: "rejected", 5: "cancelled",
                      12: "expired", 6: "pending_cancel"}.get(int(raw), status) \
                if raw is not None else status
        except (TypeError, ValueError):
            pass
        rec["status"] = status
        if rep.get("order_id"):
            rec["order_id"] = rep.get("order_id")
        rec["message"] = rep.get("message") or ""
        if raw is not None:
            rec["raw_status"] = raw
        # 同步/轮询若带回真实费用，必须随真实价量保留；后续缺费用轮询不清空旧值。
        if rep.get("fee_actual") is not None:
            rec["fee_actual"] = rep.get("fee_actual")
        elif rep.get("fee") is not None:
            rec["fee_actual"] = rep.get("fee")
        if rep.get("fee") is not None:
            rec["fee"] = rep.get("fee")
        reported_qty = max(0, int(rep.get("filled_qty") or 0))
        rec["filled_qty"] = max(int(rec.get("filled_qty") or 0), reported_qty)
        reported_px = _positive_price(rep.get("filled_price"))
        if reported_px is not None:
            rec["filled_price"] = reported_px
            rec["filled_notional"] = int(rec["filled_qty"]) * reported_px
        elif int(rec["filled_qty"]) > 0 and not _positive_price(rec.get("filled_price")):
            rec["filled_notional"] = None
    placed = max(0, int(rec.get("qty_placed") or 0))
    filled = min(placed, max(0, int(rec.get("filled_qty") or 0)))
    rec["filled_qty"] = filled
    remaining = max(0, placed - filled)
    terminal = str(rec.get("status") or "") in _TERMINAL_ORDER_STATUS
    rec["qty_remaining"] = 0 if terminal else remaining
    limit = _limit_of_record(rec)
    if limit is not None:
        rec["execution_limit_price"] = limit
        rec["execution_ceiling_price"] = limit  # 兼容别名，语义仍是真实限价
    rec["reserved_notional"] = (0.0 if terminal or limit is None
                                else float(Decimal(remaining) * _dec(limit)))
    actual_px = _positive_price(rec.get("filled_price"))
    if actual_px is not None and limit is not None and _dec(actual_px) > _dec(limit):
        rec["budget_bound_breach"] = {
            "kind": "fill_above_execution_limit", "code": rec.get("code"),
            "order_id": rec.get("order_id"), "filled_price": actual_px,
            "execution_limit_price": limit, "execution_ceiling_price": limit,
            "source": "sync_or_poll"}
    return rec


def _budget_bound_issues(fills, prev_close=None):
    """验证在途单的真实限价与成交上界；旧涨停上界不再兜底。"""
    issues = []
    for rec in ((fills or {}).get("buys") or []):
        code = str(rec.get("code") or "")
        placed = max(0, int(rec.get("qty_placed") or 0))
        filled = min(placed, max(0, int(rec.get("filled_qty") or 0)))
        status = str(rec.get("status") or "")
        remaining = max(0, placed - filled)
        limit = _limit_of_record(rec)
        active = remaining > 0 and status not in _TERMINAL_ORDER_STATUS and status != "skipped"
        if (active or (filled > 0 and not _positive_price(rec.get("filled_price")))) \
                and limit is None:
            issues.append({"kind": "missing_execution_limit", "code": code,
                           "order_id": rec.get("order_id")})
        elif active:
            rec["execution_limit_price"] = limit
            rec["execution_ceiling_price"] = limit
            rec["reserved_notional"] = float(Decimal(remaining) * _dec(limit))
        actual_px = _positive_price(rec.get("filled_price"))
        if actual_px is not None and limit is not None and _dec(actual_px) > _dec(limit):
            issues.append({"kind": "fill_above_execution_limit", "code": code,
                           "order_id": rec.get("order_id"), "filled_price": actual_px,
                           "execution_limit_price": limit})
    return issues


def _intent_to_fill(row):
    limit = _positive_price(row.get("execution_limit_price"))
    qty = max(0, int(row.get("qty") or 0))
    rec = {"code": str(row.get("code") or ""), "side": "BUY",
           "symbol": gm_symbol_of(str(row.get("code") or "")),
           "intent_id": str(row.get("intent_id") or ""),
           "gap": row.get("gap"), "price": row.get("reference_price"),
           "qty_target": qty, "qty_placed": qty, "truncated": False,
           "defer_reason": None, "status": row.get("status") or "reserved",
           "filled_qty": max(0, int(row.get("filled_qty") or 0)),
           "filled_price": row.get("filled_price"),
           "filled_notional": row.get("filled_notional"),
           "reserved_notional": row.get("reserved_notional"),
           "order_id": row.get("order_id"), "raw_status": row.get("raw_status"),
           "qty_remaining": max(0, int(row.get("qty_remaining") or 0)),
           "message": row.get("message") or "",
           "execution_limit_price": limit, "execution_ceiling_price": limit,
           "budget_price_kind": "reference_limit"}
    for key in ("budget_bound_breach", "fee_actual", "fee"):
        if row.get(key) is not None:
            rec[key] = row.get(key)
    return _settle_buy_budget_record(rec)


def _merge_recovered_buys(view, recovered):
    """恢复记录并入 fills 预算视图，覆盖崩溃时 fills 尚未落盘的窗口。"""
    rows = view.setdefault("buys", [])
    for saved in recovered:
        iid = str(saved.get("intent_id") or "")
        oid = str(saved.get("order_id") or "")
        hit = None
        for idx, old in enumerate(rows):
            if iid and str(old.get("intent_id") or "") == iid:
                hit = idx
                break
            if oid and str(old.get("order_id") or "") == oid:
                hit = idx
                break
        if hit is None:
            rows.append(saved)
        else:
            # 持久化意图与 fills 冲突时保留较新的真实价量字段，但身份仍按 intent/order。
            rows[hit] = {**rows[hit], **saved}


def recover_order_intents(gateway, date, book_dir, log=print):
    """重启恢复：有 ID 先逐单 poll；无 ID/查不到/写回失败都禁止后续新 BUY。"""
    from . import order_intents as _oi
    out = {"fills": {"date": str(date), "buys": []}, "issues": [], "blocked": False}
    try:
        rows = _oi.load(book_dir, date, strict=True)
    except Exception as e:
        out["issues"].append({"kind": "order_intent_read_failed", "error": str(e)})
        out["blocked"] = True
        return out
    dirty = False
    for row in rows:
        rec = _intent_to_fill(row)
        status = str(rec.get("status") or "")
        active = status not in _TERMINAL_ORDER_STATUS and int(rec.get("qty_remaining") or 0) > 0
        if active:
            oid = str(rec.get("order_id") or "")
            if not oid:
                out["issues"].append({"kind": "recovery_order_id_missing",
                                      "code": rec.get("code"), "intent_id": rec.get("intent_id")})
            elif not hasattr(gateway, "poll_order"):
                out["issues"].append({"kind": "recovery_poll_unavailable",
                                      "code": rec.get("code"), "order_id": oid})
            else:
                try:
                    rep = gateway.poll_order(oid, rec.get("code")) or {}
                    if str(rep.get("message") or "") == "order_not_found":
                        raise LookupError("order_not_found")
                    _settle_buy_budget_record(rec, rep)
                    if str(rec.get("status") or "") in ("unresolved", "unknown", "error"):
                        raise RuntimeError("recovery_status_unresolved")
                    row.update({"status": rec.get("status"),
                                "filled_qty": rec.get("filled_qty"),
                                "filled_price": rec.get("filled_price"),
                                "filled_notional": rec.get("filled_notional"),
                                "qty_remaining": rec.get("qty_remaining"),
                                "reserved_notional": rec.get("reserved_notional"),
                                "raw_status": rec.get("raw_status"),
                                "fee_actual": rec.get("fee_actual"),
                                "fee": rec.get("fee"),
                                "message": rec.get("message")})
                    if rec.get("budget_bound_breach"):
                        row["budget_bound_breach"] = rec["budget_bound_breach"]
                    dirty = True
                except Exception as e:
                    out["issues"].append({"kind": "recovery_order_unresolved",
                                          "code": rec.get("code"), "order_id": oid,
                                          "error": "%s:%s" % (type(e).__name__, e)})
        if rec.get("budget_bound_breach"):
            out["issues"].append(rec["budget_bound_breach"])
        out["fills"]["buys"].append(rec)
    out["issues"].extend(_budget_bound_issues(out["fills"]))
    if dirty:
        try:
            _oi.write_all(book_dir, date, rows)
        except Exception as e:
            out["issues"].append({"kind": "order_intent_recovery_persist_failed",
                                  "error": str(e)})
    out["blocked"] = bool(out["issues"])
    if out["blocked"]:
        try:
            log(f"[WS] ⛔ 委托意图恢复未闭环，禁止新 BUY: {out['issues']}")
        except Exception:
            pass
    return out


def sync_order_intent_from_record(book_dir, date, rec):
    """异步 fill/terminal 同步既有意图；找不到证明时抛错，不事后伪造预留。"""
    from . import order_intents as _oi
    return _oi.upsert_fill_record(book_dir, date, rec)


def _execute_buy_plan_with_budget(gateway, buy_plan, prev_close, budget_fills,
                                  cash_available, daily_budget, single_budget, cfg,
                                  dry_run=False, sleep_fn=time.sleep, log=print,
                                  external_committed=None, book_dir=None, date=None):
    """原数量＋真实限价的顺序 BUY 链；额度不足整笔跳过，不缩手数。"""
    from . import backfill as _bf
    from . import order_intents as _oi
    lot = int(cfg["min_lot"])
    headroom = _dec(cfg["cash_headroom"], "0.95")
    external = {str(k): _dec(v or 0) for k, v in (external_committed or {}).items()}
    view = budget_fills if isinstance(budget_fills, dict) else {"buys": []}
    view.setdefault("buys", [])
    live_intents = bool(book_dir and date and not dry_run)
    issues = []
    if live_intents:
        recovered = recover_order_intents(gateway, date, book_dir, log=log)
        _merge_recovered_buys(view, recovered["fills"]["buys"])
        issues.extend(recovered["issues"])
    issues.extend(_budget_bound_issues(view, prev_close))
    all_issues = list(issues)
    start = len(view["buys"])
    records = []
    for b in (buy_plan or []):
        code = str(b.get("code") or "")
        limit = execution_limit_price(b.get("price"))
        rec = {"code": code, "symbol": gm_symbol_of(code), "side": "BUY",
               "gap": b.get("gap"), "price": b.get("price"),
               "qty_target": int(b.get("qty_target") or 0), "qty_placed": 0,
               "truncated": bool(b.get("truncated")),
               "defer_reason": b.get("defer_reason"),
               "status": "skipped", "filled_qty": 0, "filled_price": None,
               "filled_notional": 0.0, "reserved_notional": 0.0,
               "fee_est": 0.0, "message": b.get("defer_reason") or "",
               "order_id": None, "qty_remaining": 0,
               "execution_limit_price": limit, "execution_ceiling_price": limit,
               "budget_price_kind": "reference_limit"}
        if issues:
            rec["defer_reason"] = "budget_or_recovery_blocked"
            rec["message"] = "budget_or_recovery_blocked"
            rec["budget_bound_breach"] = list(issues)
            records.append(rec)
            continue
        if b.get("defer_reason") or int(b.get("qty_place") or 0) <= 0:
            records.append(rec)
            continue
        if limit is None:
            rec["defer_reason"] = "missing_execution_limit"
            rec["message"] = "参考价无法形成有效限价"
            records.append(rec)
            continue
        qty = int(b.get("qty_place") or 0)  # 原数量；这里绝不再按 cap 重算或缩小
        required = Decimal(qty) * _dec(limit)
        committed = {k: _dec(v) for k, v in _bf.committed_notional(view).items()}
        external_total = sum(external.values(), Decimal(0))
        daily_left = max(Decimal(0), _dec(daily_budget) - external_total
                         - sum(committed.values(), Decimal(0)))
        local_used = sum((_dec(_bf.record_committed_notional(r))
                          for r in view["buys"][start:]), Decimal(0))
        cash_left = max(Decimal(0), _dec(cash_available or 0) - local_used)
        single_left = max(Decimal(0), _dec(single_budget)
                          - external.get(code, Decimal(0)) - committed.get(code, Decimal(0)))
        if required > daily_left or required > single_left or required > cash_left * headroom:
            rec.update({"defer_reason": "budget_exceeds_original_order",
                        "message": "原数量×实际限价超过 daily/cash/single 余量，整笔不下单",
                        "qty_target": qty, "required_notional": float(required),
                        "daily_left": float(daily_left), "single_left": float(single_left),
                        "cash_left": float(cash_left)})
            records.append(rec)
            continue

        rec.update({"intent_id": _oi.new_id(), "qty_placed": qty, "qty_remaining": qty,
                    "status": "reserved", "reserved_notional": float(required),
                    "message": "", "defer_reason": None})
        view["buys"].append(rec)  # 内存预算先锁定；下一步必须在 SDK 前持久化同一意图
        if live_intents:
            try:
                _oi.upsert_fill_record(book_dir, date, rec, creating=True)
            except Exception as e:
                view["buys"].remove(rec)
                rec.update({"intended_qty": qty, "qty_placed": 0, "qty_remaining": 0,
                            "reserved_notional": 0.0, "status": "error",
                            "defer_reason": "order_intent_persist_failed",
                            "message": "SDK 前意图落盘失败，未调用柜台: %s" % e})
                issue = {"kind": "order_intent_persist_failed", "code": code,
                         "error": str(e)}
                issues.append(issue)
                all_issues.append(issue)
                records.append(rec)
                continue
        if dry_run:
            rep = {"order_id": None, "status": "dry", "filled_qty": 0,
                   "filled_price": None, "message": ""}
        else:
            def _ack(reply):
                _settle_buy_budget_record(rec, reply)
                if live_intents:
                    _oi.acknowledge(book_dir, date, rec)
            rep = _place_and_poll(gateway, code, "BUY", qty, b.get("price"),
                                  cfg["order_poll_rounds"], cfg["order_poll_sleep_sec"],
                                  sleep_fn, log, execution_limit=limit, on_ack=_ack)
        _settle_buy_budget_record(rec, rep)
        if live_intents:
            try:
                _oi.upsert_fill_record(book_dir, date, rec)
            except Exception as e:
                rep["intent_persist_error"] = str(e)
                rep["status"] = "unresolved"
                _settle_buy_budget_record(rec, rep)
        if rep.get("intent_persist_error"):
            issue = {"kind": "order_intent_ack_persist_failed", "code": code,
                     "order_id": rec.get("order_id"), "error": rep["intent_persist_error"]}
            issues.append(issue)
            all_issues.append(issue)
        if str(rec.get("status") or "") in ("unresolved", "unknown", "error") \
                and int(rec.get("qty_remaining") or 0) > 0:
            issue = {"kind": "order_result_unresolved", "code": code,
                     "order_id": rec.get("order_id"),
                     "qty_remaining": int(rec.get("qty_remaining") or 0),
                     "reserved_notional": rec.get("reserved_notional")}
            issues.append(issue)
            all_issues.append(issue)
        eff_px = rec.get("filled_price") or b.get("price") or 0
        rec["fee_est"] = round(float(rec.get("filled_qty") or qty)
                               * float(eff_px or 0) * _FEE_BUY, 2)
        if rec.get("status") not in _TERMINAL_ORDER_STATUS:
            rec["booked_qty"] = int(rec.get("filled_qty") or qty)
        breach = rec.get("budget_bound_breach")
        if breach:
            issues.append(breach)
            all_issues.append(breach)
        log(f"[WS] BUY {code} gap={b.get('gap') if b.get('gap') is not None else float('nan'):.4f} "
            f"{qty}股@参考{float(b.get('price') or 0):.3f}/实际限价{limit:.2f} → "
            f"{rec.get('status')} 实际={rec.get('filled_notional')} 预留={rec.get('reserved_notional')}"
            f"{' [truncated]' if b.get('truncated') else ''} {rec.get('message') or ''}")
        records.append(rec)
    return records, all_issues


# ════════════════════════ 持仓账本（positions.json，卖侧真源） ════════════════════════

BOOK_FILE = "positions.json"


def load_book(book_dir: str = BOOK_DIR_DEFAULT) -> dict:
    """读超跌跳空确认持仓账本。缺文件/损坏 → 空账本（fail-safe 不抛）。"""
    fp = os.path.join(book_dir, BOOK_FILE)
    if not os.path.exists(fp):
        return {"updated_at": "", "open": {}}
    try:
        with open(fp, encoding="utf-8") as f:
            b = json.load(f)
    except Exception:
        return {"updated_at": "", "open": {}}
    if not isinstance(b, dict) or not isinstance(b.get("open"), dict):
        return {"updated_at": "", "open": {}}
    return b


def save_book(book: dict, book_dir: str = BOOK_DIR_DEFAULT) -> str:
    """落盘持仓账本（幂等覆盖）。返回文件路径。"""
    os.makedirs(book_dir, exist_ok=True)
    book["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fp = os.path.join(book_dir, BOOK_FILE)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(json.dumps(book, ensure_ascii=False, indent=2))
    return fp


def reconcile_book(book_dir: str, acct_qty: dict) -> int:
    """把账本按**账户实际持仓**夹紧：账户没有的整条剔除、数量超出的削到账户数量。返回改动笔数。

    ⚠️ 为什么必须有（2026-10-14 **上线首日实测**）：
      `register_buys_to_book` 在买单 `status="submitted"`（**挂单未成交**）时就会入账本，
      而 `qty` 取的是 `filled_qty or qty_placed` ⇒ **一股没成交也按计划数量全额记进去**。
      当天实测：候选 4 只，gap 最高的两只 **涨停买不到**（+10.03% / +9.99%）⇒ 一直挂在委托上；
      结果 **账本 5 只 / 账户 3 只**。不夹紧的话，14:50 卖侧会对那两只下**无券可卖**的单。

    反过来**也不能**改成"只认 filled_qty"：成交回调（宿主 `on_order_status`）**不写账本**
    ⇒ 后来成交的挂单会**永远进不了账本**（变成无人认领持仓 ⇒ 被归属锁锁死）。
    故取「**挂单即入账 + 每次卖前按账户夹紧**」：没成交就掉，成交了就留。
    """
    book = load_book(book_dir)
    open_pos = book.get("open") or {}
    n = 0
    for code in list(open_pos):
        have = int(acct_qty.get(code) or 0)
        rec = open_pos[code]
        if have <= 0:
            open_pos.pop(code, None)
            n += 1
        elif int(rec.get("qty") or 0) > have:
            rec["qty"] = have
            n += 1
    if n:
        book["open"] = open_pos
        save_book(book, book_dir)
    return n


def register_buys_to_book(book_dir: str, date: str, buys_out: list, picks: list,
                          prev_close: dict, ma20: dict, log=print) -> dict:
    """把当日已成交买单并入持仓账本（卖侧真源）。拒单/废单不入账。返回合并后的账本。

    每笔实际并入数量同步写入 ``booked_qty``：初始 submitted 为 qty_placed，同步
    partial 仅为 filled_qty；后续终态只能回滚该数量中的未成交余量。
    """
    board_map = {p["code"]: p.get("board", "") for p in (picks or [])}
    book = load_book(book_dir)
    for r in (buys_out or []):
        if r.get("status") in _TERMINAL_BAD:
            continue
        qty = int(r.get("filled_qty") or r.get("qty_placed") or 0)
        if qty <= 0:
            continue
        code = r["code"]
        px = float(r.get("filled_price") or r.get("price") or 0)
        if px <= 0:
            continue
        r["booked_qty"] = qty
        pc = float(prev_close.get(code) or 0)
        ma = float(ma20.get(code) or 0)
        dev20 = (pc / ma - 1.0) if (pc > 0 and ma > 0) else None
        old = book["open"].get(code)
        if old:
            # ⚠️ 2026-10-12 修（两日纸面演练抓到的**真缺陷**，非搬家引入）：
            #    原实现只在「同日」合并，**跨日再加仓会整体覆盖**条目 ⇒ 数量只记新买的、
            #    entry_date/entry_price 被重置 ⇒ 账本恒小于账户，而卖侧只卖 min(账本, available)
            #    ⇒ 那部分股票**永远卖不出去**（且 14:50 的账本对账只减不增，永不自愈）。
            #    现在：无论同日/跨日，**一律合并**（数量相加、入场价按量加权），并保留**原 entry_date**
            #    与 days_held ⇒ 「一票=一笔仓位」，到期/止损都按首次入场算。
            oq = int(old.get("qty") or 0)
            opx = float(old.get("entry_price") or 0)
            nq = oq + qty
            book["open"][code] = {**old, "qty": nq,
                                  "entry_price": round((opx * oq + px * qty) / nq, 4)}
            log(f"[WS] 账本合并 {code}: 加仓 {oq}→{nq}股（entry_date 保持 "
                f"{old.get('entry_date')}，加权入场价 {book['open'][code]['entry_price']}）")
            continue
        book["open"][code] = {
            "entry_date": str(date), "entry_price": round(px, 4), "qty": qty,
            "dev20": (round(dev20, 6) if dev20 is not None else None),
            "gap": r.get("gap"), "board": board_map.get(code, ""),
            "days_held": 0, "last_check_date": None,
        }
    save_book(book, book_dir)
    return book


# ════════════════════════ 卖侧 · 决策（纯函数） ════════════════════════

def build_sell_plan(book_open: dict, prices: dict, today: str,
                    hold_days: int = DEFAULTS["hold_days"],
                    stop_pct: float = DEFAULTS["stop_pct"],
                    deep_dev20: float = DEFAULTS["stop_deep_dev20"]) -> list:
    """由持仓账本 + 现价 → 卖出决策（SL5只深止损 + 到期平仓）。纯函数，可单测。

    规则（exp19 定稿，owner 2026-10-10 拍板 SL5只深）：
      · 深超跌(dev20 < deep_dev20) 持仓：任一交易日 close < entry_price×(1−stop_pct) → 止损。
      · 浅/中超跌：不挂止损，只按持有到期。
      · 全部：持有满 hold_days 个交易日 → 到期平仓（死拿5天 horizon，exp6/7）。
    返回 [{code, entry_date, entry_price, dev20, days_held, deep, price,
          stop_hit, expired, reason}]；reason∈{t1_same_day,no_price,hold,
          sl5_deep,horizon_expired}，hold 表示不卖出。
    """
    plan = []
    for code, rec in sorted((book_open or {}).items()):
        rec = rec or {}
        entry_date = str(rec.get("entry_date") or "")
        entry_price = float(rec.get("entry_price") or 0)
        dev20 = rec.get("dev20")
        days_held = int(rec.get("days_held") or 0)
        deep = dev20 is not None and float(dev20) < float(deep_dev20)
        cur = float(prices.get(code) or 0)
        row = {"code": code, "entry_date": entry_date, "entry_price": entry_price,
               "dev20": dev20, "days_held": days_held, "deep": deep, "price": cur,
               "stop_hit": False, "expired": False, "reason": "hold"}
        if entry_date == today:
            row["reason"] = "t1_same_day"
        elif not (cur > 0 and entry_price > 0):
            row["reason"] = "no_price"
        elif days_held >= int(hold_days):
            row["expired"] = True
            row["reason"] = "horizon_expired"
        elif deep and cur < entry_price * (1 - float(stop_pct)):
            row["stop_hit"] = True
            row["reason"] = "sl5_deep"
        plan.append(row)
    return plan


# ════════════════════════ 卖侧 · 共用（对账 + 下单扣账） ════════════════════════

def _snapshot_positions(gateway) -> dict:
    """账户当前持仓快照 → {code: {qty, available, cost}}（仅 qty>0）。"""
    positions = {code_of(k) or str(k): {"qty": int((v or {}).get("qty") or 0),
                                        "available": int((v or {}).get("available") or 0),
                                        "cost": float((v or {}).get("cost") or 0)}
                 for k, v in (gateway.get_positions() or {}).items()}
    return {k: v for k, v in positions.items() if v["qty"] > 0}


def _reconcile_open(open_pos: dict, positions: dict, log=print) -> None:
    """账本 ↔ 账户对账：外部卖出（终端手工单等）→ 从账本移除/减量。原地改 open_pos。"""
    for code in list(open_pos):
        acct = positions.get(code)
        acct_qty = int((acct or {}).get("qty") or 0)
        rec = open_pos[code]
        book_qty = int(rec.get("qty") or 0)
        if acct_qty <= 0:
            log(f"[WS] 账本票 {code} 账户已无持仓(外部卖出) → 移出账本")
            open_pos.pop(code, None)
        elif acct_qty < book_qty:
            log(f"[WS] 账本票 {code} 账户持仓 {acct_qty} < 账本 {book_qty}(外部部分卖出) → 减量")
            rec["qty"] = acct_qty


def _days_held_by_calendar(entry_date: str, date: str, trading_days) -> int | None:
    """按交易日历推导持有天数：`entry_date < d <= date` 的交易日数（含当日）。

    日历不可用/为空 ⇒ None（调用方退化为计数器）。entry 当日/异常未来日 ⇒ 0。
    """
    if not trading_days:
        return None
    entry = str(entry_date or "")
    today = str(date)
    if not entry or entry >= today:
        return 0
    days = {str(d)[:10] for d in trading_days}
    days.add(today)                       # 正在运行的当日必为交易日
    return sum(1 for d in days if entry < d <= today)


def _advance_days_held(open_pos: dict, date: str, trading_days=None, log=print) -> None:
    """每日一次推进 days_held（幂等；entry 当日不推进）。14:50 止损检查专用。

    **㉘ 修复（2026-11-13，688652 满 5 交易日漏卖红线）**：原实现是「策略每跑一天 +1」
    的计数器，跨周末/漏跑/重启/状态回退都会丢增量（688652 在 11-12 应为 5 却只有 4，
    到期未卖，迟至 11-13 才卖出）。现在优先按**交易日历推导**
    （`_days_held_by_calendar`），计数与日历不一致时**以日历为准**并留痕
    `days_held_calendar_fixed`；日历未注入/取数失败时退化为旧计数器行为。
    """
    for code, rec in open_pos.items():
        entry = str(rec.get("entry_date") or "")
        if entry == str(date):
            continue
        cal = _days_held_by_calendar(entry, date, trading_days)
        if cal is not None:
            old = int(rec.get("days_held") or 0)
            if cal != old:
                rec["days_held"] = cal
                rec["days_held_calendar_fixed"] = {"from": old, "to": cal, "date": str(date)}
                log(f"[WS] ㉘天数日历校准 {code}: 计数器 {old} → 交易日历 {cal}（{date}）")
            rec["last_check_date"] = date
        elif rec.get("last_check_date") != date:
            rec["days_held"] = int(rec.get("days_held") or 0) + 1
            rec["last_check_date"] = date


def _execute_sell_decisions(gateway, open_pos: dict, positions: dict, decisions: list,
                            cfg: dict, sleep_fn, log) -> list:
    """对 decisions 里需卖出的条目下单并扣账本。返回 sells_out。

    decisions 条目字段：code/reason/price（+ 可选 entry_date/dev20/days_held 留痕）。
    reason ∈ {hold,t1_same_day,no_price,not_next_day} 跳过；其余一律 SELL。
    """
    sells_out = []
    for s in decisions:
        code = s["code"]
        rec = open_pos.get(code)
        base = {"code": code, "symbol": gm_symbol_of(code), "side": "SELL",
                "reason": s["reason"], "entry_date": s.get("entry_date"),
                "entry_price": s.get("entry_price"), "dev20": s.get("dev20"),
                "days_held": s.get("days_held"), "price": s.get("price"),
                "qty_placed": 0, "status": "skipped", "filled_qty": 0,
                "filled_price": None, "fee_est": 0.0, "message": ""}
        if s["reason"] in ("hold", "t1_same_day", "no_price", "not_next_day") or rec is None:
            base["message"] = "无需卖出" if s["reason"] == "hold" else s["reason"]
            sells_out.append(base)
            continue
        avail = int((positions.get(code) or {}).get("available") or 0)
        sellable = min(int(rec.get("qty") or 0), avail)
        if sellable <= 0:
            base["message"] = "t1_locked(无可卖量)"
            sells_out.append(base)
            continue
        rep = _place_and_poll(gateway, code, "SELL", sellable, s["price"],
                              cfg["order_poll_rounds"], cfg["order_poll_sleep_sec"],
                              sleep_fn, log)
        base.update({"qty_placed": sellable, "status": rep.get("status"),
                     "filled_qty": int(rep.get("filled_qty") or 0),
                     "filled_price": rep.get("filled_price"),
                     "message": rep.get("message") or ""})
        eff_px = rep.get("filled_price") or s["price"]
        base["fee_est"] = round(float(rep.get("filled_qty") or sellable) * float(eff_px or 0) * _FEE_SELL, 2)
        if rep.get("status") in _TERMINAL_BAD or rep.get("status") == "error":
            log(f"[WS] ⚠️ SELL {code} {rep.get('status')}: {base['message']}")
        else:
            remaining = int(rec.get("qty") or 0) - int(rep.get("filled_qty") or 0)
            if remaining <= 0:
                open_pos.pop(code, None)
            else:
                rec["qty"] = remaining
        log(f"[WS] SELL {code} {s['reason']} {sellable}股@{s['price']:.3f} → "
            f"{rep.get('status')} {base['message']}")
        sells_out.append(base)

    # ── 未知态卖单迟确认（2026-10-16 修）────────────────────────────────
    # 根因：GM 仿真终端同步下单回报里 order_id 为空 ⇒ `_place_and_poll` 的轮询
    # 前置条件 `rep.get("order_id")` 为假 ⇒ 一轮都不轮询；而成交回报是异步的
    # （当日实测 0.5~6 秒后才到）⇒ status 停在 submitted、filled_qty=0
    # ⇒ 明明卖出成交了，账本却不剔除（2026-10-16 实测：301373/605198/688003
    # 三笔全部成交，positions.json 仍挂 6 只，需手工回填）。
    # 处置：对仍停在 submitted 的卖单，按「账户持仓变化」做迟确认——
    # 复用轮询参数等待 + 重取持仓快照，数量下降即视为成交，同步回填账本。
    # 订单真没成交（挂在限价上）⇒ 账户持仓不变 ⇒ 保留账本条目，行为正确。
    pending = [r for r in sells_out if r["status"] == "submitted" and r["qty_placed"] > 0]
    if pending:
        rounds = max(1, int(cfg.get("order_poll_rounds", 3)))
        for _ in range(rounds):
            sleep_fn(float(cfg.get("order_poll_sleep_sec", 2.0)))
            snap = _snapshot_positions(gateway)
            still = []
            for r in pending:
                code = r["code"]
                rec = open_pos.get(code)
                if rec is None:
                    continue
                before = int((positions.get(code) or {}).get("qty") or 0)
                now_qty = int((snap.get(code) or {}).get("qty") or 0)
                filled = max(0, before - now_qty)
                if filled > 0:
                    r["status"] = "filled_by_position"
                    r["filled_qty"] = int(r.get("filled_qty") or 0) + filled
                    r["message"] = (r["message"] + " 按账户持仓变化迟确认成交").strip()
                    remaining = int(rec.get("qty") or 0) - filled
                    if remaining <= 0:
                        open_pos.pop(code, None)
                    else:
                        rec["qty"] = remaining
                        still.append(r)   # 部分成交：剩余委托可能仍在途，下轮继续跟
                    log(f"[WS] SELL {code} 迟确认: 账户持仓 {before}→{now_qty} "
                        f"⇒ 本轮成交 {filled} 股，账本已回填")
                else:
                    still.append(r)
            positions = snap          # 滚动基准，防下轮重复计数
            pending = still
            if not pending:
                break
    return sells_out


def _finish_sell(date, mode, started, sells_out, open_pos, note, tag, book_dir, cfg) -> dict:
    """卖侧收尾：写 sells_{tag}_{date}.json + 摘要。返回 fills dict。"""
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notional = sum((r.get("filled_qty") or r.get("qty_placed") or 0)
                   * (r.get("filled_price") or r.get("price") or 0)
                   for r in sells_out if r["qty_placed"] > 0)
    horizon_expired = sum(1 for r in sells_out
                          if r.get("reason") == "horizon_expired" and r["qty_placed"] > 0)
    summary = {"sells_planned": len(sells_out),
               "sells_placed": sum(1 for r in sells_out if r["qty_placed"] > 0),
               "sells_filled": sum(1 for r in sells_out if r.get("filled_qty") and r["filled_qty"] > 0),
               **_sell_order_terminal_summary(sells_out),
               "stop_count": sum(1 for r in sells_out if r.get("reason") == "sl5_deep" and r["qty_placed"] > 0),
               # 兼容字段：expired_count 仍表示持有到期，不表示柜台 status=12。
               "expired_count": horizon_expired,
               "horizon_expired_count": horizon_expired,
               "pop_count": sum(1 for r in sells_out if r.get("reason") == "pop_sell" and r["qty_placed"] > 0),
               "open_after": len(open_pos),
               "notional_placed": round(notional, 2)}
    fills = {"date": str(date), "mode": mode, "started_at": started, "finished_at": finished,
             "strategy": "oversold_gap_confirm", "note": note,
             "sells": sells_out, "summary": summary,
             "book_open": {k: v for k, v in sorted(open_pos.items())},
             "fee_model": _FEE_SRC,
             "params": {k: cfg[k] for k in DEFAULTS}}
    os.makedirs(book_dir, exist_ok=True)
    fp_fills = os.path.join(book_dir, f"sells_{tag}_{date}.json")
    with open(fp_fills, "w", encoding="utf-8") as f:
        f.write(json.dumps(fills, ensure_ascii=False, indent=2))
    fills["_paths"] = {"sells": fp_fills}
    return fills


# ════════════════════════ 卖侧 · 收盘止损/到期平仓（SL5只深） ════════════════════════

def run_sell_exec(gateway, date: str, prices: dict,
                  book_dir: str = BOOK_DIR_DEFAULT, params: dict | None = None,
                  dry_run: bool = False, sleep_fn=time.sleep, log=print,
                  trading_days=None) -> dict:
    """超跌跳空确认 14:50 收盘止损/到期平仓卖侧主流程。返回 fills dict（同时落盘）。

    prices = {code: 当日现价}（调用方注入，14:50 批量取数）。
    trading_days = 近期交易日列表（ISO 日期，调用方由指数日线注入；㉘ 修复）——
    注入后 days_held 按交易日历推导，自愈计数器滞后；None ⇒ 退化为逐日 +1 旧行为。
    """
    cfg = {**DEFAULTS, **(params or {})}
    mode = "dry" if dry_run else "live"
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    book = load_book(book_dir)
    open_pos = dict(book.get("open") or {})
    positions = _snapshot_positions(gateway)
    _reconcile_open(open_pos, positions, log)
    _advance_days_held(open_pos, date, trading_days=trading_days, log=log)

    sell_plan = build_sell_plan(open_pos, prices, str(date),
                                hold_days=int(cfg["hold_days"]),
                                stop_pct=float(cfg["stop_pct"]),
                                deep_dev20=float(cfg["stop_deep_dev20"]))
    log(f"[WS] {date} 卖侧检查 账本={len(open_pos)}只 现价={len(prices)} "
        f"决策={[(s['code'], s['reason']) for s in sell_plan]}")

    sells_out = _execute_sell_decisions(gateway, open_pos, positions, sell_plan,
                                        cfg, sleep_fn, log)
    book["open"] = open_pos
    save_book(book, book_dir)
    return _finish_sell(date, mode, started, sells_out, open_pos,
                        "sell_side_sl5_deep", "close", book_dir, cfg)


# ════════════════════════ 卖侧 · 次日早盘冲高卖 ════════════════════════

def build_morning_sell_plan(book_open: dict, morning_highs: dict, today: str,
                            target_pct: float = DEFAULTS["pop_target_pct"]) -> list:
    """D+1 早盘冲高卖决策（纯函数，可单测）。

    只对「次日」持仓判定：entry_date != today 且 days_held == 0（= 第一个可卖早盘；
    days_held 由 14:50 止损检查每天推进一次，早盘时仍是 0）。早盘最高 >= entry_price×(1+target)
    → 冲高卖。浅/中/深都适用（这是止盈不是止损）。返回决策列表。
    """
    plan = []
    for code, rec in sorted((book_open or {}).items()):
        rec = rec or {}
        entry_date = str(rec.get("entry_date") or "")
        entry_price = float(rec.get("entry_price") or 0)
        days_held = int(rec.get("days_held") or 0)
        hi = float(morning_highs.get(code) or 0)
        row = {"code": code, "entry_date": entry_date, "entry_price": entry_price,
               "days_held": days_held, "price": hi, "dev20": rec.get("dev20"),
               "reason": "hold"}
        if entry_date == today:
            row["reason"] = "t1_same_day"
        elif days_held != 0:
            row["reason"] = "not_next_day"
        elif not (hi > 0 and entry_price > 0):
            row["reason"] = "no_price"
        elif hi >= entry_price * (1 + float(target_pct)):
            row["reason"] = "pop_sell"
        plan.append(row)
    return plan


def run_morning_sell_exec(gateway, date: str, morning_highs: dict,
                          book_dir: str = BOOK_DIR_DEFAULT, params: dict | None = None,
                          dry_run: bool = False, sleep_fn=time.sleep, log=print) -> dict:
    """超跌跳空确认 次日早盘冲高卖主流程。返回 fills dict（同时落盘）。

    morning_highs = {code: 当日早盘最高价}（调用方注入，10:00 批量取数）。
    只对 days_held==0（次日）持仓判定；更老持仓不动，交 14:50 止损/到期。
    """
    cfg = {**DEFAULTS, **(params or {})}
    mode = "dry" if dry_run else "live"
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    book = load_book(book_dir)
    open_pos = dict(book.get("open") or {})
    positions = _snapshot_positions(gateway)
    _reconcile_open(open_pos, positions, log)
    # 早盘卖**不**推进 days_held（推进留给 14:50；此处只读 days_held==0 判定次日）

    plan = build_morning_sell_plan(open_pos, morning_highs, str(date),
                                   target_pct=float(cfg["pop_target_pct"]))
    log(f"[WS] {date} 早盘冲高检查 账本={len(open_pos)}只 "
        f"决策={[(s['code'], s['reason']) for s in plan]}")

    sells_out = _execute_sell_decisions(gateway, open_pos, positions, plan,
                                        cfg, sleep_fn, log)
    book["open"] = open_pos
    save_book(book, book_dir)
    return _finish_sell(date, mode, started, sells_out, open_pos,
                        "sell_side_morning_pop", "morning", book_dir, cfg)


# ════════════════════════ 主流程 ════════════════════════

def _log_decision(log, full: dict, names: dict | None = None,
                  boards: dict | None = None, top_k: int = 6) -> None:
    """把 10:00 的**判定过程**打进控制台/策略日志（2026-10-15，owner 要求）。

    ⚠️ 纯展示：只读 `decision.evaluate` 附加的 `diag` 字段，不参与任何下单决策。
    存在的理由：10-15 实盘 176 只候选全废，但控制台只有一行 `pool_n=176`，
    看不出"卡在哪一关、差多远"。这里补三样东西：
      ① 漏斗：池 → 超跌 → ①全程不破昨收 → ②站上昨收 → ③站上VWAP → 三全过
         （各关是**独立**通过数，所以能看出"176 只里只有 1 只没破昨收"这种话）
      ② 首个未过关的分布（`signal_ok` 的 reason，按检查顺序即瀑布）
      ③ gap 最高的未过票明细：差在哪一关、破了昨收多少
    """
    d = (full or {}).get('diag') or {}
    g = d.get('gates') or {}
    if not g:
        return
    _pool = int((full or {}).get('pool_n') or 0)
    # 着色（未开启时 style.c 原样返回 ⇒ 纯文本，安全）
    def _gv(name, val):
        """漏斗里把关：通过率 <10% 标黄加粗（瓶颈），否则绿。"""
        _n = _st.c("%s" % val, _st.YELLOW, _st.BOLD) if (_pool and val / _pool < 0.10) \
            else _st.c("%s" % val, _st.GREEN)
        return "%s %s" % (_st.c(name, _st.GREY), _n)
    _all_n = int(g.get('all', 0))
    log("%s %s 池 %s | %s | %s | %s | %s | %s" % (
        _st.c("[OSC]", _st.CYAN), _st.c("10:00 判定漏斗:", _st.BOLD),
        _st.c("%d" % _pool, _st.BOLD),
        _gv("超跌(昨收<MA20)", g.get('weak', 0)), _gv("①全程不破昨收", g.get('g1', 0)),
        _gv("②站上昨收", g.get('g2', 0)), _gv("③站上VWAP", g.get('g3', 0)),
        _st.c("三全过 %d" % _all_n,
              _st.GREEN, _st.BOLD) if _all_n else _st.c("三全过 0", _st.RED, _st.BOLD)))
    _rb = int((full or {}).get('rejected_board') or 0)
    if _rb:
        log("%s %s" % (_st.c("[OSC]", _st.CYAN),
                       _st.c("10:00 触发但被板块过滤掉 %d 只（可交易 %d 只）"
                             % (_rb, int((full or {}).get('n_tradable') or 0)), _st.YELLOW)))
    rs = d.get('reasons') or {}
    if rs:
        _names = {'bar_missing': '棒缺失', 'prev_close_missing': '昨收缺失',
                  'not_weak(prev_close>=ma20)': '非超跌', 'gap_not_held(low<prev_close)': '①破昨收',
                  'not_above_prev_close': '②未站上昨收', 'not_above_vwap': '③未站上VWAP'}
        log("%s %s " % (_st.c("[OSC]", _st.CYAN), _st.c("10:00 首个未过关:", _st.DIM))
            + " · ".join("%s %s" % (_names.get(k, k), _st.c("%d" % v, _st.YELLOW))
                         for k, v in list(rs.items())[:6]))
    near = (d.get('near') or [])[:top_k]
    if not near:
        return
    nm = names or {}
    bm = boards or {}
    _tag = {'gap_not_held(low<prev_close)': '①破昨收', 'not_above_prev_close': '②未站上昨收',
            'not_above_vwap': '③未站上VWAP', 'not_weak(prev_close>=ma20)': '非超跌',
            'bar_missing': '棒缺失'}
    log("%s %s" % (_st.c("[OSC]", _st.CYAN),
                   _st.c("10:00 最接近(gap 降序 %d/%d 只):"
                         % (len(near), d.get('n_near') or len(near)), _st.DIM)))
    for n in near:
        _c = str(n.get('code'))
        _name = nm.get(_c) or nm.get(_c.zfill(6)) or ''
        _why = _tag.get(n.get('reason'), str(n.get('reason')))
        _lo = ''
        try:
            if n.get('low') and n.get('prev_close'):
                _lo = " 最低/昨收%+.2f%%" % ((n['low'] / n['prev_close'] - 1.0) * 100.0)
        except Exception:
            pass
        _gap = (n.get('gap') or 0) * 100
        log("        %s %-8s %-10s %s  差:%s%s  昨收%.2f 低%.2f 收%.2f" % (
            _st.c(_c, _st.CYAN), _name, str(bm.get(_c) or '')[:10],
            _st.c("gap%+6.2f%%" % _gap, _st.GREEN if _gap > 0 else _st.RED),
            _st.c(_why, _st.YELLOW), _lo,
            n.get('prev_close') or 0, n.get('low') or 0, n.get('close') or 0))


def run_buy_exec(gateway, date: str, prev_close: dict, ma20: dict, bars: dict,
                 board: dict | None = None, allowed_boards=None,
                 book_dir: str = BOOK_DIR_DEFAULT, params: dict | None = None,
                 dry_run: bool = False, sleep_fn=time.sleep, log=print,
                 exclude_codes=None, code_committed=None, names: dict | None = None,
                 prescreen_meta: dict | None = None) -> dict:
    """超跌跳空确认 10:00 买侧执行主流程。返回 fills dict（同时落盘）。

    fail-closed：决策核因缺数据/min_pool 不足返回空 picks ⇒ 当日不下单（不抛异常）。

    `exclude_codes` / `code_committed`（2026-10-14 买不进替补用）：补单轮调用时，
    把"今日已碰过的票"排除、并按**已占用名义**扣单票额度（详见 `backfill.py`）。
    **默认 None ⇒ 行为与加这两参之前逐位一致。**
    """
    cfg = {**DEFAULTS, **(params or {})}
    lot = int(cfg["min_lot"])
    top_n = int(cfg["top_n"])
    single_budget = float(cfg["single_budget"])
    daily_budget = float(cfg["daily_budget"])
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "dry" if dry_run else "live"

    _full = evaluate_full(prev_close, ma20, bars, board=board,
                          allowed_boards=allowed_boards, top_n=top_n)
    # 判定过程落控制台/策略日志（2026-10-15，纯展示；两条分支共用同一处调用）
    _log_decision(log, _full, names=names, boards=board)
    picks = list(_full["picks"])
    _picks_raw = list(picks)
    _excl = set(exclude_codes or ())
    if _excl:
        picks = [p for p in picks if str(p.get("code")) not in _excl]
    # 顺移候补：全排序候选里去掉**本轮 picks** 与"今日已碰过"的票
    from . import backfill as _bf
    spares = _bf.build_spares(_full.get("signals"), [p["code"] for p in _picks_raw],
                              _excl, bars=bars)
    # 10:00 判定全貌 + 逐只原因落盘（2026-10-17）。**必须在这里**：`picks` 已过 exclude
    # 过滤、`spares` 已建好，且位于 no-picks 早返回**之前** ⇒ 两条分支都写。
    # 纯展示留痕：内部全兜底，写失败绝不影响下单（fail-closed 语义不在这里）。
    decision_fp = _write_decision(date, mode, started, _full, picks, spares,
                                  prev_close=prev_close, ma20=ma20, board=board,
                                  names=names, book_dir=book_dir,
                                  prescreen_meta=prescreen_meta,
                                  note=("no_picks" if not picks else ""), log=log)
    if not picks:
        log(f"[WS] {date} 无超跌跳空确认候选（fail-closed，当日不下单） pool_n={len(prev_close)}")
        # 2026-10-13 修复：无候选日 cash_before 原先写死 0.0，会被误读成「现金查询失败」
        # （10-13 实际现金 24.3 万）。改为如实读取——纯报告字段，不影响 fail-closed 语义。
        try:
            cash0 = float(gateway.get_cash() or 0)
        except Exception:
            cash0 = 0.0
        return _write_fills(date, mode, started, [], [], cash0, 0.0, cfg, book_dir,
                            note="no_picks", spares=spares)

    cash0 = float(gateway.get_cash() or 0)
    positions = {code_of(k) or str(k): {"qty": int((v or {}).get("qty") or 0),
                                        "available": int((v or {}).get("available") or 0),
                                        "cost": float((v or {}).get("cost") or 0)}
                 for k, v in (gateway.get_positions() or {}).items()}
    positions = {k: v for k, v in positions.items() if v["qty"] > 0}
    external_committed = {str(k): max(0.0, float(v or 0))
                          for k, v in (code_committed or {}).items()}
    cash_budget = min(max(0.0, daily_budget - sum(external_committed.values())), cash0)
    log(f"[WS] {date} 候选={len(picks)} 现金={cash0:.0f} 预算={cash_budget:.0f} "
        f"picks={[(p['code'], round(p.get('gap') or 0, 4)) for p in picks]}")

    # 买入参考价 = 10:00 收盘（信号入场价口径）；它不再承担成交上界或异步额度结算。
    for p in picks:
        b = bars.get(p["code"]) or {}
        p["price"] = p.get("price") or float(b.get("close") or 0)

    buy_plan = build_buy_plan(picks, positions, cash_budget, single_budget,
                              min_lot=lot, cash_headroom=float(cfg["cash_headroom"]),
                              code_committed=code_committed)
    budget_view = {"date": str(date), "buys": []}
    new_buys, budget_issues = _execute_buy_plan_with_budget(
        gateway, buy_plan, prev_close, budget_view, cash0, daily_budget, single_budget,
        cfg, dry_run=dry_run, sleep_fn=sleep_fn, log=log,
        external_committed=external_committed, book_dir=book_dir, date=date)
    # new_buys 是本轮完整决策记录（含零数量/deferred）；budget_view 只含
    # 恢复旧单与真正走到 SDK 前预留的委托，不能用它覆盖并丢掉未下单记录。
    new_ids = {id(rec) for rec in new_buys}
    recovered = [rec for rec in (budget_view.get("buys") or [])
                 if id(rec) not in new_ids]
    buys_out = recovered + list(new_buys)
    total_placed = 0.0
    for rec in buys_out:
        if rec.get("status") in _TERMINAL_ORDER_STATUS or not rec.get("qty_placed"):
            continue
        eff_px = rec.get("filled_price") or rec.get("price") or 0
        total_placed += float(rec.get("filled_qty") or rec.get("qty_placed") or 0) \
            * float(eff_px or 0)
    if budget_issues:
        log(f"[WS] ⛔ 买入上界审计异常，后续 BUY 已 fail-closed: {budget_issues}")

    fills = _write_fills(date, mode, started, buys_out, picks, cash0, total_placed,
                         cfg, book_dir, spares=spares)
    if not dry_run:
        try:
            # 恢复出的旧委托只回填 fills，不再次加进 positions；仅本轮新记录入账。
            register_buys_to_book(book_dir, date, new_buys, picks, prev_close, ma20, log)
        except Exception as e:
            log(f"[WS] ⚠️ 买入入账失败（不阻断）: {e}")
    return fills


def _san(x):
    """NaN/inf → None（落盘安全：`json.dump` 默认把 NaN 写成非法 JSON 字面量）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _write_decision(date, mode, started, full, picks, spares, *,
                    prev_close=None, ma20=None, board=None, names=None,
                    book_dir=BOOK_DIR_DEFAULT, prescreen_meta=None,
                    note: str = "", log=print) -> str | None:
    """10:00 判定全貌 + **逐只原因** → `decision_{date}.json`（2026-10-17）。

    为什么值得单独落盘：`_log_decision` 只把漏斗 + 最接近的 6 只打进控制台/日志，
    逐只原因（`decision.evaluate` 的 `diag['failures']`）出过内存就没了 ⇒
    GUI 无法回答「某只票为什么没被选上」。

    ⚠️ **纯展示留痕，绝不抛**：整段兜底；写失败只记一行日志，不影响下单。
    ⚠️ 原子写（tmp+replace）：GUI 可能正在读，普通写会让它读到半截 JSON。
    ⚠️ 本文件记的是 **10:00 的决策**；后续补单轮不改写它（见 `merge_backfill_fills`）。
    """
    try:
        d = (full or {}).get('diag') or {}
        g = d.get('gates') or {}
        _bd = board or {}
        _nm = names or {}
        _rank = {str(p.get('code')): i + 1 for i, p in enumerate(picks or [])}

        rows = []
        for p in (picks or []):
            _c = str(p.get('code'))
            rows.append({'code': _c, 'name': _nm.get(_c, ''), 'board': p.get('board', ''),
                         'gap': _san(p.get('gap')), 'status': 'picked',
                         'rank': _rank.get(_c), 'reason': 'ok'})
        for s in (spares or []):
            _c = str(s.get('code'))
            rows.append({'code': _c, 'name': _nm.get(_c, ''), 'board': s.get('board', ''),
                         'gap': _san(s.get('gap')), 'status': 'spare',
                         'rank': None, 'reason': 'ok', 'price': _san(s.get('price'))})
        for f in (d.get('failures') or []):
            _c = str(f.get('code'))
            rows.append({'code': _c, 'name': _nm.get(_c, ''),
                         'board': f.get('board') or _bd.get(_c, ''),
                         'gap': _san(f.get('gap')), 'status': 'failed',
                         'rank': None, 'reason': f.get('reason'),
                         'gates': f.get('gates'), 'low': _san(f.get('low')),
                         'close': _san(f.get('close')), 'vwap': _san(f.get('vwap')),
                         'prev_close': _san(f.get('prev_close')),
                         'ma20': _san(f.get('ma20'))})
        # gap 降序、None 垫底（None 与数字不可比 ⇒ 用 -inf 替）
        rows.sort(key=lambda r: (-(r['gap'] if r['gap'] is not None else -1e9), r['code']))

        payload = {
            "date": str(date), "generated_at": started, "mode": mode,
            "strategy": "oversold_gap_confirm", "note": note or "",
            "premarket": ({"universe": (prescreen_meta or {}).get("universe", ""),
                           "board_top_n": (prescreen_meta or {}).get("board_top_n")}
                          if prescreen_meta else None),
            "funnel": {"pool_n": int((full or {}).get('pool_n') or 0),
                       "weak": g.get('weak'), "g1": g.get('g1'), "g2": g.get('g2'),
                       "g3": g.get('g3'), "all": g.get('all'),
                       "n_tradable": int((full or {}).get('n_tradable') or 0),
                       "n_failures": d.get('n_failures')},
            # ⚠️ 各关是**独立通过数**，不是"上一关的幸存者"（见 decision.evaluate 注释）
            "reason_counts": d.get('reasons') or {},
            "picks": [{"code": r['code'], "name": r['name'], "board": r['board'],
                       "gap": r['gap'], "rank": r['rank'], "status": "picked",
                       "reason": "ok"}
                      for r in rows if r['status'] == 'picked'],
            "spares": [{"code": r['code'], "gap": r['gap'], "board": r['board']}
                       for r in rows if r['status'] == 'spare'],
            "stocks": rows,
        }
        os.makedirs(book_dir, exist_ok=True)
        fp = os.path.join(book_dir, "decision_%s.json" % str(date))
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, fp)
        return fp
    except Exception as e:                                        # noqa: BLE001
        try:
            log(f"[WS] ⚠️ 决策留痕写入失败（不阻断）: {e}")
        except Exception:
            pass
        return None


def _order_terminal_summary(records) -> dict:
    """委托终态四栏；持有到期不是委托 expired，绝不在此混计。"""
    counts = {name: sum(1 for r in (records or []) if r.get("status") == name)
              for name in ("rejected", "cancelled", "expired", "error")}
    return {"rejected_count": counts["rejected"],
            "cancelled_count": counts["cancelled"],
            "expired_count": counts["expired"],
            "error_count": counts["error"],
            "terminal_counts": dict(counts)}


def _sell_order_terminal_summary(records) -> dict:
    """卖侧委托终态分栏；`expired_count` 留给既有 horizon 字段，故另设 order_expired。"""
    counts = {name: sum(1 for r in (records or []) if r.get("status") == name)
              for name in ("rejected", "cancelled", "expired", "error")}
    return {"rejected_count": counts["rejected"],
            "cancelled_count": counts["cancelled"],
            "order_expired_count": counts["expired"],
            "error_count": counts["error"],
            "terminal_counts": dict(counts)}


def _buys_summary(buys_out: list) -> dict:
    """重算委托摘要，严格分开真实成交、在途预留与保守占用。"""
    _ok = [r for r in (buys_out or [])
           if not (r.get("status") in _TERMINAL_BAD)]
    actual = 0.0
    reserved = 0.0
    committed = 0.0
    breaches = []
    for r in (buys_out or []):
        placed = max(0, int(r.get("qty_placed") or 0))
        filled = min(placed, max(0, int(r.get("filled_qty") or 0)))
        actual_px = _positive_price(r.get("filled_price"))
        known_notional = None
        try:
            explicit = r.get("filled_notional")
            if explicit is not None:
                value = float(explicit)
                if math.isfinite(value) and value > 0:
                    known_notional = value
        except (TypeError, ValueError):
            pass
        if known_notional is None and filled > 0 and actual_px is not None:
            known_notional = filled * actual_px
        if known_notional is not None:
            actual += known_notional
            committed += known_notional
        elif filled > 0:
            # 量已到、VWAP 未到：actual 不编造，committed 按真实限价保守计提。
            limit = _limit_of_record(r)
            if limit is not None:
                committed += filled * limit
        status = str(r.get("status") or "")
        if status not in _TERMINAL_ORDER_STATUS and status != "skipped":
            remaining = max(0, placed - filled)
            limit = _limit_of_record(r)
            if remaining > 0 and limit is not None:
                value = float(Decimal(remaining) * _dec(limit))
                reserved += value
                committed += value
        breach = r.get("budget_bound_breach")
        if isinstance(breach, list):
            breaches.extend(breach)
        elif breach:
            breaches.append(breach)
    return {"buys_planned": len(buys_out or []),
            "buys_placed": sum(1 for r in _ok if (r.get("qty_placed") or 0) > 0),
            **_order_terminal_summary(buys_out),
            "deferred_count": sum(1 for r in (buys_out or []) if r.get("defer_reason")),
            "actual": round(actual, 2), "reserved": round(reserved, 2),
            "committed": round(committed, 2), "budget_bound_breaches": breaches,
            # 兼容旧展示字段；新口径不再把未成交委托价冒充实际成交额。
            "notional_placed": round(committed, 2)}


def _write_fills(date, mode, started, buys_out, picks, cash0, total_placed,
                 cfg, book_dir, note: str = "", spares=None, skip_reason=None) -> dict:
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # ⚠️ 2026-10-12：`buys_placed` 原先只数 `qty_placed>0`，把**被拒/被锁**的单也算成"已下单"
    # （与 S1 复盘里 `buys_placed=4/rejected=0` 的虚报同源）。改为只数未被终态否决的。
    # （2026-10-16：口径抽成 `_buys_summary` 与补单轮合并共用；数值不变，由单测钉住。）
    summary = {**_buys_summary(buys_out),
               "candidates": len(picks), "cash_before": round(cash0, 2)}
    if skip_reason is not None:
        summary["skip_reason"] = skip_reason
    fills = {"date": str(date), "mode": mode, "started_at": started, "finished_at": finished,
             "strategy": "oversold_gap_confirm", "note": note or "",
             "picks": [{"code": p["code"], "gap": p.get("gap"),
                        "board": p.get("board", "")} for p in picks],
             # 2026-10-14 买不进替补：把**顺移候补**一并落盘。
             # 为什么必须落盘：`hooks._STATE` 在内存里，掘金策略中途重启就没了 ⇒
             # 挂单会一直冻结到 15:00（正是 10-14 的事故形态）。落盘后补单钩子能回读。
             "spares": [{"code": s["code"], "gap": s.get("gap"),
                         "board": s.get("board", ""), "price": s.get("price")}
                        for s in (spares or [])],
             "buys": buys_out, "summary": summary,
             "fee_model": _FEE_SRC,
             "params": {k: cfg[k] for k in DEFAULTS}}
    os.makedirs(book_dir, exist_ok=True)
    fp_fills = os.path.join(book_dir, f"fills_{date}.json")
    with open(fp_fills, "w", encoding="utf-8") as f:
        f.write(json.dumps(fills, ensure_ascii=False, indent=2))
    fp_rep = os.path.join(book_dir, f"exec_report_{date}.json")
    with open(fp_rep, "w", encoding="utf-8") as f:
        f.write(json.dumps({"date": str(date), "mode": mode, "summary": summary,
                            "fills_file": fp_fills}, ensure_ascii=False, indent=2))
    fills["_paths"] = {"fills": fp_fills, "report": fp_rep}
    return fills


# ════════════════════════ 买不进替补 · 补单轮留痕合并（2026-10-16 生产施工） ════════════════════════

def merge_backfill_fills(book_dir: str, date: str, round_no: int, round_res: dict,
                         placed=None, shift_from=None, log=print):
    """把一轮**补单（顺移）**结果并入 `fills_{date}.json`。返回归一化后的本轮 buys 记录列表；
    fills 文件缺失/读坏 ⇒ None（**不新建**——首轮都没跑就没有可合并的底账）。

    做什么：
      · 本轮下单记录追加进 `buys`，带 `round`（1 起）/`shift_from`（本轮撤单确认释放的 code）
        两个留痕字段——首轮记录**没有**这两键，以此区分「10:00 首单」与「顺移补单」；
      · 本轮撤单/额度/跳过明细记进 `backfill_rounds`；
      · summary 按合并后的 buys 全量重算（`_buys_summary`，与首轮同一口径）；
      · **幂等**：同 `round_no` 重入 ⇒ 先剔除该轮旧记录再写入，不会重复追加
        （钩子侧本就"每轮每日一次"，这里是进程重启/重放时的第二道保险）。
    不做什么：不碰 `picks`/`spares`（那是 10:00 首单的证据快照）；不写持仓账本
    （那是 `register_buys_to_book` 的事，调用方自行决定）。
    """
    fp = os.path.join(book_dir, f"fills_{date}.json")
    if not os.path.exists(fp):
        log(f"[WS] ⚠️ 补单轮留痕：{fp} 不存在（首轮 fills 缺失）⇒ 跳过合并")
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            fills = json.load(f)
    except Exception as e:
        log(f"[WS] ⚠️ 补单轮留痕：fills 读坏（跳过合并）: {e}")
        return None

    rn = int(round_no)
    shift = [str(c) for c in (shift_from or []) if c]
    recs = []
    for p in (placed or []):
        qty = int(p.get("qty") or p.get("qty_placed") or 0)
        filled = int(p.get("filled_qty") or 0)
        # 以执行器原始记录为底，intent_id、真实限价、VWAP/成交额与 breach 全部保留；
        # 这里只补留痕字段，不再用展示价或参考价重算执行事实。
        rec = dict(p)
        rec.update({"code": p.get("code"),
                    "symbol": p.get("symbol") or gm_symbol_of(str(p.get("code") or "")),
                    "side": "BUY", "gap": p.get("gap"), "price": p.get("price"),
                    "qty_target": int(p.get("qty_target") or qty), "qty_placed": qty,
                    "truncated": bool(p.get("truncated")),
                    "defer_reason": p.get("defer_reason"), "status": p.get("status"),
                    "filled_qty": filled, "filled_price": p.get("filled_price"),
                    "message": p.get("message") or "", "order_id": p.get("order_id"),
                    "qty_remaining": int(p.get("qty_remaining") or max(0, qty - filled)),
                    "round": rn, "shift_from": shift})
        if "booked_qty" not in rec:
            rec["booked_qty"] = ((filled or qty)
                                 if p.get("status") not in _TERMINAL_BAD else 0)
        if "fee_est" not in rec:
            eff_px = p.get("filled_price") or p.get("price") or 0
            rec["fee_est"] = round(float(filled or qty) * float(eff_px or 0) * _FEE_BUY, 2)
        recs.append(rec)

    buys = [b for b in (fills.get("buys") or []) if int(b.get("round") or 0) != rn]
    buys.extend(recs)
    fills["buys"] = buys

    rounds = [r for r in (fills.get("backfill_rounds") or []) if int(r.get("round") or 0) != rn]
    _cr = (round_res or {}).get("cancel") or {}
    rounds.append({"round": rn,
                   "cancel": {"released": _cr.get("released") or [],
                              "unconfirmed": _cr.get("unconfirmed") or [],
                              "failed": _cr.get("failed") or [],
                              "race_filled": _cr.get("filled") or []},
                   "cash_budget": (round_res or {}).get("cash_budget"),
                   "shift_from": shift,
                   "placed": [{"code": r["code"], "qty": r["qty_placed"], "status": r["status"]}
                              for r in recs],
                   "skipped": (round_res or {}).get("skipped") or [],
                   "note": (round_res or {}).get("note") or ""})
    rounds.sort(key=lambda r: int(r.get("round") or 0))
    fills["backfill_rounds"] = rounds

    # summary 全量重算（candidates/cash_before 是首轮现场，保留原值）
    summary = dict(fills.get("summary") or {})
    summary.update(_buys_summary(buys))
    summary["backfill_rounds_done"] = len(rounds)
    fills["summary"] = summary

    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(fills, ensure_ascii=False, indent=2))
    os.replace(tmp, fp)
    fp_rep = os.path.join(book_dir, f"exec_report_{date}.json")
    try:
        with open(fp_rep, "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": str(date), "mode": fills.get("mode"),
                                "summary": summary, "fills_file": fp},
                               ensure_ascii=False, indent=2))
    except Exception:
        pass
    log(f"[WS] 补单第{rn}轮留痕：fills 合并 buys+{len(recs)}（shift_from={shift or '-'}）⇒ "
        f"buys_placed={summary.get('buys_placed')}")
    return recs
