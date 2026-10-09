# -*- coding: utf-8 -*-
r"""s1_executor.py — S1 终选选股策略 · 开盘执行器（2026-10-10 施工，W3 执行集成）。

owner 决策（2026-10-10）：研究线 S1 终选（N=4 槽 / M=8 调出缓冲 / min_hold=1 /
TP 臂 A / score_eq 等权复合分，卫生宇宙）2026-10-12 起直接驱动掘金**模拟盘**
auto 账本自动下单，无影子期。规则真源 = t_io/validation/factor_mining/s0_account/s1_sim.py
（s1_run_sim，只读）；本模块只负责「执行」——消费 W2 盘前产出的 picks json，
09:30 开盘后先卖后买，全部留痕。

picks schema（W2 产出，t_io/state/s1_book/picks/picks_{date}.json）::

    {
      "date":  "2026-10-12",
      "sells": [{"symbol": "600481", "qty": 1200, "reason": "rank_exit|TP_FIX|..."}],
      "buys":  [{"symbol": "300456", "target_weight": 0.25, "reason": "rank_enter r=1"}],
      "holds": [{"symbol": "688008", ...}],          # 继续持有（不动作）
      "meta":  {...}                                  # N/M/min_hold/tp_arm/score 等
    }

symbol 兼容 "600481" / "SHSE.600481" / "600481.SH"，模块内统一归一为 6 位裸码。

本模块**不 import gm.api**——券商/仿真通道全部经注入的 Gateway 鸭子类型，
因此 gm_main（MODE_LIVE）、干跑脚本、单元测试可共用同一套执行逻辑。

Gateway 接口（鸭子类型，全部必需，poll_order 可选）::

    get_cash() -> float                       # 可用现金（元）
    get_positions() -> {code: {"qty": int, "available": int, "cost": float}}
    get_open_price(code) -> float | None      # 当日开盘价；None=取价失败/停牌
    place_order(code, side, qty, price) -> dict
        side ∈ {"BUY","SELL"}；price 仅为留痕参考价（下单方式由 Gateway 决定，
        gm_main 侧沿用市价单风格）。返回：
        {"order_id": str|None, "status": "submitted"|"filled"|"partial"|"rejected",
         "filled_qty": int, "filled_price": float|None, "message": str}
    poll_order(order_id, code) -> dict        # 同上结构；可选，缺失则不轮询

执行语义（与 s1_run_sim 对齐的部分刻意保持简单——开盘价成交、回款当日可用）：
  ① 先卖：picks.sells ∪ 清仓计划（账户内不在 buys∪holds 名单的旧持仓，bootstrap
     账本切换用，owner 2026-10-10 拍板）；T+1 校验 qty→min(qty, available)，
      blocked 部分留痕（次日 picks 应重发，执行器只记录不擅自补单）。
  ② 后买：预算 = 期初现金 + Σ（未拒卖单 qty×开盘价×proceeds_haircut）；逐 rank
     顺序折算 target_weight×equity_est/开盘价 → 100 股整数倍；已持仓部分抵扣；
     现金不足按 rank 顺序截断并记录；卖单被拒自动从预算剔除（不透支）。
  ③ 废单/部分成交：place_order 返回 submitted/partial 时轮询 poll_rounds 次，
     终态全部落 fills；拒单不中断后续单。
  ④ 停牌/取价失败：买单顺延（deferred，记录，次日 picks 重评），卖单记录失败。

产物（全部幂等覆盖）：
  t_io/state/s1_book/fills_{date}.json        — 逐单留痕（真源）
  t_io/state/s1_book/exec_report_{date}.json  — 当日执行摘要
  t_io/metrics/equity_s1_daily_{date}.json    — S1 专属净值（α/β 记账钩，
      source 含「S1账本口径」，与 equity_daily.py 的「仿真账户口径」链完全隔离；
      exec-time 估计值，EOD 精确重估由后续脚本负责——见 doc/solutions/2026-10-10_S1执行集成.md）
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from datetime import datetime

# ── 成本真源（core/cost_model.py，只读 import；加载失败回退同值常量）──────────
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

_S1_SOURCE_TAG = "S1账本口径"
BOOK_DIR_DEFAULT = os.path.join(_ROOT, "t_io", "state", "s1_book")
METRICS_DIR_DEFAULT = os.path.join(_ROOT, "t_io", "metrics")

DEFAULTS = {
    "proceeds_haircut": 0.999,     # 卖出预计回款折扣（对齐 gm_main OPEN_ALIGN 的 ×0.999）
    "min_lot": 100,                # A 股最小交易单位
    "order_poll_rounds": 3,        # submitted/partial 轮询次数
    "order_poll_sleep_sec": 2.0,   # 轮询间隔（秒）
    "sell_retries": 1,             # 卖单拒单后同轮重试次数（保守默认 1 次重试）
}


class PicksError(Exception):
    """picks 文件缺失/过期/schema 非法 —— 调用方必须 fail-closed（当日不下单）。"""


# ════════════════════════ 符号与 picks 解析 ════════════════════════

def code_of(symbol) -> str:
    """任意符号写法 → 6 位裸码。无法解析返回 ""。"""
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    for sep in (".", "_"):
        if sep in s:
            parts = [p for p in s.split(sep) if p]
            # "SHSE.600481" / "600481.SH" / "SHSE_600481"
            for p in parts:
                if p.isdigit() and len(p) == 6:
                    return p
            return ""
    return s if (s.isdigit() and len(s) == 6) else ""


def gm_symbol_of(code: str) -> str:
    """6 位裸码 → GM 符号（与 gm_main._code_to_gm 同口径：5/6/9 沪市，其余深市）。"""
    return ("SHSE." if code[:1] in "569" else "SZSE.") + code


def _norm_entry(e, kind: str) -> dict:
    """归一 picks 条目：字符串 → {"symbol": s}；dict → 补 code 字段。"""
    if isinstance(e, str):
        e = {"symbol": e}
    if not isinstance(e, dict):
        raise PicksError(f"picks.{kind} 条目类型非法: {type(e).__name__}")
    code = code_of(e.get("symbol"))
    if not code:
        raise PicksError(f"picks.{kind} 条目 symbol 无法解析: {e.get('symbol')!r}")
    return {**e, "code": code}


def load_picks(date: str, book_dir: str = BOOK_DIR_DEFAULT) -> dict:
    """读取并校验 picks_{date}.json。任何不一致 → PicksError（fail-closed）。"""
    fp = os.path.join(book_dir, "picks", f"picks_{date}.json")
    if not os.path.exists(fp):
        raise PicksError(f"picks 文件缺失: {fp}")
    try:
        with open(fp, encoding="utf-8") as f:
            p = json.load(f)
    except Exception as e:
        raise PicksError(f"picks 解析失败 {fp}: {e}")
    if not isinstance(p, dict):
        raise PicksError("picks 顶层必须是 dict")
    pdate = str(p.get("date") or "")
    if pdate != str(date):
        raise PicksError(f"picks 日期不匹配: 文件={pdate!r} 请求={date!r}（防陈旧 picks 误执行）")
    sells = [_norm_entry(e, "sells") for e in (p.get("sells") or [])]
    buys = [_norm_entry(e, "buys") for e in (p.get("buys") or [])]
    holds = [_norm_entry(e, "holds") for e in (p.get("holds") or [])]
    for b in buys:
        tw = b.get("target_weight")
        if not isinstance(tw, (int, float)) or tw <= 0:
            raise PicksError(f"picks.buys {b['code']} target_weight 非法: {tw!r}")
    for s in sells:
        q = s.get("qty")
        if not isinstance(q, (int, float)) or q <= 0:
            raise PicksError(f"picks.sells {s['code']} qty 非法: {q!r}")
    return {"date": str(date), "sells": sells, "buys": buys, "holds": holds,
            "meta": p.get("meta") or {}}


# ════════════════════════ 计划构建（纯函数，可单测） ════════════════════════

def build_sell_plan(picks: dict, positions: dict, min_lot: int = 100) -> list:
    """合并 picks.sells 与清仓计划，做 T+1 钳制。

    清仓计划（bootstrap 账本切换）：账户持仓中不在 buys∪holds 名单的全部卖出。
    返回 [{code, qty_request, qty_place, t1_blocked, reason, source, full_close}]，
    qty_place=0 的条目也保留（留痕 t1_blocked / 无持仓）。
    """
    keep = {b["code"] for b in picks["buys"]} | {h["code"] for h in picks["holds"]}
    plan, seen = [], set()
    for s in picks["sells"]:
        code = s["code"]
        seen.add(code)
        pos = positions.get(code) or {}
        avail = max(0, int(pos.get("available") or 0))
        held = max(0, int(pos.get("qty") or 0))
        req = int(s.get("qty") or 0)
        place = min(req, avail)
        full_close = held > 0 and place >= held
        if not full_close:
            place = (place // min_lot) * min_lot
        plan.append({"code": code, "qty_request": req, "qty_place": place,
                     "t1_blocked": max(0, req - avail), "reason": s.get("reason") or "",
                     "source": "picks", "full_close": full_close})
    # ── 清仓计划：旧镜像/历史持仓不在 S1 名单内 → 全卖（owner 拍板的账本切换）──
    for code, pos in sorted((positions or {}).items()):
        code = str(code)
        if code in seen or code in keep:
            continue
        held = max(0, int((pos or {}).get("qty") or 0))
        if held <= 0:
            continue
        avail = max(0, int((pos or {}).get("available") or 0))
        place = min(held, avail)
        full_close = place >= held
        if not full_close:
            place = (place // min_lot) * min_lot
        plan.append({"code": code, "qty_request": held, "qty_place": place,
                     "t1_blocked": max(0, held - avail),
                     "reason": "bootstrap_liquidation(非S1名单旧持仓清仓)",
                     "source": "liquidation", "full_close": full_close})
    return plan


def build_buy_plan(picks: dict, positions: dict, prices: dict, cash_budget: float,
                   equity_est: float, min_lot: int = 100) -> list:
    """按 rank 顺序（buys 列表序）折算买入量并做现金截断。纯函数。

    prices: {code: open_price|None}；None=停牌/取价失败 → deferred。
    返回 [{code, rank, target_weight, target_value, held_value, qty_target,
           qty_place, truncated, defer_reason, reason, price, cash_left}]。
    """
    plan, cash_left = [], float(cash_budget)
    for i, b in enumerate(picks["buys"]):
        code = b["code"]
        rank = int(b.get("rank") or (i + 1))
        tw = float(b.get("target_weight") or 0)
        px = prices.get(code)
        rec = {"code": code, "rank": rank, "target_weight": tw,
               "reason": b.get("reason") or "", "price": px,
               "target_value": round(tw * equity_est, 2), "held_value": 0.0,
               "qty_target": 0, "qty_place": 0, "truncated": False,
               "defer_reason": None, "cash_left": None}
        if px is None or px <= 0:
            rec["defer_reason"] = "no_price(停牌或取价失败,顺延)"
            plan.append(rec)
            continue
        held_qty = max(0, int((positions.get(code) or {}).get("qty") or 0))
        held_val = held_qty * px
        rec["held_value"] = round(held_val, 2)
        need_val = max(0.0, tw * equity_est - held_val)
        qty_target = int(need_val / px / min_lot) * min_lot
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

_TERMINAL_OK = {"filled"}
_TERMINAL_BAD = {"rejected", "expired", "cancelled"}


def _place_and_poll(gateway, code, side, qty, price, poll_rounds, poll_sleep, sleep_fn, log):
    """下单 +（可选）轮询终态。返回最终回报 dict。任何异常 → status=error 不抛出。"""
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
                log(f"[S1] poll_order {code} 异常（保持上一状态）: {e}")
                break
            if rep2:
                rep = {**rep, **rep2}
    return rep


def _fee_of(side: str, qty: int, price) -> float:
    if not qty or not price:
        return 0.0
    rate = _FEE_SELL if side == "SELL" else _FEE_BUY
    return round(float(qty) * float(price) * rate, 2)


# ════════════════════════ 净值留痕（α/β 记账钩） ════════════════════════

def _find_prev_s1_equity(date: str, metrics_dir: str):
    """最近一个 equity 非 null 的 S1 口径历史（只接 S1 链，绝不串「仿真账户口径」）。"""
    fps = sorted(glob.glob(os.path.join(metrics_dir, "equity_s1_daily_*.json")))
    for fp in reversed(fps):
        d = os.path.basename(fp)[len("equity_s1_daily_"):-len(".json")]
        if d >= date:
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if _S1_SOURCE_TAG not in str(rec.get("source") or ""):
            continue
        if rec.get("equity") is not None:
            return float(rec["equity"]), d
    return None, None


def write_equity_s1(date: str, equity, cash, market_value,
                    metrics_dir: str = METRICS_DIR_DEFAULT, note: str = "") -> str:
    """S1 专属净值落盘（exec-time 估计口径；prev_equity 仅链接 S1 链）。"""
    os.makedirs(metrics_dir, exist_ok=True)
    prev_eq, prev_d = _find_prev_s1_equity(date, metrics_dir)
    account_ret = (equity / prev_eq - 1.0) if (equity is not None and prev_eq) else None
    notes = [f"{_S1_SOURCE_TAG}(选股策略执行,exec-time估计,开盘价为成本基准)"]
    if note:
        notes.append(note)
    if prev_eq:
        notes.append(f"prev_equity取自{prev_d}(S1链)")
    else:
        notes.append("prev_equity无(S1链首日)→account_ret=null")
    notes.append("benchmark/alpha=exec-time不计算(EOD重估脚本补齐);费用未扣(成交回报filled_commission为准)")
    out = {"date": date, "equity": round(equity, 2) if equity is not None else None,
           "cash": round(cash, 2) if cash is not None else None,
           "market_value": round(market_value, 2) if market_value is not None else None,
           "prev_equity": prev_eq, "account_ret": account_ret,
           "benchmark_ret": None, "benchmark_aux_ret": None, "alpha": None,
           "t0_realized": None, "flow_adjust": 0.0,
           "source": " | ".join(notes)}
    fp = os.path.join(metrics_dir, f"equity_s1_daily_{date}.json")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    return fp


# ════════════════════════ 主流程 ════════════════════════

def run_open_exec(gateway, date: str, book_dir: str = BOOK_DIR_DEFAULT,
                  metrics_dir: str = METRICS_DIR_DEFAULT, params: dict | None = None,
                  dry_run: bool = False, sleep_fn=time.sleep, log=print) -> dict:
    """S1 开盘执行主流程。返回 fills dict（同时落盘）。picks 异常向上抛 PicksError。

    dry_run=True：仍调用 Gateway（由调用方给 mock），仅产物目录标记 mode=dry。
    """
    cfg = {**DEFAULTS, **(params or {})}
    lot = int(cfg["min_lot"])
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "dry" if dry_run else "live"

    picks = load_picks(date, book_dir)           # PicksError → 调用方 fail-closed
    meta = picks["meta"]
    log(f"[S1] picks 已载入 {date}: sells={len(picks['sells'])} buys={len(picks['buys'])} "
        f"holds={len(picks['holds'])} meta={ {k: meta.get(k) for k in ('n_slots','buffer_m','min_hold','tp_arm','score')} }")

    # ── 账户快照 ──
    cash0 = float(gateway.get_cash() or 0)
    positions = {code_of(k) or str(k): {"qty": int((v or {}).get("qty") or 0),
                                        "available": int((v or {}).get("available") or 0),
                                        "cost": float((v or {}).get("cost") or 0)}
                 for k, v in (gateway.get_positions() or {}).items()}
    positions = {k: v for k, v in positions.items() if v["qty"] > 0}
    log(f"[S1] 账户快照: cash={cash0:.2f} 持仓={len(positions)} 只 {sorted(positions)}")

    # ── 取价（卖单也要价：预算折算/留痕用）──
    need_px = {s["code"] for s in picks["sells"]} | {b["code"] for b in picks["buys"]} \
        | set(positions)
    prices, price_missing = {}, []
    for code in sorted(need_px):
        try:
            px = gateway.get_open_price(code)
            px = float(px) if px else None
        except Exception as e:
            log(f"[S1] 取价异常 {code}: {e}")
            px = None
        if px is None or px <= 0:
            px = None
            price_missing.append(code)
        prices[code] = px
    if price_missing:
        log(f"[S1] ⚠️ 取价失败/停牌 {len(price_missing)} 只: {price_missing}（买单顺延/卖单按可得价处理）")

    # ── 净值估计（开盘价口径；取价缺失回退 cost）──
    mv0 = 0.0
    for code, pos in positions.items():
        px = prices.get(code) or pos.get("cost") or 0
        mv0 += pos["qty"] * px
    equity_est = cash0 + mv0
    log(f"[S1] 净值估计 equity={equity_est:.2f}（现金 {cash0:.2f} + 持仓市值 {mv0:.2f}，开盘价口径）")

    # ── ① 先卖（picks.sells ∪ 清仓计划）──
    sell_plan = build_sell_plan(picks, positions, min_lot=lot)
    n_liq = sum(1 for s in sell_plan if s["source"] == "liquidation")
    if n_liq:
        log(f"[S1] ⚠️ 清仓计划: {n_liq} 只非 S1 名单旧持仓将全卖（bootstrap 账本切换）")
    sells_out, proceeds = [], 0.0
    for s in sell_plan:
        code = s["code"]
        px = prices.get(code)
        rec = {"code": code, "symbol": gm_symbol_of(code), "side": "SELL",
               "source": s["source"], "reason": s["reason"],
               "qty_request": s["qty_request"], "qty_placed": 0,
               "t1_blocked": s["t1_blocked"], "status": "skipped",
               "filled_qty": 0, "filled_price": None, "fee_est": 0.0, "message": ""}
        if s["qty_place"] <= 0:
            rec["message"] = ("无可卖量(T+1)" if s["t1_blocked"] > 0 else "无需卖出")
            sells_out.append(rec)
            continue
        if px is None:
            rec["message"] = "取价失败,卖单顺延(次日picks重评)"
            sells_out.append(rec)
            continue
        rep = _place_and_poll(gateway, code, "SELL", s["qty_place"], px,
                              cfg["order_poll_rounds"], cfg["order_poll_sleep_sec"],
                              sleep_fn, log)
        # 保守重试：submitted/partial 之外的可判定失败按 sell_retries 重试
        tries = 0
        while rep.get("status") == "error" and tries < int(cfg["sell_retries"]):
            tries += 1
            log(f"[S1] SELL {code} 第 {tries} 次重试（前次: {rep.get('message')}）")
            rep = _place_and_poll(gateway, code, "SELL", s["qty_place"], px,
                                  cfg["order_poll_rounds"], cfg["order_poll_sleep_sec"],
                                  sleep_fn, log)
        rec.update({"qty_placed": s["qty_place"], "status": rep.get("status"),
                    "filled_qty": int(rep.get("filled_qty") or 0),
                    "filled_price": rep.get("filled_price"),
                    "message": rep.get("message") or ""})
        eff_px = rec["filled_price"] or px
        rec["fee_est"] = _fee_of("SELL", rec["filled_qty"] or s["qty_place"], eff_px)
        # 预算口径：拒单/过期不计回款；其余（含 submitted 未决）按挂单价×haircut 预占
        if rep.get("status") not in _TERMINAL_BAD and rep.get("status") != "error":
            proceeds += s["qty_place"] * px
        else:
            log(f"[S1] ⚠️ SELL {code} {rep.get('status')}: {rec['message']}（回款不计入买侧预算）")
        log(f"[S1] SELL {code} {s['qty_place']}股@{px:.3f} → {rec['status']}"
            f"{' t1_blocked=' + str(s['t1_blocked']) if s['t1_blocked'] else ''} {rec['message']}")
        sells_out.append(rec)

    cash_budget = cash0 + proceeds * float(cfg["proceeds_haircut"])
    log(f"[S1] 买侧预算: 期初现金 {cash0:.0f} + 预计回款 {proceeds:.0f}×{cfg['proceeds_haircut']}"
        f" = {cash_budget:.0f}")

    # ── ② 后买（rank 顺序，现金截断）──
    buy_plan = build_buy_plan(picks, positions, prices, cash_budget, equity_est, min_lot=lot)
    buys_out = []
    for b in buy_plan:
        code = b["code"]
        rec = {"code": code, "symbol": gm_symbol_of(code), "side": "BUY",
               "rank": b["rank"], "target_weight": b["target_weight"],
               "target_value": b["target_value"], "held_value": b["held_value"],
               "qty_target": b["qty_target"], "qty_placed": 0,
               "truncated": b["truncated"], "defer_reason": b["defer_reason"],
               "reason": b["reason"], "status": "skipped",
               "filled_qty": 0, "filled_price": None, "fee_est": 0.0, "message": ""}
        if b["defer_reason"] or b["qty_place"] <= 0:
            rec["message"] = b["defer_reason"] or "无需买入(已达目标权重)"
            buys_out.append(rec)
            continue
        px = b["price"]
        rep = _place_and_poll(gateway, code, "BUY", b["qty_place"], px,
                              cfg["order_poll_rounds"], cfg["order_poll_sleep_sec"],
                              sleep_fn, log)
        rec.update({"qty_placed": b["qty_place"], "status": rep.get("status"),
                    "filled_qty": int(rep.get("filled_qty") or 0),
                    "filled_price": rep.get("filled_price"),
                    "message": rep.get("message") or ""})
        eff_px = rec["filled_price"] or px
        rec["fee_est"] = _fee_of("BUY", rec["filled_qty"] or b["qty_place"], eff_px)
        if rep.get("status") in _TERMINAL_BAD or rep.get("status") == "error":
            log(f"[S1] ⚠️ BUY {code} {rep.get('status')}: {rec['message']}（预算自动让给后续 rank）")
        log(f"[S1] BUY {code} r{b['rank']} {b['qty_place']}股@{px:.3f}"
            f"(tw={b['target_weight']:.2%}) → {rec['status']}"
            f"{' [truncated]' if b['truncated'] else ''} {rec['message']}")
        buys_out.append(rec)

    # ── ③ 落盘：fills / exec_report / equity_s1 ──
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_rej = sum(1 for r in sells_out + buys_out if r["status"] in _TERMINAL_BAD or r["status"] == "error")
    n_t1 = sum(1 for r in sells_out if r["t1_blocked"] > 0)
    n_defer = sum(1 for r in buys_out if r["defer_reason"])
    cash_after_est = cash_budget - sum(
        (r["filled_qty"] or r["qty_placed"]) * (r["filled_price"] or prices.get(r["code"]) or 0)
        for r in buys_out if r["qty_placed"] > 0 and r["status"] not in _TERMINAL_BAD
        and r["status"] != "error")
    summary = {"sells_planned": len(sell_plan),
               "sells_placed": sum(1 for r in sells_out if r["qty_placed"] > 0),
               "buys_planned": len(buy_plan),
               "buys_placed": sum(1 for r in buys_out if r["qty_placed"] > 0),
               "liquidation_count": n_liq, "rejected_count": n_rej,
               "t1_blocked_count": n_t1, "deferred_count": n_defer,
               "proceeds_expected": round(proceeds, 2),
               "cash_before": round(cash0, 2), "cash_after_est": round(cash_after_est, 2),
               "equity_est": round(equity_est, 2)}
    fills = {"date": str(date), "mode": mode, "started_at": started, "finished_at": finished,
             "account": {"cash_before": round(cash0, 2),
                         "positions_before": {k: v for k, v in sorted(positions.items())},
                         "equity_est": round(equity_est, 2)},
             "prices": {k: v for k, v in sorted(prices.items()) if v},
             "price_missing": price_missing,
             "sells": sells_out, "buys": buys_out,
             "bootstrap_liquidation": n_liq > 0,
             "summary": summary, "picks_meta": meta,
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
    fp_eq = write_equity_s1(str(date), equity=equity_est, cash=cash_after_est,
                            market_value=equity_est - cash_after_est,
                            metrics_dir=metrics_dir,
                            note=f"mode={mode};摘要见 {fp_rep}")
    log(f"[S1] 留痕完成: {fp_fills} | {fp_rep} | {fp_eq}")
    log(f"[S1] 摘要: {json.dumps(summary, ensure_ascii=False)}")
    fills["_paths"] = {"fills": fp_fills, "report": fp_rep, "equity": fp_eq}
    return fills
