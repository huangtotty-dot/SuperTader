# -*- coding: utf-8 -*-
"""fillbackfill.py — 超跌跳空确认 · **fill 事件驱动回填**（2026-10-19 任务B）。

## 根因（2026-10-19 复盘实证）

GM 仿真终端**同步下单回报**里 `order_id` 恒空、`filled_qty` 恒 0，真实成交是**异步事件**
（宿主 `gm_main.on_order_status` 收到 status==3 后写 `t_io/bridge/events_*.jsonl`）。
于是两条失真链：

  · **卖侧**：executor 下单后账本不剔除；10-16 加的迟确认兜底（按账户持仓变化轮询，
    窗口≈6s）在 10-19 601118（成交于窗口关闭后 1.4s）再度漏单 ⇒ 连续两天手工 RECONCILE。
  · **买侧**：`register_buys_to_book`「挂单即入账」按计划数+委托价入账，成交回报**从不**
    回写 ⇒ fills_2026-10-16/10-19 所有买单 filled_qty=0、order_id=""，
    601118 账本 entry_price=6.53（委托价）vs 真实成交 6.59。

## 方案

宿主成交回调（`gm_main.on_order_status` status==3，已过孤儿闸/防重闸）即调
`apply_fill(book_dir, event)`：

  · **卖出**：账本剔除/减量；全平则条目移入 `closed` 并补 `exit_date/exit_price/
    exit_qty/exit_reason/exit_fee`（沿用 10-16/10-19 手工 RECONCILE 的字段口径）；
    同时修正当日 `sells_morning_/sells_close_{date}.json` 里匹配的 submitted 留痕。
  · **买入**：以真实 `filled_qty/filled_price` **修正**入账（把 register 记下的
    「qty_placed@委托价」份额原地换成「成交qty@真实价」，数量与加权价都重算）；
    同时修正 `fills_{date}.json` 匹配买单留痕（status→filled_by_event 等）。

executor 的迟确认兜底**保留不删**（双保险）；两者叠加安全——见下「并发与幂等」。

## 2026-10-21 拒单回填（apply_reject）

根因：宿主拒单分支（`gm_main.on_order_status` status∈4/5/6/8/12）只写 write_reject/
write_risk，从不回填 OSC——而买侧「挂单即入账」，拒单 ⇒ 账本虚挂 + fills 留痕永远
submitted + summary `rejected_count=0` 虚报（10-21 002591/688028 实证）。
`apply_reject(book_dir, event)` 对称于 `apply_fill`：留痕 status→rejected（summary
按 `_buys_summary` 重算即自动正确）、BUY 时把该笔「qty_placed@委托价」份额从账本
剔除（加权反算还原旧成本）、SELL 时账本不动（卖侧下单不扣账，无账可回滚）。

## 并发与幂等

  · GM SDK 单一事件派发线程（2026-09-11 事故注释实证：on_bar 阻塞会让 fill 排队）⇒
    本回调**绝不**与 executor 钩子并发执行；回调跑时钩子已 `save_book` 完毕。
    即便如此，本模块每次 apply 都**重新从磁盘读账本**、写完 **tmp+os.replace 原子落盘**。
  · 幂等三层：① 应用日志 `fill_backfill_journal.jsonl` 事件唯一键去重；
    ② 语义守卫——卖出目标条目已不存在 / 买入匹配留痕已 filled ⇒ 直接跳过；
    ③ 迟确认先成交回填的场景下，本回调看到账本已剔除 ⇒ no-op（不会二次减量）。
  · 唯一键 = `日期|code|side|qty|成交价(4位)|回报时间`。同一事件重放（回调+轮询补记）
    键相同 ⇒ 跳过；两笔**参数全同**的真实独立成交靠 ② 的语义守卫兜底。

⚠️ 本模块**不 import gm.api**（仓规）；不读账户、不下单——纯账本/留痕修正。
⚠️ 仅 MODE_LIVE 接线（宿主侧闸）；回测/干跑不经过这里。
⚠️ 已知边界：A 股仿真市价单实为「全成或全拒」，买单多段部分成交时，首笔按
   「委托量→真实成交量」换量换价，后续分笔只修价不修量；数量残差由卖前
   `reconcile_book` 按账户夹紧兜底（与既有纪律一致）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from . import executor

JOURNAL_FILE = "fill_backfill_journal.jsonl"

# 与 executor._TERMINAL_BAD 同义（不 import 私有名，避免耦合内部常量演化）
_BAD_STATUS = {"rejected", "expired", "cancelled", "error"}


# ════════════════════════ 工具 ════════════════════════

def fill_key(event: dict) -> str:
    """事件唯一键（幂等锚）。"""
    ts = str(event.get("time") or "")
    try:
        px = "%.4f" % float(event.get("price") or 0)
    except (TypeError, ValueError):
        px = "0"
    return "%s|%s|%s|%s|%s|%s" % (
        ts[:10], str(event.get("code") or ""), str(event.get("side") or "").upper(),
        int(event.get("qty") or 0), px, ts)


def _atomic_dump(payload: dict, fp: str) -> None:
    """原子落盘（tmp + os.replace）——账本/留痕**必须**走这里（GUI/复盘可能正在读）。"""
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, fp)


def _load_json(fp: str):
    from core.utils import read_json
    return read_json(fp, None)


def _journal_keys(book_dir: str) -> set:
    """读应用日志的全部事件键（一年几百条量级，全量读无压力）。"""
    keys = set()
    fp = os.path.join(book_dir, JOURNAL_FILE)
    if not os.path.exists(fp):
        return keys
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    k = (json.loads(line) or {}).get("key")
                    if k:
                        keys.add(k)
                except Exception:
                    continue
    except Exception:
        pass
    return keys


def _journal_append(book_dir: str, rec: dict) -> None:
    """追加一条应用日志（失败静默——语义守卫是第二道保险）。"""
    try:
        fp = os.path.join(book_dir, JOURNAL_FILE)
        os.makedirs(book_dir, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


# ════════════════════════ 留痕修正（fills_/sells_ json） ════════════════════════

def _find_unconfirmed_buy_rec(fills: dict, code: str):
    """fills_{date}.json 里该 code 最新一笔「已下单未**全额**确认」的买单留痕。"""
    for rec in reversed(fills.get("buys") or []):
        if str(rec.get("code")) != str(code):
            continue
        if int(rec.get("qty_placed") or 0) <= 0:
            continue
        if rec.get("status") in _BAD_STATUS:
            continue
        if int(rec.get("filled_qty") or 0) >= int(rec.get("qty_placed") or 0):
            continue                      # 已全额确认（语义守卫：重复事件不再匹配）
        return rec
    return None


def _already_confirmed_buy_rec(fills: dict, code: str, qty: int, price: float) -> bool:
    """语义守卫②：存在同 code 且已确认数量 ≥ 本次事件的买单留痕 ⇒ executor 同步路径
    （或先前事件）已入账，跳过。

    价格条件放宽：同步成交（轮询 filled）时 `filled_price` 可能为 None
    （register 会回退用委托价入账，数量/价都已正确）⇒ 此时只按数量判定。
    """
    for rec in reversed(fills.get("buys") or []):
        if str(rec.get("code")) != str(code):
            continue
        _fq = int(rec.get("filled_qty") or 0)
        if _fq <= 0 or _fq < int(qty):
            continue
        _fp = rec.get("filled_price")
        if not _fp:                              # 同步成交无价 ⇒ 数量匹配即视为已入账
            return True
        try:
            if abs(float(_fp) - float(price)) < 1e-6:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _fix_buy_fills_file(book_dir: str, date: str, code: str, qty: int,
                        price: float, fee, order_id: str, log) -> dict | None:
    """修正 fills_{date}.json 里匹配的买单留痕；返回被修正的 rec（未命中/失败 → None）。"""
    fp = os.path.join(book_dir, "fills_%s.json" % date)
    fills = _load_json(fp)
    if not isinstance(fills, dict):
        return None
    rec = _find_unconfirmed_buy_rec(fills, code)
    if rec is None:
        return None
    rec["status"] = "filled_by_event"
    # 事件 qty 是该委托的成交总量（status==3 全成）⇒ 覆盖到总量而非累加；
    # 价取真实成交价（比同步路径的委托价回退更准）。
    rec["filled_qty"] = max(int(rec.get("filled_qty") or 0), int(qty))
    rec["filled_price"] = round(float(price), 4)
    if fee:
        rec["fee_actual"] = round(float(rec.get("fee_actual") or 0) + float(fee), 2)
    if order_id:
        rec["order_id"] = str(order_id)
    rec["qty_remaining"] = max(0, int(rec.get("qty_placed") or 0)
                               - int(rec["filled_qty"]))
    rec["fill_confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # summary 按合并口径全量重算（与首轮/补单轮同一函数），exec_report 同步
    try:
        summary = dict(fills.get("summary") or {})
        summary.update(executor._buys_summary(fills.get("buys") or []))
        summary["fills_event_confirmed"] = int(summary.get("fills_event_confirmed") or 0) + 1
        fills["summary"] = summary
    except Exception:
        pass
    try:
        _atomic_dump(fills, fp)
        _rep_fp = os.path.join(book_dir, "exec_report_%s.json" % date)
        _rep = _load_json(_rep_fp)
        if isinstance(_rep, dict):
            _rep["summary"] = fills.get("summary")
            _atomic_dump(_rep, _rep_fp)
    except Exception as e:
        log("[FB] ⚠️ fills 留痕修正落盘失败（账本已修）: %s" % e)
    return rec


def _find_sell_rec(book_dir: str, date: str, code: str):
    """在当日 sells_morning_/sells_close_ 里找该 code 最新一笔有效卖单留痕。
    返回 (fills, fp, rec)；无命中 → (None, None, None)。**只读不改**。"""
    for tag in ("morning", "close"):
        fp = os.path.join(book_dir, "sells_%s_%s.json" % (tag, date))
        fills = _load_json(fp)
        if not isinstance(fills, dict):
            continue
        hit = None
        for rec in reversed(fills.get("sells") or []):
            if str(rec.get("code")) != str(code):
                continue
            if int(rec.get("qty_placed") or 0) <= 0:
                continue
            if rec.get("status") in _BAD_STATUS:
                continue
            hit = rec
        if hit is not None:
            return fills, fp, hit
    return None, None, None


def _fix_sell_fills_file(book_dir: str, date: str, code: str, qty: int,
                         price: float, fee, log) -> str:
    """修正当日 sells_morning_/sells_close_ 里匹配的卖单留痕；返回卖出原因（无命中 → ''）。

    已**全额**确认的留痕不再累加（迟确认/同步成交/先前事件，哪个先到都行）。
    """
    fills, fp, hit = _find_sell_rec(book_dir, date, code)
    if hit is None:
        return ""
    if int(hit.get("filled_qty") or 0) >= int(hit.get("qty_placed") or 0):
        return str(hit.get("reason") or "")         # 已全额确认：只取原因，不再累加
    reason = str(hit.get("reason") or "")
    hit["status"] = "filled_by_event"
    # 事件 qty 是该委托的**成交总量**（status==3 全成）⇒ 覆盖到总量而非累加；
    # 价取真实成交价（executor 迟确认只能记估算价/委托价，此处是更准的真源）。
    hit["filled_qty"] = max(int(hit.get("filled_qty") or 0), int(qty))
    hit["filled_price"] = round(float(price), 4)
    if fee:
        hit["fee_actual"] = round(float(fee), 2)
    hit["fill_confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        summary = dict(fills.get("summary") or {})
        summary["sells_filled"] = sum(
            1 for r in (fills.get("sells") or [])
            if int(r.get("filled_qty") or 0) > 0)
        fills["summary"] = summary
        _atomic_dump(fills, fp)
    except Exception as e:
        log("[FB] ⚠️ sells 留痕修正落盘失败（账本已修）: %s" % e)
    return reason


# ════════════════════════ 主入口 ════════════════════════

def apply_fill(book_dir: str, event: dict, log=print) -> dict | None:
    """把一条 fill 事件回填进持仓账本（+ 修正当日留痕）。幂等；全程 fail-open 由调用方兜底。

    event 字段（与 events_*.jsonl 的 fill 行同构，宿主回调构造）：
        time / code / side∈{BUY,SELL} / qty / price / order_id / fee / fill_vwap
    返回动作描述 dict（跳过返回 {"action": "skipped", ...}；事件非法返回 None）。
    """
    code = executor.code_of(event.get("code")) or str(event.get("code") or "")
    side = str(event.get("side") or "").upper()
    ts = str(event.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    date = ts[:10]
    try:
        qty = int(event.get("qty") or 0)
        price = float(event.get("fill_vwap") or event.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if not code or side not in ("BUY", "SELL") or qty <= 0 or price <= 0 or len(date) != 10:
        return None
    fee = event.get("fee")
    order_id = str(event.get("order_id") or "")

    key = fill_key({**event, "code": code, "side": side, "qty": qty, "price": price,
                    "time": ts})

    book = executor.load_book(book_dir)
    open_pos = book.setdefault("open", {})

    # ── 幂等①：日志键去重 ──
    if key in _journal_keys(book_dir):
        return {"action": "skipped", "reason": "duplicate_key", "key": key,
                "code": code, "side": side}

    if side == "SELL":
        result = _apply_sell(book, open_pos, book_dir, date, code, qty, price, fee, log)
    else:
        result = _apply_buy(book, open_pos, book_dir, date, code, qty, price,
                            fee, order_id, log)

    if result is None:                      # 语义守卫判定为重复/无对象 ⇒ 不动账本
        result = {"action": "skipped", "reason": "semantic_guard",
                  "code": code, "side": side, "qty": qty, "price": price}
    else:
        _atomic_dump(book, os.path.join(book_dir, executor.BOOK_FILE))

    result["key"] = key
    _journal_append(book_dir, {**result, "event": {"time": ts, "code": code, "side": side,
                                                   "qty": qty, "price": price,
                                                   "order_id": order_id},
                                 "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return result


def _apply_sell(book, open_pos, book_dir, date, code, qty, price, fee, log):
    """卖出回填：剔除/减量 + closed 留痕 + sells json 修正。无需动作返回 None。

    ⚠️ 双保险去重（与 executor 迟确认/同步成交叠加安全）：
    fill 事件的 qty 是该委托的**成交总量**（status==3 全成 ⇒ = qty_placed）；
    executor 侧若已扣账（同步 filled 或迟确认 filled_by_position），会同步把
    sells 留痕 rec.filled_qty 写成已扣数量 ⇒ 本函数只回填 **差额** `qty − rec.filled_qty`，
    差额 ≤0 即视为「executor 已处理」，账本不动（防二次减量）。
    """
    _fills, _fp, rec = _find_sell_rec(book_dir, date, code)
    prior = int(rec.get("filled_qty") or 0) if rec else 0
    reason = str(rec.get("reason") or "") if rec else ""
    delta = int(qty) - prior
    if rec is not None and delta <= 0:
        log("[FB] SELL %s %d股@%.4f：executor 已扣账 %d 股（%s）⇒ 账本不动"
            % (code, qty, price, prior, rec.get("status")))
        return None
    entry = open_pos.get(code)
    if entry is None:
        # 迟确认兜底/先前事件已剔除 ⇒ 幂等跳过（不二次减量）
        log("[FB] SELL %s %d股@%.4f：账本已无该票（迟确认或先前事件已回填）⇒ 跳过"
            % (code, qty, price))
        return None
    _fix_sell_fills_file(book_dir, date, code, qty, price, fee, log)
    oq = int(entry.get("qty") or 0)
    nq = oq - delta
    if nq <= 0:
        open_pos.pop(code, None)
        closed = book.setdefault("closed", {})
        closed[code] = {**entry,
                        "exit_date": date, "exit_price": round(float(price), 4),
                        "exit_qty": oq, "exit_reason": reason or "event_backfill",
                        "exit_fee": (round(float(fee), 2) if fee else None),
                        "closed_by": "fill_event"}
        log("[FB] SELL %s %d股@%.4f 全平 ⇒ 账本剔除，入 closed（%s）"
            % (code, oq, price, reason or "event_backfill"))
        return {"action": "sell_closed", "code": code, "qty": oq, "price": price,
                "exit_reason": reason or "event_backfill",
                "detail": "open %d→0，入 closed" % oq}
    entry["qty"] = nq
    entry.setdefault("partial_exits", []).append(
        {"date": date, "qty": delta, "price": round(float(price), 4),
         "fee": (round(float(fee), 2) if fee else None),
         "reason": reason or "event_backfill"})
    log("[FB] SELL %s %d股@%.4f 部分回填（executor 已扣 %d）⇒ 账本 %d→%d"
        % (code, qty, price, prior, oq, nq))
    return {"action": "sell_reduced", "code": code, "qty": delta, "price": price,
            "exit_reason": reason or "event_backfill",
            "detail": "open %d→%d（executor 已扣 %d）" % (oq, nq, prior)}


def _apply_buy(book, open_pos, book_dir, date, code, qty, price, fee, order_id, log):
    """买入回填：以真实成交修正入账（换价/换量）+ fills json 修正。无需动作返回 None。"""
    fills_fp = os.path.join(book_dir, "fills_%s.json" % date)
    fills = _load_json(fills_fp) or {}
    rec = _find_unconfirmed_buy_rec(fills, code)
    if rec is None:
        # 语义守卫②：留痕已是 confirmed 且参数一致 ⇒ 重复事件，跳过
        if _already_confirmed_buy_rec(fills, code, qty, price):
            log("[FB] BUY %s %d股@%.4f：留痕已确认（重复事件）⇒ 跳过" % (code, qty, price))
            return None
        # 兜底合入：找不到对应委托留痕（异常路径，如账本缺失但真实成交）——
        # 按 register_buys_to_book 同款加权合并直接入账，标 created_by 供复盘追查。
        entry = open_pos.get(code)
        picks = {str(p.get("code")): p for p in (fills.get("picks") or [])}
        if entry:
            oq = int(entry.get("qty") or 0)
            opx = float(entry.get("entry_price") or 0)
            nq = oq + qty
            open_pos[code] = {**entry, "qty": nq,
                              "entry_price": round((opx * oq + price * qty) / nq, 4)}
            log("[FB] ⚠️ BUY %s %d股@%.4f 无匹配委托留痕 ⇒ 兜底加权合并 %d→%d"
                % (code, qty, price, oq, nq))
            return {"action": "buy_merged_fallback", "code": code, "qty": qty,
                    "price": price, "detail": "无匹配留痕，加权合并 %d→%d" % (oq, nq)}
        open_pos[code] = {
            "entry_date": date, "entry_price": round(float(price), 4), "qty": qty,
            "dev20": None, "gap": (picks.get(code) or {}).get("gap"),
            "board": (picks.get(code) or {}).get("board", ""),
            "days_held": 0, "last_check_date": None,
            "created_by": "fill_event_no_fills_rec"}
        log("[FB] ⚠️ BUY %s %d股@%.4f 无匹配委托留痕 ⇒ 兜底新建入账" % (code, qty, price))
        return {"action": "buy_created_fallback", "code": code, "qty": qty,
                "price": price, "detail": "无匹配留痕，新建入账"}

    placed = int(rec.get("qty_placed") or 0)
    rec_px = float(rec.get("price") or 0)
    _fix_buy_fills_file(book_dir, date, code, qty, price, fee, order_id, log)

    entry = open_pos.get(code)
    if entry and placed > 0 and int(entry.get("qty") or 0) >= placed:
        # 正常路径（单线程派发 ⇒ register「挂单即入账」先于本回调）：
        # 账本里该笔委托贡献 = qty_placed @ 委托价，原地换成 成交qty @ 真实价。
        oq = int(entry.get("qty") or 0)
        opx = float(entry.get("entry_price") or 0)
        nq = oq - placed + qty
        if nq > 0:
            np_ = (opx * oq - rec_px * placed + price * qty) / nq
            entry["qty"] = nq
            entry["entry_price"] = round(np_, 4)
            log("[FB] BUY %s %d股@%.4f 入账修正：qty %d→%d，entry_price %.4f→%.4f"
                % (code, qty, price, oq, nq, opx, np_))
        else:
            open_pos.pop(code, None)
            log("[FB] BUY %s 修正后数量为 0（异常）⇒ 剔除条目" % code)
        return {"action": "buy_corrected", "code": code, "qty": qty, "price": price,
                "detail": "qty %d→%d，entry_price %.4f→%s"
                          % (oq, nq, opx, entry and entry.get("entry_price"))}
    if entry is None:
        # 回调先于 register 的异常次序（进程重启等）⇒ 直接按真实成交新建；
        # register 稍后再跑会重复计量的残差，由卖前 reconcile_book 按账户夹紧兜底。
        picks = {str(p.get("code")): p for p in (fills.get("picks") or [])}
        open_pos[code] = {
            "entry_date": date, "entry_price": round(float(price), 4), "qty": qty,
            "dev20": None, "gap": (picks.get(code) or {}).get("gap"),
            "board": (picks.get(code) or {}).get("board", ""),
            "days_held": 0, "last_check_date": None,
            "created_by": "fill_event"}
        log("[FB] BUY %s %d股@%.4f 新建入账（回调先于 register 的异常次序）"
            % (code, qty, price))
        return {"action": "buy_created", "code": code, "qty": qty, "price": price,
                "detail": "账本无条目，按真实成交新建"}
    # entry 存在但数量 < qty_placed（账本被外部夹紧过）⇒ 数量以账户为准不动，只换价
    oq = int(entry.get("qty") or 0)
    opx = float(entry.get("entry_price") or 0)
    if oq > 0 and qty > 0:
        entry["entry_price"] = round((opx * max(0, oq - qty) + price * qty) / oq, 4)
    log("[FB] ⚠️ BUY %s 账本数量 %d < 委托量 %d（已被夹紧）⇒ 只修价不修量"
        % (code, oq, placed))
    return {"action": "buy_price_corrected", "code": code, "qty": qty, "price": price,
            "detail": "账本已被夹紧(qty=%d)，只修价" % oq}


# ════════════════════════ 拒单回填（2026-10-21） ════════════════════════

# 留痕上表示「已确认成交」的状态——拒单事件与它们矛盾 ⇒ 语义守卫跳过
_CONFIRMED_STATUS = {"filled", "filled_by_event", "filled_by_position"}


def reject_key(event: dict) -> str:
    """拒单事件唯一键（幂等锚）——side 加 REJECT_ 前缀，与同参数 fill 事件不撞键。"""
    side = str(event.get("side") or "").upper()
    return fill_key({**event, "side": "REJECT_" + side, "price": 0})


def _reject_buy_fills_file(book_dir: str, date: str, code: str,
                           status, rej_detail: str, log):
    """fills_{date}.json 匹配买单留痕 → rejected + summary 重算 + exec_report 同步。
    返回被修正的 rec；无命中/语义守卫 → None（只读不改）。"""
    fp = os.path.join(book_dir, "fills_%s.json" % date)
    fills = _load_json(fp)
    if not isinstance(fills, dict):
        return None
    rec = _find_unconfirmed_buy_rec(fills, code)
    if rec is None:
        return None                       # 孤儿/手工单或留痕已终态（rejected/全成）
    if str(rec.get("status")) in _CONFIRMED_STATUS:
        # 语义守卫：留痕已确认成交却来拒单（事件乱序/重复）⇒ 跳过，不得改回 rejected
        log("[FB] BUY 拒单 %s：留痕已 %s（成交与拒单矛盾）⇒ 语义守卫跳过"
            % (code, rec.get("status")))
        return None
    rec["status"] = "rejected"
    rec["message"] = "柜台拒单 status=%s %s" % (status, rej_detail)
    rec["reject_confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec["qty_remaining"] = 0
    # summary 按合并口径全量重算：status→rejected 后 buys_placed 自动减、rejected_count 自动加
    try:
        summary = dict(fills.get("summary") or {})
        summary.update(executor._buys_summary(fills.get("buys") or []))
        fills["summary"] = summary
    except Exception:
        pass
    try:
        _atomic_dump(fills, fp)
        _rep_fp = os.path.join(book_dir, "exec_report_%s.json" % date)
        _rep = _load_json(_rep_fp)
        if isinstance(_rep, dict):
            _rep["summary"] = fills.get("summary")
            _atomic_dump(_rep, _rep_fp)
    except Exception as e:
        log("[FB] ⚠️ 拒单 fills 留痕落盘失败（账本照修）: %s" % e)
    return rec


def _reject_sell_fills_file(book_dir: str, date: str, code: str,
                            status, rej_detail: str, log) -> bool:
    """sells_morning_/sells_close_ 匹配卖单留痕 → rejected + 该文件 summary 重算。
    返回是否修正。账本不动——卖侧 executor 下单时不扣账本，拒单无账可回滚。"""
    fills, fp, hit = _find_sell_rec(book_dir, date, code)
    if hit is None:
        return False                        # 孤儿/手工单或留痕已终态
    if str(hit.get("status")) in _CONFIRMED_STATUS \
            or int(hit.get("filled_qty") or 0) > 0:
        # 语义守卫：留痕已确认成交却来拒单 ⇒ 跳过（与 _fix_sell_fills_file 同纪律）
        log("[FB] SELL 拒单 %s：留痕已 %s（成交与拒单矛盾）⇒ 语义守卫跳过"
            % (code, hit.get("status")))
        return False
    hit["status"] = "rejected"
    hit["message"] = "柜台拒单 status=%s %s" % (status, rej_detail)
    hit["reject_confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hit["qty_remaining"] = 0
    try:
        summary = dict(fills.get("summary") or {})
        summary["rejected_count"] = sum(
            1 for r in (fills.get("sells") or [])
            if r.get("status") in _BAD_STATUS or r.get("status") == "error")
        fills["summary"] = summary
        _atomic_dump(fills, fp)
    except Exception as e:
        log("[FB] ⚠️ 拒单 sells 留痕落盘失败: %s" % e)
    return True


def apply_reject(book_dir: str, event: dict, log=print) -> dict | None:
    """把一条拒单事件回填：留痕 → rejected（summary 重算）+ BUY 时回滚账本虚挂份额。
    幂等；全程 fail-open（单条失败 log 后继续，由调用方兜底）。

    event 字段（宿主拒单分支构造）：
        time / code / side∈{BUY,SELL} / qty / order_id / status(原始数字码) / rej_detail
    返回动作描述 dict（跳过返回 {"action": "skipped", ...}；事件非法返回 None）。
    """
    code = executor.code_of(event.get("code")) or str(event.get("code") or "")
    side = str(event.get("side") or "").upper()
    ts = str(event.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    date = ts[:10]
    try:
        qty = int(event.get("qty") or 0)
    except (TypeError, ValueError):
        return None
    if not code or side not in ("BUY", "SELL") or qty <= 0 or len(date) != 10:
        return None
    status = event.get("status")
    rej_detail = str(event.get("rej_detail") or "")
    order_id = str(event.get("order_id") or "")

    key = reject_key({"time": ts, "code": code, "side": side, "qty": qty})

    # ── 幂等①：日志键去重 ──
    if key in _journal_keys(book_dir):
        return {"action": "skipped", "reason": "duplicate_key", "key": key,
                "code": code, "side": side}

    book = executor.load_book(book_dir)
    open_pos = book.setdefault("open", {})
    book_dirty = False

    if side == "SELL":
        if _reject_sell_fills_file(book_dir, date, code, status, rej_detail, log):
            log("[FB] SELL 拒单 %s %d股：留痕→rejected（账本不动，卖侧下单不扣账）"
                % (code, qty))
            result = {"action": "sell_rejected", "code": code, "qty": qty,
                      "detail": "卖单留痕→rejected；账本不动"}
        else:
            result = {"action": "skipped", "reason": "semantic_guard",
                      "code": code, "side": side, "qty": qty,
                      "detail": "无匹配在途卖单留痕（孤儿/手工单或已终态）"}
    else:
        rec = _reject_buy_fills_file(book_dir, date, code, status, rej_detail, log)
        if rec is None:
            result = {"action": "skipped", "reason": "semantic_guard",
                      "code": code, "side": side, "qty": qty,
                      "detail": "无匹配在途买单留痕（孤儿/手工单或已终态）"}
        else:
            placed = int(rec.get("qty_placed") or 0)
            rec_px = float(rec.get("price") or 0)
            entry = open_pos.get(code)
            if entry is None:
                # 账本已无该 code（卖前 reconcile 已夹紧）⇒ no-op，留痕已修
                log("[FB] BUY 拒单 %s %d股：留痕→rejected；账本已无该票"
                    "（reconcile 已夹紧）⇒ 账本 no-op" % (code, placed))
                result = {"action": "buy_rejected_book_missing", "code": code,
                          "qty": placed,
                          "detail": "留痕→rejected；账本已无条目 no-op"}
            else:
                # 关键：OSC 买侧「挂单即入账」，该 code 条目含本笔 qty_placed@委托价
                # 份额（可能被加权合并进旧持仓）⇒ 剔除并反算加权价还原旧成本。
                oq = int(entry.get("qty") or 0)
                opx = float(entry.get("entry_price") or 0)
                nq = oq - placed
                book_dirty = True
                if oq < placed:
                    # 异常态：账本数量 < 委托量（外部夹紧过）⇒ 删条目并警示
                    open_pos.pop(code, None)
                    log("[FB] ⚠️ BUY 拒单 %s：账本数量 %d < 委托量 %d（异常态）⇒ 删条目"
                        % (code, oq, placed))
                    result = {"action": "buy_rejected_removed_abnormal", "code": code,
                              "qty": placed,
                              "detail": "账本 %d < 委托 %d（异常），删条目" % (oq, placed)}
                elif nq == 0:
                    open_pos.pop(code, None)
                    log("[FB] BUY 拒单 %s %d股@%s：整条虚挂剔除" % (code, placed, rec_px))
                    result = {"action": "buy_rejected_removed", "code": code,
                              "qty": placed,
                              "detail": "open %d→0，剔除条目" % oq}
                else:
                    npx = (opx * oq - rec_px * placed) / nq
                    entry["qty"] = nq
                    entry["entry_price"] = round(npx, 4)
                    log("[FB] BUY 拒单 %s %d股@%.4f：账本 %d→%d，"
                        "entry_price %.4f→%.4f（还原旧成本）"
                        % (code, placed, rec_px, oq, nq, opx, npx))
                    result = {"action": "buy_rejected_unweighted", "code": code,
                              "qty": placed,
                              "detail": "账本 %d→%d，entry_price %.4f→%.4f"
                                        % (oq, nq, opx, npx)}

    if book_dirty:
        _atomic_dump(book, os.path.join(book_dir, executor.BOOK_FILE))

    result["key"] = key
    _journal_append(book_dir, {**result,
                               "event": {"time": ts, "code": code, "side": side,
                                         "qty": qty, "status": status,
                                         "order_id": order_id,
                                         "rej_detail": rej_detail},
                               "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return result
