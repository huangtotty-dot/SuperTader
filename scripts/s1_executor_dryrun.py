# -*- coding: utf-8 -*-
r"""s1_executor_dryrun.py — S1 执行器全流程干跑（mock picks + mock GM 回报）。

owner 决策背景：S1 终选（N4/M8/H1/TP-A/score_eq）2026-10-12 起驱动掘金模拟盘
auto 账本。本脚本在**不碰 gm.api**的前提下，把 s1_executor.run_open_exec 的关键
分支全部跑通：清仓计划生成、先卖后买、T+1 校验、现金截断、停牌顺延、拒单级联。

产物目录与生产隔离：t_io/state/s1_book/dryrun/（picks/fills/report/equity 全在里面，
不污染真实 s1_book 与 t_io/metrics 的 S1 净值链）。

用法：
    python scripts/s1_executor_dryrun.py                  # 全部场景
    python scripts/s1_executor_dryrun.py --scenario bootstrap
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUTO = os.path.join(_ROOT, "execution", "auto")
for _p in (_ROOT, _AUTO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import s1_executor as s1e  # noqa: E402

DRY_BOOK = os.path.join(_ROOT, "t_io", "state", "s1_book", "dryrun")
DRY_DATE = "2026-10-12"


class MockGateway:
    """mock GM 通道：价格表 + 行为表（fill/partial/reject/error），记录全部下单。"""

    def __init__(self, cash, positions, prices, behavior=None):
        self._cash = float(cash)
        self._pos = dict(positions)          # {code: {qty, available, cost}}
        self._px = dict(prices)              # {code: open_price|None}
        self._bh = dict(behavior or {})      # {code: "fill"|"partial"|"reject"|"error"}
        self.orders = []                     # 下单记录 [{code, side, qty, price, status}]
        self._seq = 0

    def get_cash(self):
        return self._cash

    def get_positions(self):
        return self._pos

    def get_open_price(self, code):
        return self._px.get(code)

    def place_order(self, code, side, qty, price):
        self._seq += 1
        oid = f"MOCK-{self._seq:03d}"
        act = self._bh.get(code, "fill")
        px = float(price or self._px.get(code) or 0)
        if act == "error":
            raise RuntimeError("mock gateway boom")
        if act == "reject":
            rec = {"order_id": oid, "status": "rejected", "filled_qty": 0,
                   "filled_price": None, "message": "mock拒单 NoEnoughCash/风控"}
        elif act == "partial":
            fq = max(100, (int(qty) // 2 // 100) * 100)
            rec = {"order_id": oid, "status": "partial", "filled_qty": fq,
                   "filled_price": px, "message": "mock部分成交"}
            self._apply(side, code, fq, px)
        else:
            rec = {"order_id": oid, "status": "filled", "filled_qty": int(qty),
                   "filled_price": px, "message": ""}
            self._apply(side, code, int(qty), px)
        self.orders.append({"code": code, "side": side, "qty": int(qty),
                            "price": px, **rec})
        return rec

    def poll_order(self, order_id, code):     # partial → 下轮翻 filled（终态）
        for o in self.orders:
            if o["order_id"] == order_id:
                if o["status"] == "partial":
                    rest = o["qty"] - o["filled_qty"]
                    o["status"] = "filled"
                    o["filled_qty"] = o["qty"]
                    self._apply(o["side"], code, rest, o["price"])
                return {k: o[k] for k in ("order_id", "status", "filled_qty",
                                          "filled_price", "message")}
        return {"order_id": order_id, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": "not_found"}

    def _apply(self, side, code, qty, px):
        if side == "SELL":
            p = self._pos.get(code)
            if p:
                p["qty"] = max(0, p["qty"] - qty)
                p["available"] = max(0, p["available"] - qty)
            self._cash += qty * px
        else:
            p = self._pos.setdefault(code, {"qty": 0, "available": 0, "cost": 0.0})
            old_v = p["qty"] * p["cost"]
            p["qty"] += qty                      # T+1：available 不加
            p["cost"] = (old_v + qty * px) / p["qty"] if p["qty"] else 0.0
            self._cash -= qty * px


# ── 场景定义 ─────────────────────────────────────────────────────────────

def _write_picks(book, date, sells, buys, holds, meta=None):
    os.makedirs(os.path.join(book, "picks"), exist_ok=True)
    fp = os.path.join(book, "picks", f"picks_{date}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"date": date, "sells": sells, "buys": buys, "holds": holds,
                   "meta": meta or {"n_slots": 4, "buffer_m": 8, "min_hold": 1,
                                    "tp_arm": "A", "score": "score_eq",
                                    "source": "dryrun_mock"}},
                  f, ensure_ascii=False, indent=1)
    return fp


SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def bootstrap(book):
    """周一首次运行：旧镜像持仓全部清仓 + 买入 S1 Top-4（账本切换）。"""
    _write_picks(book, DRY_DATE,
                 sells=[],
                 buys=[{"symbol": "300456", "target_weight": 0.25, "reason": "rank_enter r=1"},
                       {"symbol": "688008", "target_weight": 0.25, "reason": "rank_enter r=2"},
                       {"symbol": "600276", "target_weight": 0.25, "reason": "rank_enter r=3"},
                       {"symbol": "300153", "target_weight": 0.25, "reason": "rank_enter r=4"}],
                 holds=[])
    gw = MockGateway(
        cash=20000,
        positions={"600481": {"qty": 1400, "available": 1400, "cost": 3.9},
                   "000988": {"qty": 500, "available": 500, "cost": 37.6},
                   "515180": {"qty": 50000, "available": 50000, "cost": 0.92},
                   "300054": {"qty": 800, "available": 800, "cost": 50.9}},
        prices={"600481": 3.95, "000988": 38.0, "515180": 0.925, "300054": 51.2,
                "300456": 45.0, "688008": 60.0, "600276": 44.0, "300153": 12.0})
    return gw, {"liquidation": 4, "sells_placed": 4, "buys_placed": 4}


@scenario
def t1_block(book):
    """T+1：picks 要卖 1200，但 available 只有 400（当日已买入部分不可卖）。"""
    _write_picks(book, DRY_DATE,
                 sells=[{"symbol": "300456", "qty": 1200, "reason": "rank_exit r=9>M8"}],
                 buys=[{"symbol": "600276", "target_weight": 0.5, "reason": "rank_enter r=1"}],
                 holds=[{"symbol": "688008"}])
    gw = MockGateway(
        cash=10000,
        positions={"300456": {"qty": 1200, "available": 400, "cost": 45.0},
                   "688008": {"qty": 800, "available": 800, "cost": 60.0}},
        prices={"300456": 46.0, "688008": 61.0, "600276": 44.0})
    return gw, {"t1_blocked": 800, "sell_placed_qty": 400, "buys_placed": 1}


@scenario
def cash_truncate(book):
    """现金不足：rank 1-2 买满，rank 3 截断，rank 4 买不起顺延。"""
    _write_picks(book, DRY_DATE,
                 sells=[],
                 buys=[{"symbol": "300456", "target_weight": 0.40, "reason": "r=1"},
                       {"symbol": "688008", "target_weight": 0.40, "reason": "r=2"},
                       {"symbol": "600276", "target_weight": 0.40, "reason": "r=3"},
                       {"symbol": "300153", "target_weight": 0.40, "reason": "r=4"}],
                 holds=[])
    gw = MockGateway(
        cash=60000, positions={},
        prices={"300456": 45.0, "688008": 60.0, "600276": 44.0, "300153": 12.0})
    return gw, {"buys_placed": 3, "truncated": 2, "deferred": 1}
    # 口径说明：r3 截断至 300 股（truncated），r4 截断至 0 且顺延（truncated+deferred 双标记）


@scenario
def suspend_and_sell_reject(book):
    """停牌顺延（买 300153 无价、卖 000988 无价）+ 卖单被拒回款不入预算。"""
    _write_picks(book, DRY_DATE,
                 sells=[{"symbol": "000988", "qty": 500, "reason": "rank_exit"},
                        {"symbol": "600481", "qty": 1400, "reason": "rank_exit"}],
                 buys=[{"symbol": "300456", "target_weight": 0.5, "reason": "r=1"},
                       {"symbol": "300153", "target_weight": 0.5, "reason": "r=2(停牌)"}],
                 holds=[])
    gw = MockGateway(
        cash=5000,
        positions={"000988": {"qty": 500, "available": 500, "cost": 37.6},
                   "600481": {"qty": 1400, "available": 1400, "cost": 3.9}},
        prices={"000988": None, "600481": 3.95, "300456": 45.0, "300153": None},
        behavior={"600481": "reject"})
    return gw, {"sell_deferred": 1, "sell_rejected": 1, "buy_deferred": 1,
                "buys_placed": 1, "buy0_qty": 100}   # 5000 现金只能买 1 手 45 元票


@scenario
def partial_fill(book):
    """部分成交：卖单 partial → 轮询翻 filled，回款按挂单价计预算。"""
    _write_picks(book, DRY_DATE,
                 sells=[{"symbol": "600276", "qty": 2000, "reason": "rank_exit"}],
                 buys=[{"symbol": "300456", "target_weight": 0.8, "reason": "r=1"}],
                 holds=[])
    gw = MockGateway(
        cash=0,
        positions={"600276": {"qty": 2000, "available": 2000, "cost": 44.0}},
        prices={"600276": 44.0, "300456": 45.0},
        behavior={"600276": "partial"})
    return gw, {"sell_status": "filled", "buys_placed": 1}


def _check(name, cond, detail, failures):
    tag = "OK " if cond else "FAIL"
    print(f"    [{tag}] {detail}")
    if not cond:
        failures.append(f"{name}: {detail}")


def run_scenario(name, fresh=True):
    fn = SCENARIOS[name]
    book = os.path.join(DRY_BOOK, name)
    if fresh and os.path.exists(book):
        shutil.rmtree(book)
    os.makedirs(book, exist_ok=True)
    metrics = os.path.join(book, "metrics")
    gw, expect = fn(book)
    print(f"\n═══ 场景 {name} ═══")
    res = s1e.run_open_exec(gw, DRY_DATE, book_dir=book, metrics_dir=metrics,
                            params={"order_poll_sleep_sec": 0.01},
                            dry_run=True, log=lambda *a: print("   ", *a))
    sm, fails = res["summary"], []
    sells, buys = res["sells"], res["buys"]
    if "liquidation" in expect:
        _check(name, res["bootstrap_liquidation"] and sm["liquidation_count"] == expect["liquidation"],
               f"清仓计划 {expect['liquidation']} 只（实际 {sm['liquidation_count']}）", fails)
    if "sells_placed" in expect:
        _check(name, sm["sells_placed"] == expect["sells_placed"],
               f"卖单 {expect['sells_placed']} 笔（实际 {sm['sells_placed']}）", fails)
    if "t1_blocked" in expect:
        got = sum(s["t1_blocked"] for s in sells)
        _check(name, got == expect["t1_blocked"],
               f"T+1 拦截 {expect['t1_blocked']} 股（实际 {got}）", fails)
    if "sell_placed_qty" in expect:
        got = sum(s["qty_placed"] for s in sells)
        _check(name, got == expect["sell_placed_qty"],
               f"实际卖量 {expect['sell_placed_qty']}（实际 {got}）", fails)
    if "buys_placed" in expect:
        _check(name, sm["buys_placed"] == expect["buys_placed"],
               f"买单 {expect['buys_placed']} 笔（实际 {sm['buys_placed']}）", fails)
    if "truncated" in expect:
        got = sum(1 for b in buys if b["truncated"])
        _check(name, got == expect["truncated"],
               f"截断 {expect['truncated']} 笔（实际 {got}）", fails)
    if "deferred" in expect:
        got = sum(1 for b in buys if b["defer_reason"])
        _check(name, got == expect["deferred"],
               f"买单顺延 {expect['deferred']} 笔（实际 {got}）", fails)
    if "sell_deferred" in expect:
        got = sum(1 for s in sells if "取价失败" in (s["message"] or ""))
        _check(name, got == expect["sell_deferred"],
               f"卖单顺延 {expect['sell_deferred']} 笔（实际 {got}）", fails)
    if "sell_rejected" in expect:
        got = sum(1 for s in sells if s["status"] == "rejected")
        _check(name, got == expect["sell_rejected"],
               f"卖单被拒 {expect['sell_rejected']} 笔（实际 {got}）", fails)
    if "buy_deferred" in expect:
        got = sum(1 for b in buys if b["defer_reason"])
        _check(name, got == expect["buy_deferred"],
               f"买侧顺延 {expect['buy_deferred']} 笔（实际 {got}）", fails)
    if "buy0_qty" in expect:
        got = next((b["qty_placed"] for b in buys if b["qty_placed"] > 0), 0)
        _check(name, got == expect["buy0_qty"],
               f"首笔买量 {expect['buy0_qty']}（实际 {got}，拒卖回款不得入预算）", fails)
    if "sell_status" in expect:
        got = sells[0]["status"] if sells else None
        _check(name, got == expect["sell_status"],
               f"卖单终态 {expect['sell_status']}（实际 {got}，partial→poll 翻 filled）", fails)
    # 产物齐备性（所有场景通用）
    for kind in ("fills", "report", "equity"):
        _check(name, os.path.exists(res["_paths"][kind]),
               f"产物存在: {os.path.basename(res['_paths'][kind])}", fails)
    with open(res["_paths"]["equity"], encoding="utf-8") as f:
        eq = json.load(f)
    _check(name, "S1账本口径" in eq.get("source", ""),
           "equity 带 S1账本口径 source 标签（与仿真账户口径链隔离）", fails)
    print(f"    摘要: {json.dumps(sm, ensure_ascii=False)}")
    return fails


def main():
    ap = argparse.ArgumentParser(description="S1 执行器干跑（mock picks + mock GM）")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS) + ["all"], default="all")
    args = ap.parse_args()
    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    print(f"S1 执行器干跑 · 日期 {DRY_DATE} · 产物隔离目录 {DRY_BOOK}")
    all_fails = []
    for n in names:
        try:
            all_fails += run_scenario(n)
        except Exception as e:
            import traceback
            traceback.print_exc()
            all_fails.append(f"{n}: 异常 {type(e).__name__}: {e}")
    print("\n" + "═" * 50)
    if all_fails:
        print(f"❌ 干跑未通过 {len(all_fails)} 项:")
        for f in all_fails:
            print(f"  - {f}")
        sys.exit(1)
    print(f"✅ 全部 {len(names)} 个场景通过（产物见 {DRY_BOOK}/<scenario>/）")


if __name__ == "__main__":
    main()
