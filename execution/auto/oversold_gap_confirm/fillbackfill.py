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

## 2026-10-21 委托终态回填（apply_reject）

根因：宿主原拒单分支（`gm_main.on_order_status` status∈4/5/6/8/12）只写 write_reject/
write_risk，从不回填 OSC——而买侧「挂单即入账」，委托未成交即终态 ⇒ 账本虚挂 +
fills 留痕永远 submitted + summary `rejected_count=0` 虚报（10-21 002591/688028 实证）。
`apply_reject(book_dir, event)` 对称于 `apply_fill`：按原始状态把留痕分别修为
rejected/cancelled/expired（summary 按 `_buys_summary` 重算即自动正确）、BUY 时把该笔
「qty_placed@委托价」份额从账本剔除（加权反算还原旧成本）、SELL 时账本不动（卖侧
下单不扣账，无账可回滚）。status=6 是待撤在途而非终态，直接跳过且不释放任何份额。

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
    """读取 UTF-8 JSON；缺失、损坏或读取失败时返回 None。"""
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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


def reconcile_day_state(book_dir: str, date: str, fills=None):
    """以最新 fills 重建 day_state.pending，并同步真实 ID、已成交量和剩余量。

    只有磁盘上的 fills 才是本函数的真源；day_state 不存在时只返回对账结果，不凭空
    创建首轮状态。补单钩子在每轮前也调用它，因此 `no_open_order` 本身从不清 pending。
    """
    from . import backfill
    fresh = fills if isinstance(fills, dict) else _load_json(
        os.path.join(book_dir, "fills_%s.json" % date))
    if not isinstance(fresh, dict):
        return None
    pending = backfill.pending_from_fills(fresh)
    fp = os.path.join(book_dir, "day_state_%s.json" % date)
    state = _load_json(fp)
    if isinstance(state, dict):
        state["pending"] = pending
        _atomic_dump(state, fp)
    return {"fills": fresh, "pending": pending, "day_state": state}


# ════════════════════════ 留痕修正（fills_/sells_ json） ════════════════════════

def _find_unconfirmed_buy_rec(fills: dict, code: str, order_id=None,
                              include_priceless_race=False):
    """找对应未全成买单；真实 ID 优先精确匹配，旧空 ID 记录可被真实回报认领。

    无价撤单竞态已写成 filled_by_event 且数量可能已全成；带价真实 fill 到达时，
    include_priceless_race=True 允许重新找到该记录补价，而不是误判重复后兜底加仓。
    同步路径的 status=filled 无价记录不在此列，仍按既有幂等守卫跳过。
    """
    candidates = []
    for rec in reversed(fills.get("buys") or []):
        if str(rec.get("code")) != str(code):
            continue
        if int(rec.get("qty_placed") or 0) <= 0:
            continue
        if rec.get("status") in _BAD_STATUS:
            continue
        is_full = int(rec.get("filled_qty") or 0) >= int(rec.get("qty_placed") or 0)
        priceless_race = (include_priceless_race and is_full
                          and rec.get("status") == "filled_by_event"
                          and not rec.get("filled_price"))
        if is_full and not priceless_race:
            continue
        candidates.append(rec)
    if order_id:
        wanted = str(order_id)
        for rec in candidates:
            if str(rec.get("order_id") or rec.get("id") or "") == wanted:
                return rec
        for rec in candidates:
            if not str(rec.get("order_id") or rec.get("id") or ""):
                return rec
        return None
    return candidates[0] if candidates else None


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
    rec = _find_unconfirmed_buy_rec(
        fills, code, order_id,
        include_priceless_race=price is not None and float(price) > 0)
    if rec is None:
        return None
    rec["status"] = "filled_by_event"
    # 事件 qty 是该委托的成交总量（status==3 全成）⇒ 覆盖到总量而非累加；
    # 有真实成交价才写价，撤单竞态回报缺价时保留 null，绝不编造。
    rec["filled_qty"] = max(int(rec.get("filled_qty") or 0), int(qty))
    if price is not None and float(price) > 0:
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


def _find_sell_rec(book_dir: str, date: str, code: str, order_id=None):
    """在当日 sells_morning_/sells_close_ 里找有效卖单留痕。

    有真实 order_id 时精确匹配；仅旧空 ID 留痕允许被真实回报认领，绝不认领另一
    真实 ID。返回 (fills, fp, rec)；无命中 → (None, None, None)。**只读不改**。
    """
    wanted = str(order_id or "")
    for tag in ("morning", "close"):
        fp = os.path.join(book_dir, "sells_%s_%s.json" % (tag, date))
        fills = _load_json(fp)
        if not isinstance(fills, dict):
            continue
        hit = None
        blank = None
        for rec in reversed(fills.get("sells") or []):
            if str(rec.get("code")) != str(code):
                continue
            if int(rec.get("qty_placed") or 0) <= 0:
                continue
            if rec.get("status") in _BAD_STATUS:
                continue
            if wanted:
                existing = str(rec.get("order_id") or rec.get("id") or "")
                if existing == wanted:
                    return fills, fp, rec
                if not existing and blank is None:
                    blank = rec
                continue
            hit = rec
        if wanted and blank is not None:
            return fills, fp, blank
        if not wanted and hit is not None:
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


def _fix_sell_open_after(book_dir: str, date: str, code: str, open_after: int, log) -> None:
    """卖出账本更新后补写终态持仓数；重读文件以保留已落盘的成交确认字段。"""
    fills, fp, hit = _find_sell_rec(book_dir, date, code)
    if hit is None:
        return
    try:
        summary = dict(fills.get("summary") or {})
        summary["open_after"] = int(open_after)
        fills["summary"] = summary
        _atomic_dump(fills, fp)
    except Exception as e:
        log("[FB] ⚠️ sells open_after 落盘失败（账本已修）: %s" % e)


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
        if side == "SELL":
            _fix_sell_open_after(book_dir, date, code, len(open_pos), log)
        _atomic_dump(book, os.path.join(book_dir, executor.BOOK_FILE))

    result["key"] = key
    # fills 已落盘后立刻同步当日 pending：后续补单轮只以最新留痕判定是否仍需撤单。
    reconcile_day_state(book_dir, date)
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
    rec = _find_unconfirmed_buy_rec(fills, code, order_id, include_priceless_race=True)
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


def _terminal_semantics(status, detail=""):
    """原始柜台状态 → 留痕语义；6 是待撤而非终态，绝不能进入本映射。"""
    try:
        raw = int(status)
    except (TypeError, ValueError):
        return None
    table = {
        4: ("rejected", "柜台拒单 status=4", "reject_confirmed_at"),
        8: ("rejected", "柜台拒单 status=8", "reject_confirmed_at"),
        5: ("cancelled", "委托已撤销 status=5", "cancel_confirmed_at"),
        12: ("expired", "委托已过期 status=12", "expire_confirmed_at"),
    }
    sem = table.get(raw)
    if sem is None:
        return None
    terminal, message, confirmed_field = sem
    if detail:
        message += " " + str(detail)
    return terminal, message, confirmed_field


def _reject_buy_fills_file(book_dir: str, date: str, code: str,
                           status, rej_detail: str, order_id: str, log):
    """按原始状态修正买单留痕（拒单/撤单/过期），重算 summary 并同步 exec_report。"""
    sem = _terminal_semantics(status, rej_detail)
    if sem is None:
        return None
    terminal, message, confirmed_field = sem
    fp = os.path.join(book_dir, "fills_%s.json" % date)
    fills = _load_json(fp)
    if not isinstance(fills, dict):
        return None
    rec = _find_unconfirmed_buy_rec(fills, code, order_id)
    if rec is None:
        return None                       # 孤儿/手工单或留痕已终态（rejected/全成）
    if str(rec.get("status")) in _CONFIRMED_STATUS:
        log("[FB] BUY 终态 %s：留痕已 %s（成交与终态矛盾）⇒ 语义守卫跳过"
            % (code, rec.get("status")))
        return None
    rec["status"] = terminal
    rec["message"] = message
    rec[confirmed_field] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec["qty_remaining"] = 0
    if order_id:
        rec["order_id"] = str(order_id)
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
        log("[FB] ⚠️ 终态 fills 留痕落盘失败（账本照修）: %s" % e)
    return rec


def _reject_sell_fills_file(book_dir: str, date: str, code: str,
                            status, rej_detail: str, order_id: str, log) -> bool:
    """按原始状态修正卖单留痕；账本不动（卖侧下单时不扣账）。"""
    sem = _terminal_semantics(status, rej_detail)
    if sem is None:
        return False
    terminal, message, confirmed_field = sem
    fills, fp, hit = _find_sell_rec(book_dir, date, code, order_id)
    if hit is None:
        return False                        # 孤儿/手工单或留痕已终态
    if str(hit.get("status")) in _CONFIRMED_STATUS \
            or int(hit.get("filled_qty") or 0) > 0:
        log("[FB] SELL 终态 %s：留痕已 %s（成交与终态矛盾）⇒ 语义守卫跳过"
            % (code, hit.get("status")))
        return False
    hit["status"] = terminal
    hit["message"] = message
    hit[confirmed_field] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hit["qty_remaining"] = 0
    if order_id:
        hit["order_id"] = str(order_id)
    try:
        summary = dict(fills.get("summary") or {})
        summary["rejected_count"] = sum(
            1 for r in (fills.get("sells") or [])
            if r.get("status") in _BAD_STATUS or r.get("status") == "error")
        fills["summary"] = summary
        _atomic_dump(fills, fp)
    except Exception as e:
        log("[FB] ⚠️ 终态 sells 留痕落盘失败: %s" % e)
    return True


def apply_reject(book_dir: str, event: dict, log=print) -> dict | None:
    """把一条委托终态事件回填：4/8→rejected、5→cancelled、12→expired，并重算 summary。
    BUY 时回滚账本虚挂份额；status=6 待撤在途，跳过且不释放。函数名为兼容既有调用保留。
    幂等；全程 fail-open（单条失败 log 后继续，由调用方兜底）。

    event 字段（宿主终态分支构造）：
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
    sem = _terminal_semantics(status, rej_detail)
    if sem is None:
        return {"action": "skipped",
                "reason": "pending_cancel" if str(status) == "6" else "unsupported_status",
                "code": code, "side": side, "qty": qty}
    terminal = sem[0]

    key = reject_key({"time": ts, "code": code, "side": side, "qty": qty})

    # ── 幂等①：日志键去重 ──
    if key in _journal_keys(book_dir):
        return {"action": "skipped", "reason": "duplicate_key", "key": key,
                "code": code, "side": side}

    book = executor.load_book(book_dir)
    open_pos = book.setdefault("open", {})
    book_dirty = False

    if side == "SELL":
        if _reject_sell_fills_file(book_dir, date, code, status, rej_detail, order_id, log):
            log("[FB] SELL 终态 %s %d股：留痕→%s（账本不动，卖侧下单不扣账）"
                % (code, qty, terminal))
            result = {"action": "sell_" + terminal, "code": code, "qty": qty,
                      "detail": "卖单留痕→%s；账本不动" % terminal}
        else:
            result = {"action": "skipped", "reason": "semantic_guard",
                      "code": code, "side": side, "qty": qty,
                      "detail": "无匹配在途卖单留痕（孤儿/手工单或已终态）"}
    else:
        rec = _reject_buy_fills_file(book_dir, date, code, status, rej_detail, order_id, log)
        if rec is None:
            result = {"action": "skipped", "reason": "semantic_guard",
                      "code": code, "side": side, "qty": qty,
                      "detail": "无匹配在途买单留痕（孤儿/手工单或已终态）"}
        else:
            placed = int(rec.get("qty_placed") or 0)
            filled = min(placed, max(0, int(rec.get("filled_qty") or 0)))
            try:
                # booked_qty 是 register 实际并入账本的委托份额：初始 submitted 通常
                # 为 qty_placed，同步 partial 则仅为 filled_qty。只能回滚其中未成交部分。
                booked = min(placed, max(0, int(rec.get("booked_qty"))))
            except (TypeError, ValueError):
                # 旧留痕没有 booked_qty，无法区分「全额虚挂后部分成交」与「同步 partial
                # 只入账真实成交」。有成交时保守按 booked=filled ⇒ 不回滚，宁可留下
                # 待对账份额也绝不删真实成交/旧仓；零成交仍按旧口径回滚 qty_placed。
                booked = filled if filled > 0 else placed
            rollback_qty = max(0, booked - filled)
            rec_px = float(rec.get("price") or 0)
            action_prefix = "buy_" + terminal
            entry = open_pos.get(code)
            if entry is None:
                # 账本已无该 code（卖前 reconcile 已夹紧）⇒ no-op，留痕已修
                log("[FB] BUY 终态 %s %d股：留痕→%s；账本已无该票"
                    "（reconcile 已夹紧）⇒ 账本 no-op" % (code, rollback_qty, terminal))
                result = {"action": action_prefix + "_book_missing", "code": code,
                          "qty": rollback_qty,
                          "detail": "留痕→%s；账本已无条目 no-op" % terminal}
            elif rollback_qty == 0:
                # 同步 partial 已只按真实成交入账，或旧留痕归属不明采用保守兼容：
                # 留痕进入终态，但账本数量与加权成本均保持不动。
                log("[FB] BUY 终态 %s：booked=%d filled=%d ⇒ 无需回滚，保留真实成交"
                    % (code, booked, filled))
                result = {"action": action_prefix + "_no_rollback", "code": code,
                          "qty": 0,
                          "detail": "booked_qty=%d，filled_qty=%d，账本不动" % (booked, filled)}
            else:
                # 只剔除实际入账份额中的未成交余量；剩余份额按原加权成本反算，
                # 保护旧持仓与已成交部分。
                oq = int(entry.get("qty") or 0)
                opx = float(entry.get("entry_price") or 0)
                nq = oq - rollback_qty
                book_dirty = True
                if oq < rollback_qty:
                    # 异常态：账本数量 < 未成交余量（外部夹紧过）⇒ 删条目并警示
                    open_pos.pop(code, None)
                    log("[FB] ⚠️ BUY 终态 %s：账本数量 %d < 未成交余量 %d（异常态）⇒ 删条目"
                        % (code, oq, rollback_qty))
                    result = {"action": action_prefix + "_removed_abnormal", "code": code,
                              "qty": rollback_qty,
                              "detail": "账本 %d < 未成交余量 %d（异常），删条目"
                                        % (oq, rollback_qty)}
                elif nq == 0:
                    open_pos.pop(code, None)
                    log("[FB] BUY 终态 %s %d股@%s：整条虚挂剔除"
                        % (code, rollback_qty, rec_px))
                    result = {"action": action_prefix + "_removed", "code": code,
                              "qty": rollback_qty,
                              "detail": "open %d→0，剔除条目" % oq}
                else:
                    remaining_notional = opx * oq - rec_px * rollback_qty
                    # 留痕仍按整笔委托价入账时，已成交部分也要换成真实成交价；
                    # 否则只回滚余量会把 partial 的真实 fill 错留成委托价。
                    if filled > 0 and rec.get("filled_price"):
                        remaining_notional += filled * (float(rec["filled_price"]) - rec_px)
                    npx = remaining_notional / nq
                    entry["qty"] = nq
                    entry["entry_price"] = round(npx, 4)
                    log("[FB] BUY 终态 %s 未成交 %d股@%.4f：账本 %d→%d，"
                        "entry_price %.4f→%.4f（保护剩余份额成本）"
                        % (code, rollback_qty, rec_px, oq, nq, opx, npx))
                    result = {"action": action_prefix + "_unweighted", "code": code,
                              "qty": rollback_qty,
                              "detail": "账本 %d→%d，entry_price %.4f→%.4f"
                                        % (oq, nq, opx, npx)}

    if book_dirty:
        _atomic_dump(book, os.path.join(book_dir, executor.BOOK_FILE))

    result["key"] = key
    reconcile_day_state(book_dir, date)
    _journal_append(book_dir, {**result,
                               "event": {"time": ts, "code": code, "side": side,
                                         "qty": qty, "status": status,
                                         "order_id": order_id,
                                         "rej_detail": rej_detail},
                               "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return result


def apply_cancelled(book_dir: str, date: str, code: str, order_id="", log=print):
    """补单轮 released：走同一 status=5 终态链，留痕与未成交余量回滚一次完成。

    后续宿主 apply_reject(status=5) 或进程重放只看到 cancelled 留痕，语义守卫会跳过，
    因而不会双扣；重启后 pending_from_fills 也不会让该委托复活。
    """
    date = str(date)
    code = executor.code_of(code) or str(code or "")
    order_id = str(order_id or "")
    fills = _load_json(os.path.join(book_dir, "fills_%s.json" % date)) or {}
    rec = _find_unconfirmed_buy_rec(fills, code, order_id)
    if rec is None:
        reconcile_day_state(book_dir, date)
        return {"action": "skipped", "reason": "semantic_guard", "code": code}
    result = apply_reject(book_dir, {
        "time": "%s %s" % (date, datetime.now().strftime("%H:%M:%S")),
        "code": code, "side": "BUY", "qty": int(rec.get("qty_placed") or 0),
        "status": 5, "order_id": order_id, "rej_detail": ""}, log=log)
    if isinstance(result, dict):
        result["order_id"] = order_id
    return result


def apply_race_fill(book_dir: str, date: str, code: str, qty: int,
                    price=None, order_id="", log=print):
    """撤单竞态成交回填：有价走完整成交链；无价只修真实数量，绝不编造价格。"""
    code = executor.code_of(code) or str(code or "")
    try:
        qty = int(qty or 0)
        px = float(price) if price is not None else 0.0
    except (TypeError, ValueError):
        return None
    if not code or qty <= 0:
        return None
    if px > 0:
        return apply_fill(book_dir, {
            "time": "%s %s" % (date, datetime.now().strftime("%H:%M:%S")),
            "code": code, "side": "BUY", "qty": qty, "price": px,
            "order_id": str(order_id or "")}, log=log)
    rec = _fix_buy_fills_file(book_dir, str(date), code, qty, None, None,
                              str(order_id or ""), log)
    reconcile_day_state(book_dir, str(date))
    if rec is None:
        return {"action": "skipped", "reason": "semantic_guard", "code": code}
    return {"action": "buy_filled_without_price", "code": code, "qty": qty,
            "order_id": str(order_id or ""), "detail": "真实成交价缺失，filled_price 保持空"}
