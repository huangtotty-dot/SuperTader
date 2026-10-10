# -*- coding: utf-8 -*-
r"""weak_strong_executor.py — 弱转强 · 10:00 买侧执行器（2026-10-10 施工，WS2 生产接线）。

信号（冻结，见 core/weak_strong.py）：日线超跌 `昨收<MA20` + 盘中 10:00 跳空高开守住
（10:00 棒 low≥昨收 且 close>昨收 且 close>VWAP）⇒ 10:00 收盘买入，按 gap 降序取 top-N。

**买侧**：候选(盘前超跌)+10:00 棒 → 决策核 → top-N → 下单。
**卖侧（2026-10-10 接线，exp19 owner 拍板 SL5只深）**：持仓账本 `positions.json` 记
entry_date/entry_price/dev20/gap/board；每日收盘检查——深超跌(dev20<−7%)挂 −5% close 止损、
浅/中不挂、满 5 个交易日到期平仓（死拿5天 horizon）。卖出侧「次日早盘冲高卖」是下一步。

与 S1 共用掘金模拟盘账户 ⇒ 本执行器**不带独立净值链**（避免与 S1 账本双记），只落
fills/exec_report；每日名义总额度由 `weak_strong_daily_budget` 封顶，避免挤占 S1 轮动。

**不 import gm.api**——券商/仿真通道全部经注入的 Gateway 鸭子类型（与 s1_executor 同款，
gm_main 直接复用 `_S1GmGateway`），因此 gm_main / 干跑脚本 / 单测共用同一套执行逻辑。

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
import os
import sys
import time
from datetime import datetime

_ROOT = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from core.cost_model import FEE_BUY_STOCK as _FEE_BUY, FEE_SELL_STOCK as _FEE_SELL
    _FEE_SRC = "core/cost_model.py"
except Exception:                                    # pragma: no cover - 兜底
    _FEE_BUY, _FEE_SELL = 0.0000954, 0.0005954
    _FEE_SRC = "fallback_constants(=core/cost_model 同值)"

BOOK_DIR_DEFAULT = os.path.join(_ROOT, "t_io", "state", "weak_strong_book")

DEFAULTS = {
    "top_n": 4,                     # 每日按 gap 取最强前 N（exp9 Sharpe 顶点）
    "single_budget": 100000.0,      # 单票名义上限（元，对齐 OGR 10 万/腿）
    "daily_budget": 400000.0,       # 每日名义总额度（元，防挤占 S1 轮动）
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


# ════════════════════════ 决策（委托 core/weak_strong.py） ════════════════════════

def evaluate_picks(prev_close: dict, ma20: dict, bars: dict,
                   board: dict | None = None, allowed_boards=None,
                   top_n: int = DEFAULTS["top_n"]) -> list:
    """决策核入口：返回 picks 列表（[{code,gap,board,...}]，gap 降序、板块已过滤、top-N 已取）。"""
    try:
        import importlib.util as _ilu
        _p = os.path.join(_ROOT, "core", "weak_strong.py")
        _spec = _ilu.spec_from_file_location("weak_strong", _p)
        _m = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
    except Exception:                                    # pragma: no cover
        return []
    r = _m.evaluate(prev_close, ma20, bars, board=board,
                    allowed_boards=allowed_boards, top_n=top_n)
    return list(r["picks"])


# ════════════════════════ 计划构建（纯函数，可单测） ════════════════════════

def build_buy_plan(picks: list, positions: dict, cash_budget: float,
                   single_budget: float, min_lot: int = 100,
                   cash_headroom: float = 0.95) -> list:
    """按 gap 顺序（picks 已排序）折算买入量并做现金截断。纯函数。

    返回 [{code, gap, price, qty_target, qty_place, truncated, defer_reason, cash_left}]。
    """
    plan, cash_left = [], float(cash_budget)
    for p in picks:
        code = p["code"]
        px = float((p.get("price") or p.get("close") or 0))
        gap = p.get("gap")
        rec = {"code": code, "gap": gap, "price": px, "qty_target": 0,
               "qty_place": 0, "truncated": False, "defer_reason": None,
               "cash_left": None}
        held_qty = max(0, int((positions.get(code) or {}).get("qty") or 0))
        avail = max(0, int((positions.get(code) or {}).get("available") or 0))
        if not (px > 0):
            rec["defer_reason"] = "no_price(10:00收盘缺失)"
            plan.append(rec)
            continue
        if held_qty > 0 and avail <= 0:
            rec["defer_reason"] = "t1_locked(当日已持无可卖,加仓暂缓)"
            plan.append(rec)
            continue
        cap = min(single_budget, cash_left * cash_headroom)
        qty_target = int(cap / px / min_lot) * min_lot
        rec["qty_target"] = qty_target
        afford = int(cash_left / px / min_lot) * min_lot
        qty_place = min(qty_target, afford)
        if qty_place < qty_target and qty_target >= min_lot:
            rec["truncated"] = True
        rec["qty_place"] = qty_place
        if qty_place < min_lot:
            rec["defer_reason"] = ("cash_insufficient(现金不足截断至0)"
                                   if qty_target >= min_lot else "below_min_lot(不足一手)")
        else:
            cash_left -= qty_place * px
        rec["cash_left"] = round(cash_left, 2)
        plan.append(rec)
    return plan


# ════════════════════════ 下单与轮询 ════════════════════════

_TERMINAL_BAD = {"rejected", "expired", "cancelled"}


def _place_and_poll(gateway, code, side, qty, price, poll_rounds, poll_sleep, sleep_fn, log):
    try:
        rep = gateway.place_order(code, side, qty, price)
    except Exception as e:
        return {"order_id": None, "status": "error", "filled_qty": 0,
                "filled_price": None, "message": f"place_order异常:{type(e).__name__}:{e}"}
    rep = {"order_id": None, "status": "submitted", "filled_qty": 0,
           "filled_price": None, "message": "", **(rep or {})}
    if rep["status"] in ("submitted", "partial") and rep.get("order_id") \
            and hasattr(gateway, "poll_order") and poll_rounds > 0:
        for _ in range(int(poll_rounds)):
            if rep["status"] not in ("submitted", "partial"):
                break
            try:
                sleep_fn(float(poll_sleep))
                rep2 = gateway.poll_order(rep["order_id"], code)
            except Exception as e:
                log(f"[WS] poll_order {code} 异常（保持上一状态）: {e}")
                break
            if rep2:
                rep = {**rep, **rep2}
    return rep


# ════════════════════════ 持仓账本（positions.json，卖侧真源） ════════════════════════

BOOK_FILE = "positions.json"


def load_book(book_dir: str = BOOK_DIR_DEFAULT) -> dict:
    """读弱转强持仓账本。缺文件/损坏 → 空账本（fail-safe 不抛）。"""
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


def register_buys_to_book(book_dir: str, date: str, buys_out: list, picks: list,
                          prev_close: dict, ma20: dict, log=print) -> dict:
    """把当日已成交买单并入持仓账本（卖侧真源）。拒单/废单不入账。返回合并后的账本。"""
    board_map = {p["code"]: p.get("board", "") for p in (picks or [])}
    book = load_book(book_dir)
    for r in (buys_out or []):
        if r.get("status") in _TERMINAL_BAD or r.get("status") == "error":
            continue
        qty = int(r.get("filled_qty") or r.get("qty_placed") or 0)
        if qty <= 0:
            continue
        code = r["code"]
        px = float(r.get("filled_price") or r.get("price") or 0)
        if px <= 0:
            continue
        pc = float(prev_close.get(code) or 0)
        ma = float(ma20.get(code) or 0)
        dev20 = (pc / ma - 1.0) if (pc > 0 and ma > 0) else None
        old = book["open"].get(code)
        if old and str(old.get("entry_date")) == str(date):
            oq = int(old.get("qty") or 0)
            opx = float(old.get("entry_price") or 0)
            nq = oq + qty
            book["open"][code] = {**old, "qty": nq,
                                  "entry_price": round((opx * oq + px * qty) / nq, 4)}
            log(f"[WS] 账本合并 {code}: 同日加仓 {oq}→{nq}股")
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
    """账本 ↔ 账户对账：外部卖出（S1 清仓等）→ 从账本移除/减量。原地改 open_pos。"""
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


def _advance_days_held(open_pos: dict, date: str) -> None:
    """每日一次推进 days_held（幂等；entry 当日不推进）。14:50 止损检查专用。"""
    for code, rec in open_pos.items():
        if rec.get("last_check_date") != date and str(rec.get("entry_date")) != str(date):
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
    return sells_out


def _finish_sell(date, mode, started, sells_out, open_pos, note, tag, book_dir, cfg) -> dict:
    """卖侧收尾：写 sells_{tag}_{date}.json + 摘要。返回 fills dict。"""
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_rej = sum(1 for r in sells_out if r["status"] in _TERMINAL_BAD or r["status"] == "error")
    notional = sum((r.get("filled_qty") or r.get("qty_placed") or 0)
                   * (r.get("filled_price") or r.get("price") or 0)
                   for r in sells_out if r["qty_placed"] > 0)
    summary = {"sells_planned": len(sells_out),
               "sells_placed": sum(1 for r in sells_out if r["qty_placed"] > 0),
               "sells_filled": sum(1 for r in sells_out if r.get("filled_qty") and r["filled_qty"] > 0),
               "rejected_count": n_rej,
               "stop_count": sum(1 for r in sells_out if r.get("reason") == "sl5_deep" and r["qty_placed"] > 0),
               "expired_count": sum(1 for r in sells_out if r.get("reason") == "horizon_expired" and r["qty_placed"] > 0),
               "pop_count": sum(1 for r in sells_out if r.get("reason") == "pop_sell" and r["qty_placed"] > 0),
               "open_after": len(open_pos),
               "notional_placed": round(notional, 2)}
    fills = {"date": str(date), "mode": mode, "started_at": started, "finished_at": finished,
             "strategy": "weak_strong", "note": note,
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
                  dry_run: bool = False, sleep_fn=time.sleep, log=print) -> dict:
    """弱转强 14:50 收盘止损/到期平仓卖侧主流程。返回 fills dict（同时落盘）。

    prices = {code: 当日现价}（调用方注入，14:50 批量取数）。
    """
    cfg = {**DEFAULTS, **(params or {})}
    mode = "dry" if dry_run else "live"
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    book = load_book(book_dir)
    open_pos = dict(book.get("open") or {})
    positions = _snapshot_positions(gateway)
    _reconcile_open(open_pos, positions, log)
    _advance_days_held(open_pos, date)

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
    """弱转强 次日早盘冲高卖主流程。返回 fills dict（同时落盘）。

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

def run_buy_exec(gateway, date: str, prev_close: dict, ma20: dict, bars: dict,
                 board: dict | None = None, allowed_boards=None,
                 book_dir: str = BOOK_DIR_DEFAULT, params: dict | None = None,
                 dry_run: bool = False, sleep_fn=time.sleep, log=print) -> dict:
    """弱转强 10:00 买侧执行主流程。返回 fills dict（同时落盘）。

    fail-closed：决策核因缺数据/min_pool 不足返回空 picks ⇒ 当日不下单（不抛异常）。
    """
    cfg = {**DEFAULTS, **(params or {})}
    lot = int(cfg["min_lot"])
    top_n = int(cfg["top_n"])
    single_budget = float(cfg["single_budget"])
    daily_budget = float(cfg["daily_budget"])
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "dry" if dry_run else "live"

    picks = evaluate_picks(prev_close, ma20, bars, board=board,
                           allowed_boards=allowed_boards, top_n=top_n)
    if not picks:
        log(f"[WS] {date} 无弱转强候选（fail-closed，当日不下单） pool_n={len(prev_close)}")
        return _write_fills(date, mode, started, [], [], 0.0, 0.0, cfg, book_dir,
                            note="no_picks")

    cash0 = float(gateway.get_cash() or 0)
    positions = {code_of(k) or str(k): {"qty": int((v or {}).get("qty") or 0),
                                        "available": int((v or {}).get("available") or 0),
                                        "cost": float((v or {}).get("cost") or 0)}
                 for k, v in (gateway.get_positions() or {}).items()}
    positions = {k: v for k, v in positions.items() if v["qty"] > 0}
    cash_budget = min(daily_budget, cash0)
    log(f"[WS] {date} 候选={len(picks)} 现金={cash0:.0f} 预算={cash_budget:.0f} "
        f"picks={[(p['code'], round(p.get('gap') or 0, 4)) for p in picks]}")

    # 买入参考价 = 10:00 收盘（信号入场价口径）
    for p in picks:
        b = bars.get(p["code"]) or {}
        p["price"] = p.get("price") or float(b.get("close") or 0)

    buy_plan = build_buy_plan(picks, positions, cash_budget, single_budget,
                              min_lot=lot, cash_headroom=float(cfg["cash_headroom"]))
    buys_out, total_placed = [], 0.0
    for b in buy_plan:
        code = b["code"]
        rec = {"code": code, "symbol": gm_symbol_of(code), "side": "BUY",
               "gap": b["gap"], "price": b["price"],
               "qty_target": b["qty_target"], "qty_placed": 0,
               "truncated": b["truncated"], "defer_reason": b["defer_reason"],
               "status": "skipped", "filled_qty": 0, "filled_price": None,
               "fee_est": 0.0, "message": ""}
        if b["defer_reason"] or b["qty_place"] <= 0:
            rec["message"] = b["defer_reason"] or "无需买入"
            buys_out.append(rec)
            continue
        rep = _place_and_poll(gateway, code, "BUY", b["qty_place"], b["price"],
                              cfg["order_poll_rounds"], cfg["order_poll_sleep_sec"],
                              sleep_fn, log)
        rec.update({"qty_placed": b["qty_place"], "status": rep.get("status"),
                    "filled_qty": int(rep.get("filled_qty") or 0),
                    "filled_price": rep.get("filled_price"),
                    "message": rep.get("message") or ""})
        eff_px = rec["filled_price"] or b["price"]
        rec["fee_est"] = round(float(rec["filled_qty"] or b["qty_place"]) * float(eff_px or 0) * _FEE_BUY, 2)
        if rep.get("status") in _TERMINAL_BAD or rep.get("status") == "error":
            log(f"[WS] ⚠️ BUY {code} {rep.get('status')}: {rec['message']}")
        else:
            total_placed += (rec["filled_qty"] or b["qty_place"]) * float(eff_px or 0)
        log(f"[WS] BUY {code} gap={b['gap'] if b['gap'] is not None else 'nan':.4f} "
            f"{b['qty_place']}股@{b['price']:.3f} → {rec['status']}"
            f"{' [truncated]' if b['truncated'] else ''} {rec['message']}")
        buys_out.append(rec)

    fills = _write_fills(date, mode, started, buys_out, picks, cash0, total_placed,
                         cfg, book_dir)
    if not dry_run:
        try:
            register_buys_to_book(book_dir, date, buys_out, picks, prev_close, ma20, log)
        except Exception as e:
            log(f"[WS] ⚠️ 买入入账失败（不阻断）: {e}")
    return fills


def _write_fills(date, mode, started, buys_out, picks, cash0, total_placed,
                 cfg, book_dir, note: str = "") -> dict:
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_rej = sum(1 for r in buys_out if r["status"] in _TERMINAL_BAD or r["status"] == "error")
    n_defer = sum(1 for r in buys_out if r["defer_reason"])
    summary = {"candidates": len(picks), "buys_planned": len(buys_out),
               "buys_placed": sum(1 for r in buys_out if r["qty_placed"] > 0),
               "rejected_count": n_rej, "deferred_count": n_defer,
               "notional_placed": round(total_placed, 2), "cash_before": round(cash0, 2)}
    fills = {"date": str(date), "mode": mode, "started_at": started, "finished_at": finished,
             "strategy": "weak_strong", "note": note or "",
             "picks": [{"code": p["code"], "gap": p.get("gap"),
                        "board": p.get("board", "")} for p in picks],
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
