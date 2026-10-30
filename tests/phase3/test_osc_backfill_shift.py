# -*- coding: utf-8 -*-
"""超跌跳空确认 · 买不进替补 **B 方案（受限顺移）生产施工** 验收测试 —— 2026-10-16。

背景：回测过闸（Sharpe 0.384→0.518，`doc/solutions/2026-10-16_替补机制B方案回测验证.md`），
owner 2026-10-16 拍板上线（`osc_backfill_enabled` 默认翻 True）。本文件钉住上线路径的
**六条安全闸 + 两项生产加固**：

  1. 首单未成交 → 撤单确认 → 顺移候补**成交**（端到端）；
  2. 候补**封死涨停 ⇒ 跳过不追**（方案 A 的教训）；
  3. **最多补 2 轮**（on_bar 时点表/每日一次）；
  4. **10:20 后不再开新仓**（硬闸）；
  5. **撤单失败 ⇒ 本轮不补单**（账户委托状态不可知时绝不加仓）；
  6. **开关关闭 ⇒ 行为与现状一致**（不撤、不补、不留痕）；
  7. 生产加固：`order_id` 缺失（GM 仿真终端同步回报恒无 id）⇒ gateway 按 symbol
     **解析在途买单**再撤；解析不到（已成交/已终态）⇒ 未确认、不中止本轮；
  8. 留痕：fills `buys` 带 `round`/`shift_from`，`backfill_rounds` 记撤单明细；
     顺移买入**并入持仓账本**（否则卖侧看不见 ⇒ 孤儿持仓）。

运行：python -m unittest tests.phase3.test_osc_backfill_shift -v
"""
import datetime as _dt
import json
import os
import sys
import tempfile
import unittest
from datetime import time as dtime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import backfill as bf                           # noqa: E402
from oversold_gap_confirm import executor as wse                          # noqa: E402
from oversold_gap_confirm import hooks as osc_hooks                       # noqa: E402
from oversold_gap_confirm.gateway import OscGateway                       # noqa: E402


# ════════════════════════ 鸭子类型 FakeGateway（补单编排用） ════════════════════════

class FakeGateway(object):
    """够 `run_backfill_exec` 用：get_cash / get_positions / place_order /
    poll_order / cancel_order。可编程：撤单确认状态、下发是否成功、下单是否秒成。"""

    def __init__(self, cash=1_000_000.0, cancel_status=5, cancel_ok=True,
                 place_status="submitted", place_fills=False):
        self.cash = cash
        self.cancel_status = cancel_status
        self.cancel_ok = cancel_ok
        self.place_status = place_status
        self.place_fills = place_fills          # True ⇒ 下单即全成（顺移单秒成）
        self.placed = []
        self.cancelled = []

    def get_cash(self):
        return self.cash

    def get_positions(self):
        return {}

    def cancel_order(self, order_id, code):
        if not self.cancel_ok:
            return {"ok": False, "order_id": order_id, "message": "cancel_unavailable"}
        self.cancelled.append((order_id, code))
        return {"ok": True, "order_id": order_id, "message": ""}

    def poll_order(self, order_id, code):
        return {"order_id": order_id,
                "status": {5: "cancelled", 6: "pending_cancel", 3: "filled",
                           4: "rejected"}.get(self.cancel_status, "submitted"),
                "raw_status": self.cancel_status,
                "filled_qty": 0, "filled_price": None, "message": ""}

    def place_order(self, code, side, qty, price):
        self.placed.append((code, side, qty, price))
        fq = qty if self.place_fills else 0
        return {"order_id": "o_new_%d" % len(self.placed),
                "status": ("filled" if self.place_fills else self.place_status),
                "filled_qty": fq, "filled_price": (price if self.place_fills else None),
                "message": ""}


def _fills_with_pending(codes=("600001",), qty=1000, px=10.0, with_oid=True):
    """首轮 fills：给定票全部「挂单未成交」。with_oid=False 模拟 GM 仿真常态（无单号）。"""
    return {"date": "2026-10-15", "buys": [
        {"code": c, "order_id": ("o_" + c) if with_oid else None, "status": "submitted",
         "qty_placed": qty, "filled_qty": 0, "price": px, "gap": 0.1, "filled_price": None}
        for c in codes]}


def _spare(code, gap, price=10.0):
    return {"code": code, "gap": gap, "board": "半导体", "price": price}


_PARAMS = {"single_budget": 100000.0, "daily_budget": 400000.0, "cash_headroom": 0.95,
           "order_poll_rounds": 1, "order_poll_sleep_sec": 0.0}
_PC = {"600001": 9.0, "600002": 9.0, "600003": 9.0}
# 现价 10 ≥ 昨收 9、VWAP 9.5 ≤ 现价、卖一有量、涨停价 9.9 ⇒ 复检全过
_Q = {"600001": {"price": 10.0, "vwap": 9.5, "ask_v": 500},
      "600002": {"price": 10.0, "vwap": 9.5, "ask_v": 500},
      "600003": {"price": 10.0, "vwap": 9.5, "ask_v": 500}}


def _run_exec(gw, spares, quotes=None, fills=None):
    fills = fills if fills is not None else _fills_with_pending()
    return bf.run_backfill_exec(
        gw, "2026-10-15", fills, bf.pending_from_fills(fills), spares, _PC,
        _Q if quotes is None else quotes,
        params=_PARAMS, sleep_fn=lambda s: None, log=lambda *a: None)


# ════════════════════════ 闸①②⑤：编排层 ════════════════════════

class TestShiftExecGates(unittest.TestCase):
    def test_unfilled_cancel_confirmed_then_shift_fills(self):
        """闸①：首单未成交 → 撤单确认（status=5 钱已解冻）→ 顺移候补且**成交**。"""
        gw = FakeGateway(cancel_status=5, place_fills=True)
        res = _run_exec(gw, [_spare("600002", 0.08)])
        self.assertEqual(res["cancel"]["released"], ["600001"])
        self.assertEqual(gw.cancelled, [("o_600001", "600001")], "必须真的发了撤单")
        self.assertEqual([p["code"] for p in res["placed"]], ["600002"])
        self.assertEqual(res["placed"][0]["status"], "filled", "顺移单应成交")
        self.assertGreater(res["placed"][0]["filled_qty"], 0)

    def test_sealed_spare_skipped_not_chased(self):
        """闸②：候补封死涨停（贴涨停价+卖一无量）⇒ 跳过，顺移给下一名。"""
        gw = FakeGateway(cancel_status=5)
        q = {"600002": {"price": 9.9, "vwap": 9.5, "ask_v": 0},      # 封死
             "600003": {"price": 10.0, "vwap": 9.5, "ask_v": 500}}
        res = _run_exec(gw, [_spare("600002", 0.09), _spare("600003", 0.08)], quotes=q)
        self.assertIn({"code": "600002", "reason": "limit_sealed"}, res["skipped"])
        self.assertEqual([p["code"] for p in res["placed"]], ["600003"])

    def test_cancel_failed_aborts_round_no_new_orders(self):
        """闸⑤：撤单下发失败（原语缺席/拒撤）⇒ **本轮不补单**，挂单保持不动。"""
        gw = FakeGateway(cancel_ok=False)
        res = _run_exec(gw, [_spare("600002", 0.08)])
        self.assertEqual(res["cancel"]["failed"], ["600001"])
        self.assertEqual(res["placed"], [], "撤单失败 ⇒ 一个候补单都不许下")
        self.assertEqual(res["note"], "cancel_failed_abort")

    def test_pending_cancel_status6_money_not_released(self):
        """status=6（PendingCancel）钱未解冻 ⇒ 不算释放、不占释放额度，但也**不**中止本轮。"""
        gw = FakeGateway(cancel_status=6)
        res = _run_exec(gw, [_spare("600002", 0.08)])
        self.assertEqual(res["cancel"]["released"], [])
        self.assertEqual(res["cancel"]["unconfirmed"], ["600001"])
        self.assertEqual(res["committed"].get("600001"), 10000.0, "未确认冻结必须继续占额度")
        self.assertEqual([p["code"] for p in res["placed"]], ["600002"],
                         "未确认 ≠ 失败：剩余额度内的补单仍允许")

    def test_pending_without_order_id_kept(self):
        """生产加固：`order_id` 缺失的未成交买单**必须**进待补单（GM 仿真常态），
        否则整个替补机制在生产上静默失效。"""
        pend = bf.pending_from_fills(_fills_with_pending(with_oid=False))
        self.assertEqual([p["code"] for p in pend], ["600001"])
        self.assertIsNone(pend[0]["order_id"])


# ════════════════════════ 闸⑦：order_id 缺失按 symbol 解析（真 OscGateway + 桩 gm） ═

class _FakeGm(object):
    OrderSide_Buy, OrderSide_Sell = 1, 2
    OrderType_Market, OrderType_Limit = 1, 2
    PositionEffect_Open, PositionEffect_Close = 1, 2
    ADJUST_PREV = 1

    def __init__(self, orders=None, cancel_ok=True):
        self.calls = []
        self._orders = list(orders or [])
        self._cancel_ok = cancel_ok

    def order_cancel(self, **kw):
        self.calls.append(("order_cancel", kw))
        if not self._cancel_ok:
            return {"ok": False}
        # 撤单成功后，该单在 get_orders 里转为已撤销（status=5）——供后续轮询确认识别
        _oid = kw.get("order_id")
        self._orders = [dict(o, status=5) if isinstance(o, dict) and
                        (o.get("id") or o.get("order_id")) == _oid else o
                        for o in self._orders]
        return [{"id": _oid}]

    def get_orders(self, **kw):
        self.calls.append(("get_orders", kw))
        if kw.get("symbol"):
            return [dict(o, symbol=o.get("symbol") or kw["symbol"])
                    if isinstance(o, dict) else o for o in self._orders]
        return list(self._orders)

    def current(self, **kw):
        return []

    def history_n(self, **kw):
        return []


class _FakeSvc(object):
    def __init__(self, gm):
        self.gm = gm
        self.reject_statuses = (4, 5, 6, 8, 12)

    def sdk_call(self, desc, fn, *a, **k):
        return fn(*a, **k)

    def code_to_gm(self, code):
        c = str(code)
        return ("SHSE." if c[:1] in "56" else "SZSE.") + c

    def raw_code(self, symbol):
        return str(symbol).split(".")[-1]


def _osc_gw(gm):
    ctx = type("C", (), {})()
    ctx.now = _dt.datetime(2026, 10, 15, 10, 8, 0)
    acc = type("A", (), {"cash": lambda self: {"available": 1e6},
                         "positions": lambda self: []})()
    ctx.account = (lambda: acc)
    return OscGateway(ctx, _FakeSvc(gm), log=lambda *a: None)


class TestCancelOrderIdResolution(unittest.TestCase):
    def test_missing_oid_resolved_via_get_orders(self):
        """order_id 缺失 ⇒ 按 symbol 找到**在途买单**的 id ⇒ 撤它 ⇒ 确认后释放。"""
        gm = _FakeGm(orders=[{"id": "o_real", "status": 1, "side": 1}])
        gw = _osc_gw(gm)
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": None}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        cancels = [kw for name, kw in gm.calls if name == "order_cancel"]
        self.assertEqual(cancels, [{"order_id": "o_real"}],
                         "必须用解析出来的真实单号撤单，而不是拿 None 去撤")
        self.assertEqual(res["released"], ["600001"],
                         "撤单后 get_orders 报 status=5 ⇒ 必须确认释放")

    def test_no_open_order_is_unconfirmed_not_failure(self):
        """解析不到在途单（已成交/已终态）⇒ 未确认（不释放、**不中止**本轮补单）。"""
        gm = _FakeGm(orders=[])                          # 该 symbol 无在途委托
        gw = _osc_gw(gm)
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": None}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        self.assertEqual(res["unconfirmed"], ["600001"])
        self.assertEqual(res["failed"], [], "无在途单 ≠ 撤单失败（不得触发补单中止）")

    def test_open_order_without_id_is_failure_and_aborts(self):
        """找到了在途单但它也没 id ⇒ 撤不了 ⇒ 失败 ⇒ **本轮不补单**。"""
        gm = _FakeGm(orders=[{"id": "", "status": 1, "side": 1}])
        gw = _osc_gw(gm)
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": None}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        self.assertEqual(res["failed"], ["600001"])

    def test_mixed_round_not_found_does_not_block_real_cancel(self):
        """混合现场：600001 已成交（无在途）+ 600002 封板在途（有 id）⇒
        前者未确认、后者撤成释放 ⇒ 顺移候补**照常下单**。"""
        gm = _FakeGm(orders=[{"id": "o2", "status": 1, "side": 1}])

        class _ResolveGw(FakeGateway):
            def cancel_order(self, order_id, code):
                if code == "600001":
                    return {"ok": False, "order_id": None, "message": "no_open_order",
                            "not_found": True}
                return FakeGateway.cancel_order(self, order_id, code)

        gw = _ResolveGw(cancel_status=5)
        fills = _fills_with_pending(codes=("600001", "600002"))
        fills["buys"][0]["order_id"] = None               # 已成交那笔：无单号
        res = _run_exec(gw, [_spare("600003", 0.08)], fills=fills)
        self.assertEqual(res["cancel"]["unconfirmed"], ["600001"])
        self.assertEqual(res["cancel"]["released"], ["600002"])
        self.assertEqual([p["code"] for p in res["placed"]], ["600003"],
                         "未确认的那笔不得阻塞另一笔的真实顺移")


# ════════════════════════ 闸③④⑥ + 留痕/入账：hooks 层 ════════════════════════

class FakeRT(object):
    """够 `on_bar` / `try_backfill` 用的最小 rt（与 test_osc_hooks_backfill 同款）。"""
    def __init__(self, params=None, book_dir=None, kill=False, quotes=None):
        self.p = dict(params or {})
        self.book_dir = book_dir or tempfile.mkdtemp()
        self._kill = kill
        self.audits = []
        self.logs = []
        self.quotes = quotes or {}
        self.gw = FakeGateway()
        self.gm = self

    def param(self, k, d=None):
        return self.p.get(k, d)

    def log(self, *a, **k):
        self.logs.append(" ".join(str(x) for x in a))

    def audit(self, rec):
        self.audits.append(rec)

    def risk(self, *a, **k):
        pass

    def kill_switch(self):
        return self._kill

    def gateway(self, ctx):
        return self.gw

    def code_to_gm(self, code):
        c = str(code)
        return ("SHSE." if c[:1] in "56" else "SZSE.") + c

    def sdk_call(self, desc, fn, *a, **k):
        return fn(*a, **k)

    def is_live(self, ctx):
        return True

    def current(self, symbols=None, **k):
        out = []
        for s in (symbols or []):
            code = str(s).split(".")[-1]
            q = self.quotes.get(code)
            if not q:
                continue
            out.append({"symbol": s, "price": q.get("price"),
                        "cum_amount": q.get("price", 0) * (q.get("cum_volume") or 0),
                        "cum_volume": q.get("cum_volume"),
                        "quotes": [{"ask_v": q.get("ask_v", 0)}]})
        return out


def _quotes(*codes):
    return {c: {"price": 10.0, "cum_volume": 1000, "ask_v": 500} for c in codes}


def _seed_state(**kw):
    base = {"buy": True, "buy_done": True,          # buy_done=True 模拟生产：首轮已执行并落盘
            "candidates": {"600001": {"prev_close": 9.0, "ma20": 10.0, "board": "半导体"},
                           "600002": {"prev_close": 9.0, "ma20": 10.0, "board": "半导体"},
                           "600003": {"prev_close": 9.0, "ma20": 10.0, "board": "半导体"}},
            "fills": _fills_with_pending(),
            "pending": bf.pending_from_fills(_fills_with_pending()),
            "spares": [_spare("600002", 0.08)], "done_codes": {"600001"}}
    base.update(kw)
    osc_hooks._STATE.update(base)


class TestHooksShiftGates(unittest.TestCase):
    def setUp(self):
        osc_hooks.reset_day("2026-10-15")

    def _rt(self, **kw):
        kw.setdefault("params", {"osc_backfill_enabled": True, "osc_enabled": True,
                                 "osc_single_budget": 100000.0,
                                 "osc_daily_budget": 400000.0,
                                 # poll_rounds=0：补单下的顺移单保持 submitted（不在下单内轮询），
                                 # 这样它才会作为"未成交"进入下一轮 pending
                                 "osc_order_poll_rounds": 0})
        return FakeRT(**kw)

    def test_two_rounds_fire_once_each_and_no_third(self):
        """闸③：最多 2 轮（10:08 / 10:14），同轮只跑一次，不存在第三轮。"""
        rt = self._rt(quotes=_quotes("600001", "600002", "600003"))
        _seed_state(date="2026-10-15", prepare=True, morning=True, sell=True)
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 8), "2026-10-15", dtime(10, 8))
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 8), "2026-10-15", dtime(10, 8))
        n_cancel_r1 = len(rt.gw.cancelled)
        self.assertGreater(n_cancel_r1, 0, "第 1 轮应发撤单")
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 14), "2026-10-15", dtime(10, 14))
        self.assertEqual(set((osc_hooks._STATE["backfill"] or {}).keys()), {0, 1},
                         "两轮都应触发且只触发一次")
        self.assertEqual(len(osc_hooks.backfill_times(rt)), 2, "时点表只有两轮（没有第三轮）")

    def test_round2_does_not_rebuy_same_spare(self):
        """第 2 轮不得**原票追回**第 1 轮已顺移买过的候补（那是方案 A 的追价，已否决）：
        候补按 done_codes 剪枝，池耗尽 ⇒ 只撤不买。"""
        rt = self._rt(quotes=_quotes("600001", "600002", "600003"))
        _seed_state(date="2026-10-15", prepare=True, morning=True, sell=True,
                    spares=[_spare("600002", 0.08), _spare("600003", 0.07)])
        # 第 1 轮：撤 600001 → 顺移买候补（保持 submitted 进下一轮 pending）
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 8), "2026-10-15", dtime(10, 8))
        placed_r1 = [p[0] for p in rt.gw.placed]
        self.assertIn("600002", placed_r1, "第 1 轮应顺移买入候补 600002")
        # 第 2 轮：撤上一轮的顺移挂单 → 候补已剪枝 ⇒ 不得重买 600002
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 14), "2026-10-15", dtime(10, 14))
        all_placed = [p[0] for p in rt.gw.placed]
        self.assertEqual(all_placed.count("600002"), 1,
                         "600002 只能被买一次：第 2 轮不得原票追回")
        self.assertEqual([s["code"] for s in osc_hooks._STATE["spares"]], [],
                         "买过的候补必须从 spares 剪掉（池耗尽即停）")

    def test_no_new_position_after_1020(self):
        """闸④：10:20 硬闸之后不再开新仓。"""
        rt = self._rt(quotes=_quotes("600001", "600002"))
        _seed_state(date="2026-10-15", prepare=True, morning=True, sell=True)
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 21), "2026-10-15", dtime(10, 21))
        self.assertEqual(rt.gw.placed, [], "过了 10:20 不得开新仓")
        self.assertEqual(rt.gw.cancelled, [], "过了 10:20 也不应再撤单折腾")

    def test_switch_off_behaves_as_before(self):
        """闸⑥：osc_backfill_enabled=False ⇒ 不撤、不补、不留痕（与上线前一致）。"""
        rt = self._rt(params={"osc_backfill_enabled": False, "osc_enabled": True})
        _seed_state(date="2026-10-15", prepare=True, morning=True, sell=True)
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 8), "2026-10-15", dtime(10, 8))
        self.assertEqual(rt.gw.placed, [])
        self.assertEqual(rt.gw.cancelled, [])
        self.assertFalse([a for a in rt.audits if a.get("event") == "osc_backfill"],
                         "开关关闭时不应有任何补单审计事件")


class TestShiftTrailAndBook(unittest.TestCase):
    """闸⑧：留痕（round/shift_from + backfill_rounds）与持仓账本并入。"""

    def setUp(self):
        osc_hooks.reset_day("2026-10-15")
        self.book_dir = tempfile.mkdtemp()
        # 首轮 fills 底账：600001 挂单未成交
        fills0 = {"date": "2026-10-15", "mode": "live", "strategy": "oversold_gap_confirm",
                  "picks": [{"code": "600001", "gap": 0.1, "board": "半导体"}],
                  "spares": [{"code": "600002", "gap": 0.08, "board": "半导体", "price": 10.0}],
                  "buys": _fills_with_pending()["buys"],
                  "summary": {"candidates": 1, "buys_planned": 1, "buys_placed": 1,
                              "rejected_count": 0, "deferred_count": 0,
                              "notional_placed": 10000.0, "cash_before": 400000.0}}
        with open(os.path.join(self.book_dir, "fills_2026-10-15.json"), "w",
                  encoding="utf-8") as f:
            json.dump(fills0, f, ensure_ascii=False)

    def _read_fills(self):
        with open(os.path.join(self.book_dir, "fills_2026-10-15.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_round_shift_from_trail_and_book_registration(self):
        """端到端：撤 600001 → 顺移 600002 成交 ⇒ fills 带 round/shift_from，账本有 600002。"""
        rt = FakeRT(params={"osc_backfill_enabled": True, "osc_enabled": True,
                            "osc_single_budget": 100000.0, "osc_daily_budget": 400000.0},
                    book_dir=self.book_dir, quotes=_quotes("600001", "600002"))
        rt.gw = FakeGateway(cancel_status=5, place_fills=True)
        _seed_state()
        n = osc_hooks.try_backfill(rt, None, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 1)

        fills = self._read_fills()
        by_code = {b["code"]: b for b in fills["buys"]}
        self.assertIn("600002", by_code, "顺移买入必须并入 fills.buys")
        self.assertEqual(by_code["600002"]["round"], 1, "顺移单必须带 round 留痕")
        self.assertEqual(by_code["600002"]["shift_from"], ["600001"],
                         "shift_from 必须指向被撤单释放额度的票")
        self.assertNotIn("round", by_code["600001"], "首轮记录不得有 round 键（以此区分首单/补单）")
        rounds = fills.get("backfill_rounds") or []
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["cancel"]["released"], ["600001"])
        self.assertEqual(by_code["600001"]["status"], "cancelled",
                         "released 必须先落 cancelled，防止重启后pending复活")
        self.assertEqual(fills["summary"]["buys_placed"], 1,
                         "已撤首单不计有效placed，顺移成交单计入；summary按最新fills重算")

        book = wse.load_book(self.book_dir)
        self.assertIn("600002", book["open"], "顺移成交必须并入持仓账本（否则卖侧看不见）")
        self.assertGreater(int(book["open"]["600002"]["qty"]), 0)
        self.assertEqual(book["open"]["600002"]["board"], "半导体")

    def test_merge_idempotent_per_round(self):
        """同 round 重入 ⇒ 不重复追加（进程重启/重放的第二道保险）。"""
        res = {"cancel": {"released": ["600001"], "unconfirmed": [], "failed": [],
                          "filled": []},
               "cash_budget": 390000.0, "skipped": [], "note": ""}
        placed = [{"code": "600002", "qty": 1000, "price": 10.0, "status": "filled",
                   "filled_qty": 1000, "filled_price": 10.0, "qty_remaining": 0,
                   "message": "", "order_id": "o_new_1", "gap": 0.08}]
        r1 = wse.merge_backfill_fills(self.book_dir, "2026-10-15", 1, res,
                                      placed=placed, shift_from=["600001"],
                                      log=lambda *a: None)
        r2 = wse.merge_backfill_fills(self.book_dir, "2026-10-15", 1, res,
                                      placed=placed, shift_from=["600001"],
                                      log=lambda *a: None)
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        fills = self._read_fills()
        self.assertEqual(sum(1 for b in fills["buys"] if b["code"] == "600002"), 1,
                         "同 round 重入不得重复追加 buys")
        self.assertEqual(len(fills["backfill_rounds"]), 1)

    def test_merge_missing_fills_file_returns_none(self):
        """首轮 fills 缺失 ⇒ 不新建、返回 None（钩子上层吞掉，不阻断主循环）。"""
        empty_dir = tempfile.mkdtemp()
        r = wse.merge_backfill_fills(empty_dir, "2026-10-15", 1, {},
                                     placed=[], shift_from=[], log=lambda *a: None)
        self.assertIsNone(r)
        self.assertFalse(os.path.exists(os.path.join(empty_dir, "fills_2026-10-15.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
