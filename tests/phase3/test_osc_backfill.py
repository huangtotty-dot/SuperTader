# -*- coding: utf-8 -*-
"""超跌跳空确认 · **买不进替补（顺移）** 单测 —— 2026-10-14。

**为什么有这份测试**：2026-10-14 首日 gap 最大的两只全天封死涨停、19.8 万冻结到 15:00
（详见 `oversold_gap_confirm/backfill.py` 模块 docstring）。机制本身是"撤单 + 复检 + 顺移"，
每一步都**动钱**（撤错、复检漏、额度算错 = 实盘事故）⇒ 逐条钉住。

本文件覆盖 **纯函数层**（`backfill.py` 的谓词与账）与**编排层**（`cancel_and_confirm`，
用真的 `OscGateway` + 桩 gm，与 `test_framework_ownership_gateway.py` 同款风格）。

运行：python -m unittest tests.phase3.test_osc_backfill -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import backfill as bf                          # noqa: E402
from oversold_gap_confirm import executor as wse                         # noqa: E402


# ════════════════════════ 涨停价分档 ════════════════════════

class TestLimitUpPrice(unittest.TestCase):
    def test_main_board_10cm(self):
        # 600097 事故当日的真实数字：昨收 9.27 → 涨停 10.20
        self.assertEqual(bf.limit_up_price(9.27, "600097"), 10.20)

    def test_gem_and_star_20cm(self):
        self.assertEqual(bf.limit_ratio("300123"), 1.20)
        self.assertEqual(bf.limit_ratio("301373"), 1.20)
        self.assertEqual(bf.limit_ratio("688003"), 1.20)
        self.assertEqual(bf.limit_up_price(100.80, "301373"), 120.96)   # 事故票 301373
        self.assertEqual(bf.limit_up_price(22.32, "000823"), 24.55)     # 事故票 000823

    def test_half_up_not_bankers_rounding(self):
        """10.05×1.1 = 11.055 ⇒ 半进位必须给 11.06；Python round() 会给 11.06?
        实际 round(11.055,2) 因二进制表示常给 11.05/11.06 不定 ⇒ 用 floor(x*100+0.5) 钉死。"""
        self.assertEqual(bf.limit_up_price(10.05, "600000"), 11.06)
        self.assertEqual(bf.limit_up_price(2.05, "600000"), 2.26)       # 2.255 → 2.26

    def test_invalid_prev_close(self):
        for bad in (None, 0, -1, float("nan"), "abc"):
            self.assertIsNone(bf.limit_up_price(bad, "600000"), f"{bad!r} 应返回 None")


# ════════════════════════ VWAP 与量纲自检 ════════════════════════

class TestDayVwap(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(bf.day_vwap(1234.0, 100.0), 12.34)

    def test_missing_or_nonpositive(self):
        for a, v in ((None, 100), (100, None), (100, 0), (0, 100), ("x", 1)):
            self.assertIsNone(bf.day_vwap(a, v), f"({a},{v}) 应判不可用")

    def test_dimension_selfcheck_rejects_100x(self):
        """量纲护栏：cum_volume 若是「手」而 cum_amount 是「元」⇒ VWAP 大 100× ⇒ 必须判不可用。
        这条若挂了，顺移会**静默失效**（price>=vwap 恒假），且不报任何错。"""
        price = 10.0
        self.assertTrue(bf.vwap_sane(9.9, price))
        self.assertFalse(bf.vwap_sane(990.0, price), "100× 量纲错必须被挡住")
        self.assertFalse(bf.vwap_sane(0.0, price))
        self.assertFalse(bf.vwap_sane(None, price))


# ════════════════════════ 封死判据 ════════════════════════

class TestIsSealed(unittest.TestCase):
    def test_sealed_at_limit_no_ask(self):
        self.assertTrue(bf.is_sealed(10.20, 10.20, 0))

    def test_limit_price_but_ask_available_not_sealed(self):
        """涨停价上仍有卖单 ⇒ 能买进，不算封死（600097 当日涨停价上仍成交约 3 万手）。"""
        self.assertFalse(bf.is_sealed(10.20, 10.20, 500))

    def test_below_limit_not_sealed(self):
        self.assertFalse(bf.is_sealed(10.19, 10.20, 0))

    def test_missing_ask_degrades_to_price_only(self):
        """盘口缺失 ⇒ 降级只看价位（保守：多跳过）。"""
        self.assertTrue(bf.is_sealed(10.20, 10.20, None))


# ════════════════════════ 时效复检真值表 ════════════════════════

class TestRecheckOk(unittest.TestCase):
    PC = 10.0

    def test_pass(self):
        ok, why = bf.recheck_ok(10.5, self.PC, 10.2, 300, 11.0)
        self.assertTrue(ok, why)
        self.assertEqual(why, "ok")

    def test_below_prev_close(self):
        ok, why = bf.recheck_ok(9.8, self.PC, 9.5, 300, 11.0)
        self.assertFalse(ok)
        self.assertEqual(why, "below_prev_close")

    def test_below_vwap(self):
        ok, why = bf.recheck_ok(10.5, self.PC, 10.8, 300, 11.0)
        self.assertFalse(ok)
        self.assertEqual(why, "below_vwap")

    def test_sealed_skipped_first(self):
        """封死优先判定 —— 事故票就长这样（价=涨停、无卖量）。"""
        ok, why = bf.recheck_ok(11.0, self.PC, 10.5, 0, 11.0)
        self.assertFalse(ok)
        self.assertEqual(why, "limit_sealed")

    def test_vwap_missing_fail_closed(self):
        ok, why = bf.recheck_ok(10.5, self.PC, None, 300, 11.0)
        self.assertFalse(ok)
        self.assertEqual(why, "vwap_unavailable")

    def test_no_data(self):
        for px, pc in ((None, 10.0), (10.0, None), (0, 10.0), (10.0, 0)):
            ok, why = bf.recheck_ok(px, pc, 9.0, 100, 11.0)
            self.assertFalse(ok)
            self.assertEqual(why, "no_data", f"({px},{pc})")


# ════════════════════════ 候补构建 ════════════════════════

class TestBuildSpares(unittest.TestCase):
    @staticmethod
    def _sig(code, gap, decision="trade"):
        return {"code": code, "gap": gap, "board": "半导体", "decision": decision}

    def test_takes_signals_beyond_top_n_in_gap_order(self):
        sigs = [self._sig("600001", 0.10), self._sig("600002", 0.09),
                self._sig("600003", 0.08), self._sig("600004", 0.07),
                self._sig("600005", 0.06), self._sig("600006", 0.05)]
        bars = {f"60000{i}": {"close": 10.0 + i} for i in range(1, 7)}
        spares = bf.build_spares(sigs, pick_codes=["600001", "600002"], done_codes=[],
                                 bars=bars)
        self.assertEqual([s["code"] for s in spares], ["600003", "600004", "600005", "600006"])
        self.assertEqual(spares[0]["price"], 13.0)

    def test_excludes_board_filtered(self):
        """被板块过滤的 NO_TRADE 不能顺移 —— 那是明确判过"不该买"的。"""
        sigs = [self._sig("600001", 0.10), self._sig("600900", 0.09, decision="no_trade")]
        spares = bf.build_spares(sigs, ["600001"], [], bars={"600900": {"close": 10.0}})
        self.assertEqual(spares, [])

    def test_excludes_done_codes(self):
        sigs = [self._sig("600001", 0.10), self._sig("600002", 0.09)]
        spares = bf.build_spares(sigs, ["600001"], ["600002"],
                                 bars={"600002": {"close": 10.0}})
        self.assertEqual(spares, [])

    def test_missing_10am_bar_skipped(self):
        sigs = [self._sig("600001", 0.10), self._sig("600002", 0.09)]
        spares = bf.build_spares(sigs, ["600001"], [], bars={})
        self.assertEqual(spares, [], "无 10:00 棒 ⇒ 无法折算股数 ⇒ fail-closed 跳过")


# ════════════════════════ 待补单与额度账 ════════════════════════

class TestPendingAndCommitted(unittest.TestCase):
    @staticmethod
    def _fills(rows):
        return {"buys": rows}

    def test_pending_includes_submitted_and_partial_excludes_terminal(self):
        f = self._fills([
            {"code": "600001", "order_id": "o1", "status": "submitted", "qty_placed": 1000,
             "filled_qty": 0, "price": 10.0, "gap": 0.1},
            {"code": "600002", "order_id": "o2", "status": "partial", "qty_placed": 1000,
             "filled_qty": 400, "price": 20.0, "gap": 0.09},
            {"code": "600003", "order_id": "o3", "status": "filled", "qty_placed": 1000,
             "filled_qty": 1000, "price": 30.0},
            {"code": "600004", "order_id": "o4", "status": "rejected", "qty_placed": 1000,
             "filled_qty": 0, "price": 40.0},
            {"code": "600005", "order_id": "o5", "status": "expired", "qty_placed": 1000,
             "filled_qty": 0, "price": 50.0},
        ])
        pend = bf.pending_from_fills(f)
        self.assertEqual([p["code"] for p in pend], ["600001", "600002"])
        self.assertEqual(pend[0]["qty_remaining"], 1000)
        self.assertEqual(pend[1]["qty_remaining"], 600)
        self.assertEqual(pend[0]["ref_price"], 10.0)

    def test_pending_prefers_filled_price_as_ref(self):
        f = self._fills([{"code": "600001", "order_id": "o1", "status": "partial",
                          "qty_placed": 1000, "filled_qty": 400, "price": 10.0,
                          "filled_price": 10.31}])
        self.assertAlmostEqual(bf.pending_from_fills(f)[0]["ref_price"], 10.31)

    def test_committed_counts_filled_plus_unconfirmed_frozen(self):
        f = self._fills([
            {"code": "600001", "order_id": "o1", "status": "submitted", "qty_placed": 1000,
             "filled_qty": 0, "price": 10.0},                       # 全冻 10000
            {"code": "600002", "order_id": "o2", "status": "partial", "qty_placed": 1000,
             "filled_qty": 400, "price": 20.0, "filled_price": 20.0},  # 成交 8000 + 冻 12000
            {"code": "600003", "order_id": "o3", "status": "rejected", "qty_placed": 1000,
             "filled_qty": 0, "price": 30.0},                       # 终态 ⇒ 0
        ])
        c = bf.committed_notional(f)
        self.assertAlmostEqual(c["600001"], 10000.0)
        self.assertAlmostEqual(c["600002"], 20000.0)
        self.assertNotIn("600003", c)

    def test_released_code_drops_frozen_but_keeps_filled(self):
        """owner 决策 #4：**撤单确认后**才释放额度。已成交部分永远保留。"""
        f = self._fills([{"code": "600002", "order_id": "o2", "status": "submitted",
                          "qty_placed": 1000, "filled_qty": 400, "price": 20.0,
                          "filled_price": 20.0}])
        before = bf.committed_notional(f)["600002"]
        after = bf.committed_notional(f, released_codes=["600002"])["600002"]
        self.assertAlmostEqual(before, 20000.0)
        self.assertAlmostEqual(after, 8000.0, msg="释放的只是未成交冻结那部分")

    def test_budget_for_round(self):
        committed = {"600001": 100000.0, "600002": 50000.0}
        cash, single = bf.budget_for_round(400000.0, 100000.0, committed, cash_free=999999.0)
        self.assertAlmostEqual(cash, 250000.0)
        self.assertAlmostEqual(single, 100000.0)
        # 现金更紧时取现金
        cash2, _ = bf.budget_for_round(400000.0, 100000.0, committed, cash_free=30000.0)
        self.assertAlmostEqual(cash2, 30000.0)
        # 额度用尽
        cash3, _ = bf.budget_for_round(
            100000.0, 100000.0, {"600001": 100000.0}, cash_free=999999.0)
        self.assertAlmostEqual(cash3, 0.0)


# ════════════════════════ 撤单编排（真 gateway + 桩 gm）════════════════════════

class _FakeGm(object):
    """桩 gm 命名空间。可编程：撤单是否可用、get_orders 的回报状态。"""
    OrderSide_Buy, OrderSide_Sell = 1, 2
    OrderType_Market, OrderType_Limit = 1, 2
    PositionEffect_Open, PositionEffect_Close = 1, 2
    ADJUST_PREV = 1

    def __init__(self, orders=None, cancel_ok=True):
        self.calls = []
        self._orders = orders if orders is not None else []
        self._cancel_ok = cancel_ok

    def order_cancel(self, **kw):
        self.calls.append(("order_cancel", kw))
        return {"ok": self._cancel_ok} if not self._cancel_ok else [{"id": kw.get("order_id")}]

    def order_volume(self, **kw):
        self.calls.append(("order_volume", kw))
        return [{"id": "o1", "status": 0}]

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
    """最小 services：`sdk_call` / `reject_statuses` / `code_to_gm`（cancel_and_confirm
    链路里 `poll_order` 要用 code→symbol）。"""
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


class _FakeCtx(object):
    def __init__(self):
        self.now = None


class TestCancelAndConfirm(unittest.TestCase):
    def _gw(self, orders, cancel_ok=True):
        gm = _FakeGm(orders=orders, cancel_ok=cancel_ok)
        from oversold_gap_confirm.gateway import OscGateway
        ctx = _FakeCtx()
        import datetime as _dt
        ctx.now = _dt.datetime(2026, 10, 15, 10, 8, 0)
        acc = type("A", (), {"cash": lambda self: {"available": 1e6},
                             "positions": lambda self: []})()
        ctx.account = (lambda: acc)
        return OscGateway(ctx, _FakeSvc(gm), log=lambda *a: None), gm

    def test_confirmed_cancel_releases(self):
        gw, gm = self._gw([{"id": "o1", "status": 5}])
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": "o1"}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        self.assertEqual(res["released"], ["600001"])
        self.assertEqual(res["unconfirmed"], [])

    def test_pending_cancel_not_treated_as_released(self):
        """⚠️ 本测试钉住一个**既有隐患**：`poll_order` 原先把 status=6(PendingCancel)
        映射成 `rejected`，上层会误以为"钱已解冻"⇒ 超买。撤单确认**只认 5/4**。"""
        gw, gm = self._gw([{"id": "o1", "status": 6}])
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": "o1"}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        self.assertEqual(res["released"], [], "6=PendingCancel 钱还没解冻，不能算释放")
        self.assertEqual(res["unconfirmed"], ["600001"])

    def test_lost_race_counts_as_filled(self):
        gw, gm = self._gw([{"id": "o1", "status": 3, "filled_volume": 1000,
                            "filled_vwap": 10.5}])
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": "o1"}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        self.assertEqual([f["code"] for f in res["filled"]], ["600001"])
        self.assertEqual(res["released"], [])

    def test_cancel_primitive_missing_degrades_not_raises(self):
        """原语缺席（本机 gm 是空桩）⇒ 记 failed，**不抛**、不中断主循环。"""
        class _NoCancelGm(_FakeGm):
            order_cancel = None              # 模拟"该 gm 版本没有撤单原语"
        gm = _NoCancelGm()
        from oversold_gap_confirm.gateway import OscGateway
        import datetime as _dt
        ctx = _FakeCtx()
        ctx.now = _dt.datetime(2026, 10, 15, 10, 8, 0)
        ctx.account = (lambda: type("A", (), {"cash": lambda self: {"available": 1e6},
                                              "positions": lambda self: []})())
        gw = OscGateway(ctx, _FakeSvc(gm), log=lambda *a: None)
        res = bf.cancel_and_confirm(gw, [{"code": "600001", "order_id": "o1"}],
                                    poll_rounds=1, sleep_fn=lambda s: None,
                                    log=lambda *a: None)
        self.assertEqual(res["failed"], ["600001"])
        self.assertEqual(res["released"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
