# -*- coding: utf-8 -*-
"""S1 执行器单元测试（2026-10-10，W3 执行集成）。

守六件事：
  T1 picks 解析：缺失/日期不符/权重非法 → PicksError（fail-closed）；符号归一。
  T2 卖侧计划：清仓计划生成（bootstrap 账本切换）、T+1 钳制、整清零碎股放行。
  T3 买侧计划：100 股取整、已持仓抵扣、rank 顺序现金截断、停牌顺延。
  T4 主流程关键分支：卖单异常重试、卖单被拒回款不入预算、现金不足截断、
     停牌票顺延、T+1 拦截留痕、产物三件套（fills/report/equity）落盘且
     equity 带「S1账本口径」标签、prev_equity 只接 S1 链。
  T5 参数接线：S1 开关必须落在 PARAMS 且为布尔（B7 错放表前科的回归护栏）；
     S1 on 时 align/tail/legacy 默认挂起。
  T6 gm_main 静态检查：OPEN_ALIGN 已被 _s1_open_align_allowed 门控；S1 盘初
     钩子带 s1_mode+MODE_LIVE 双闸；旧引擎跳过点在 bar 累积/_ogr_pc_cache 之后；
     Gateway 下单前 write_order（孤儿闸前科）；s1_executor 不 import gm。

运行：
    "C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" -m unittest tests.phase3.test_s1_executor -v
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTO = os.path.join(_ROOT, "execution", "auto")
_CFG = os.path.join(_AUTO, "_gm", "config")
for _p in (_ROOT, _AUTO, _CFG):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import s1_executor as s1e  # noqa: E402

GMM_PATH = os.path.join(_AUTO, "gm_main.py")
DATE = "2026-10-12"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write_picks(book, date=DATE, sells=None, buys=None, holds=None, meta=None):
    os.makedirs(os.path.join(book, "picks"), exist_ok=True)
    fp = os.path.join(book, "picks", f"picks_{date}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"date": date, "sells": sells or [], "buys": buys or [],
                   "holds": holds or [], "meta": meta or {"n_slots": 4}},
              f, ensure_ascii=False)
    return fp


class FakeGateway:
    """行为表：{code: "fill"|"reject"|"error"|"partial"}；error 前 N 次抛异常后转 fill。"""

    def __init__(self, cash=0.0, positions=None, prices=None, behavior=None,
                 error_times=None):
        self.cash = float(cash)
        self.pos = dict(positions or {})
        self.px = dict(prices or {})
        self.bh = dict(behavior or {})
        self.error_times = dict(error_times or {})
        self.orders = []
        self.calls = {}
        self._seq = 0

    def get_cash(self):
        return self.cash

    def get_positions(self):
        return self.pos

    def get_open_price(self, code):
        return self.px.get(code)

    def place_order(self, code, side, qty, price):
        self.calls[code] = self.calls.get(code, 0) + 1
        if self.error_times.get(code, 0) > 0:
            self.error_times[code] -= 1
            raise RuntimeError("boom")
        self._seq += 1
        act = self.bh.get(code, "fill")
        px = float(price or self.px.get(code) or 0)
        if act == "reject":
            st, fq, fp = "rejected", 0, None
        elif act == "partial":
            st, fq, fp = "partial", max(100, (qty // 2 // 100) * 100), px
        else:
            st, fq, fp = "filled", int(qty), px
        rec = {"order_id": f"F{self._seq}", "status": st, "filled_qty": fq,
               "filled_price": fp, "message": act if act != "fill" else ""}
        self.orders.append({"code": code, "side": side, "qty": int(qty), **rec})
        return rec

    def poll_order(self, order_id, code):
        for o in self.orders:
            if o["order_id"] == order_id and o["status"] == "partial":
                o["status"] = "filled"
                o["filled_qty"] = o["qty"]
                return {k: o[k] for k in ("order_id", "status", "filled_qty",
                                          "filled_price", "message")}
        return {"order_id": order_id, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": ""}


def _run(gw, book, metrics, **kw):
    p = {"order_poll_sleep_sec": 0.001}
    p.update(kw.pop("params", {}))
    return s1e.run_open_exec(gw, DATE, book_dir=book, metrics_dir=metrics,
                             params=p, dry_run=True, sleep_fn=lambda s: None,
                             log=lambda *a: None, **kw)


# ════════════════════════ T1 picks 解析 ════════════════════════

class TestT1PicksParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_missing_file_raises(self):
        with self.assertRaises(s1e.PicksError):
            s1e.load_picks(DATE, self.tmp)

    def test_date_mismatch_raises(self):
        _write_picks(self.tmp, date="2026-10-11")
        with self.assertRaises(s1e.PicksError):
            s1e.load_picks(DATE, self.tmp)

    def test_bad_weight_raises(self):
        _write_picks(self.tmp, buys=[{"symbol": "300456", "target_weight": 0}])
        with self.assertRaises(s1e.PicksError):
            s1e.load_picks(DATE, self.tmp)

    def test_bad_sell_qty_raises(self):
        _write_picks(self.tmp, sells=[{"symbol": "300456", "qty": -100}])
        with self.assertRaises(s1e.PicksError):
            s1e.load_picks(DATE, self.tmp)

    def test_symbol_normalization(self):
        self.assertEqual(s1e.code_of("SHSE.600481"), "600481")
        self.assertEqual(s1e.code_of("600481.SH"), "600481")
        self.assertEqual(s1e.code_of("szse_300456"), "300456")
        self.assertEqual(s1e.code_of("600481"), "600481")
        self.assertEqual(s1e.code_of("garbage"), "")
        self.assertEqual(s1e.gm_symbol_of("600481"), "SHSE.600481")
        self.assertEqual(s1e.gm_symbol_of("300456"), "SZSE.300456")
        self.assertEqual(s1e.gm_symbol_of("688008"), "SHSE.688008")

    def test_holds_accept_plain_strings(self):
        _write_picks(self.tmp, holds=["688008"])
        p = s1e.load_picks(DATE, self.tmp)
        self.assertEqual(p["holds"][0]["code"], "688008")

    def test_run_raises_picks_error_fail_closed(self):
        gw = FakeGateway(cash=1e6)
        with self.assertRaises(s1e.PicksError):
            _run(gw, self.tmp, os.path.join(self.tmp, "m"))
        self.assertEqual(gw.orders, [], "picks 不可用时绝不下单")


# ════════════════════════ T2 卖侧计划 ════════════════════════

class TestT2SellPlan(unittest.TestCase):
    def test_liquidation_plan_for_non_s1_holdings(self):
        picks = {"sells": [], "holds": [{"code": "300456"}],
                 "buys": [{"code": "688008", "target_weight": 0.5}]}
        pos = {"600481": {"qty": 1400, "available": 1400, "cost": 3.9},
               "000988": {"qty": 500, "available": 500, "cost": 37.6},
               "300456": {"qty": 800, "available": 800, "cost": 45.0}}
        plan = s1e.build_sell_plan(picks, pos)
        liq = [s for s in plan if s["source"] == "liquidation"]
        self.assertEqual({s["code"] for s in liq}, {"600481", "000988"},
                         "非 buys∪holds 名单的旧持仓全部进清仓计划")
        self.assertNotIn("300456", {s["code"] for s in liq}, "holds 票不清仓")
        self.assertNotIn("688008", {s["code"] for s in liq}, "buys 票不清仓")

    def test_t1_clamp(self):
        picks = {"sells": [{"code": "300456", "qty": 1200}], "holds": [], "buys": []}
        pos = {"300456": {"qty": 1200, "available": 400, "cost": 45.0}}
        s = s1e.build_sell_plan(picks, pos)[0]
        self.assertEqual(s["qty_place"], 400)
        self.assertEqual(s["t1_blocked"], 800)

    def test_full_close_odd_lot_allowed(self):
        """整清时零碎股一次性卖出（不清仓则 floor 到 100）。"""
        picks = {"sells": [{"code": "300456", "qty": 350}], "holds": [], "buys": []}
        pos = {"300456": {"qty": 350, "available": 350, "cost": 45.0}}
        s = s1e.build_sell_plan(picks, pos)[0]
        self.assertTrue(s["full_close"])
        self.assertEqual(s["qty_place"], 350, "整清放行零碎股")

    def test_no_position_sell_kept_for_audit(self):
        picks = {"sells": [{"code": "300456", "qty": 500}], "holds": [], "buys": []}
        s = s1e.build_sell_plan(picks, {})[0]
        self.assertEqual(s["qty_place"], 0, "无持仓卖单保留 0 量条目留痕")


# ════════════════════════ T3 买侧计划 ════════════════════════

class TestT3BuyPlan(unittest.TestCase):
    def _picks(self, tws):
        return {"sells": [], "holds": [],
                "buys": [{"code": c, "target_weight": tw} for c, tw in tws]}

    def test_lot_rounding_and_held_offset(self):
        picks = self._picks([("300456", 0.5)])
        pos = {"300456": {"qty": 200, "available": 200, "cost": 45.0}}
        plan = s1e.build_buy_plan(picks, pos, {"300456": 45.0},
                                  cash_budget=1e6, equity_est=100000)
        b = plan[0]
        # target 50000，已持 200×45=9000 ⇒ 缺口 41000 ⇒ 911 股 → 900
        self.assertEqual(b["qty_place"], 900)
        self.assertEqual(b["held_value"], 9000.0)

    def test_cash_truncation_by_rank_order(self):
        picks = self._picks([("300456", 0.4), ("688008", 0.4),
                             ("600276", 0.4), ("300153", 0.4)])
        prices = {"300456": 45.0, "688008": 60.0, "600276": 44.0, "300153": 12.0}
        plan = s1e.build_buy_plan(picks, {}, prices, cash_budget=60000,
                                  equity_est=60000)
        self.assertEqual([b["qty_place"] for b in plan], [500, 400, 300, 0])
        self.assertTrue(plan[2]["truncated"])
        self.assertTrue(plan[3]["truncated"])
        self.assertIn("cash_insufficient", plan[3]["defer_reason"])
        self.assertFalse(plan[0]["truncated"])

    def test_suspended_buy_deferred(self):
        picks = self._picks([("300153", 0.5)])
        plan = s1e.build_buy_plan(picks, {}, {"300153": None},
                                  cash_budget=1e6, equity_est=100000)
        self.assertEqual(plan[0]["qty_place"], 0)
        self.assertIn("no_price", plan[0]["defer_reason"])

    def test_below_min_lot_skipped(self):
        picks = self._picks([("300456", 0.01)])     # 1000 元目标 < 一手 4500
        plan = s1e.build_buy_plan(picks, {}, {"300456": 45.0},
                                  cash_budget=1e6, equity_est=100000)
        self.assertEqual(plan[0]["qty_place"], 0)
        self.assertIn("below_min_lot", plan[0]["defer_reason"])


# ════════════════════════ T4 主流程分支 ════════════════════════

class TestT4RunBranches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.book = os.path.join(self.tmp, "book")
        self.metrics = os.path.join(self.tmp, "metrics")

    def test_bootstrap_liquidation_then_buys(self):
        """清仓计划先行：先卖旧持仓、再买 Top-4，且卖单先于买单下单。"""
        _write_picks(self.book,
                     buys=[{"symbol": "300456", "target_weight": 0.25},
                           {"symbol": "688008", "target_weight": 0.25}])
        gw = FakeGateway(cash=20000,
                         positions={"600481": {"qty": 1400, "available": 1400, "cost": 3.9}},
                         prices={"600481": 3.95, "300456": 45.0, "688008": 60.0})
        res = _run(gw, self.book, self.metrics)
        self.assertTrue(res["bootstrap_liquidation"])
        sides = [o["side"] for o in gw.orders]
        self.assertEqual(sides[0], "SELL", "必须先卖后买")
        self.assertEqual(res["summary"]["liquidation_count"], 1)
        self.assertEqual(res["summary"]["buys_placed"], 2)

    def test_sell_error_retried_then_excluded_from_budget(self):
        """卖单异常重试 sell_retries 次仍失败 → error，回款不入预算。"""
        _write_picks(self.book,
                     sells=[{"symbol": "600481", "qty": 1400}],
                     buys=[{"symbol": "300456", "target_weight": 0.9}])
        gw = FakeGateway(cash=1000,
                         positions={"600481": {"qty": 1400, "available": 1400, "cost": 3.9}},
                         prices={"600481": 3.95, "300456": 45.0},
                         error_times={"600481": 99})
        res = _run(gw, self.book, self.metrics, params={"sell_retries": 2})
        s = res["sells"][0]
        self.assertEqual(s["status"], "error")
        self.assertEqual(gw.calls["600481"], 3, "首次 + 2 次重试")
        b = res["buys"][0]
        self.assertEqual(b["qty_placed"], 0, "卖单失败回款不得入买侧预算")

    def test_sell_reject_excluded_from_budget(self):
        _write_picks(self.book,
                     sells=[{"symbol": "600481", "qty": 1400}],
                     buys=[{"symbol": "300456", "target_weight": 0.9}])
        gw = FakeGateway(cash=1000,
                         positions={"600481": {"qty": 1400, "available": 1400, "cost": 3.9}},
                         prices={"600481": 3.95, "300456": 45.0},
                         behavior={"600481": "reject"})
        res = _run(gw, self.book, self.metrics)
        self.assertEqual(res["sells"][0]["status"], "rejected")
        self.assertEqual(res["buys"][0]["qty_placed"], 0)
        self.assertEqual(res["summary"]["rejected_count"], 1)

    def test_sell_proceeds_fund_buys(self):
        """卖出回款当日可用：现金 0 也能用预计回款×haircut 买入。"""
        _write_picks(self.book,
                     sells=[{"symbol": "600276", "qty": 2000}],
                     buys=[{"symbol": "300456", "target_weight": 0.8}])
        gw = FakeGateway(cash=0,
                         positions={"600276": {"qty": 2000, "available": 2000, "cost": 44.0}},
                         prices={"600276": 44.0, "300456": 45.0})
        res = _run(gw, self.book, self.metrics)
        # 回款 88000×0.999=87912；target 0.8×88000=70400 ⇒ 1500 股（67500 ≤ 87912）
        self.assertEqual(res["buys"][0]["qty_placed"], 1500)

    def test_t1_blocked_recorded_in_fills(self):
        _write_picks(self.book, sells=[{"symbol": "300456", "qty": 1200}])
        gw = FakeGateway(cash=0,
                         positions={"300456": {"qty": 1200, "available": 400, "cost": 45.0}},
                         prices={"300456": 46.0})
        res = _run(gw, self.book, self.metrics)
        self.assertEqual(res["sells"][0]["t1_blocked"], 800)
        self.assertEqual(res["summary"]["t1_blocked_count"], 1)

    def test_partial_fill_polled_to_filled(self):
        _write_picks(self.book, sells=[{"symbol": "600276", "qty": 2000}])
        gw = FakeGateway(cash=0,
                         positions={"600276": {"qty": 2000, "available": 2000, "cost": 44.0}},
                         prices={"600276": 44.0}, behavior={"600276": "partial"})
        res = _run(gw, self.book, self.metrics)
        self.assertEqual(res["sells"][0]["status"], "filled",
                         "partial 经轮询翻 filled 终态")

    def test_artifacts_written_with_s1_source_tag(self):
        _write_picks(self.book,
                     buys=[{"symbol": "300456", "target_weight": 0.5}])
        gw = FakeGateway(cash=100000, prices={"300456": 45.0})
        res = _run(gw, self.book, self.metrics)
        for k in ("fills", "report", "equity"):
            self.assertTrue(os.path.exists(res["_paths"][k]), k)
        with open(res["_paths"]["equity"], encoding="utf-8") as f:
            eq = json.load(f)
        self.assertIn("S1账本口径", eq["source"])
        self.assertIsNone(eq["account_ret"], "S1 链首日 account_ret=null")
        self.assertGreater(eq["equity"], 0)
        with open(res["_paths"]["fills"], encoding="utf-8") as f:
            fills = json.load(f)
        self.assertEqual(fills["fee_model"], "core/cost_model.py",
                         "成本真源必须来自 core/cost_model.py")

    def test_prev_equity_chains_only_s1_files(self):
        """S1 净值链自接续；且不误接 equity_daily 的「仿真账户口径」文件。"""
        _write_picks(self.book, buys=[{"symbol": "300456", "target_weight": 0.5}])
        gw = FakeGateway(cash=100000, prices={"300456": 45.0})
        _run(gw, self.book, self.metrics)
        # 干扰项：仿真账户口径文件（必须被跳过）
        with open(os.path.join(self.metrics, "equity_s1_daily_2026-10-09.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"date": "2026-10-09", "equity": 999999,
                       "source": "仿真账户口径(heartbeat尾行+K线重估)"}, f)
        # 次日在链上跑（借首日 fills 目录换日期）
        d2 = "2026-10-13"
        _write_picks(self.book, date=d2,
                     buys=[{"symbol": "300456", "target_weight": 0.5}])
        s1e.run_open_exec(FakeGateway(cash=90000, prices={"300456": 45.0}), d2,
                          book_dir=self.book, metrics_dir=self.metrics,
                          params={"order_poll_sleep_sec": 0.001}, dry_run=True,
                          sleep_fn=lambda s: None, log=lambda *a: None)
        with open(os.path.join(self.metrics, f"equity_s1_daily_{d2}.json"),
                  encoding="utf-8") as f:
            eq2 = json.load(f)
        self.assertIsNotNone(eq2["prev_equity"])
        self.assertNotEqual(eq2["prev_equity"], 999999,
                            "仿真账户口径文件不得进入 S1 净值链")
        self.assertIsNotNone(eq2["account_ret"])

    def test_price_missing_buy_deferred_and_recorded(self):
        _write_picks(self.book, buys=[{"symbol": "300153", "target_weight": 0.5}])
        gw = FakeGateway(cash=100000, prices={"300153": None})
        res = _run(gw, self.book, self.metrics)
        b = res["buys"][0]
        self.assertEqual(b["status"], "skipped")
        self.assertIn("no_price", b["defer_reason"])
        self.assertIn("300153", res["price_missing"])
        self.assertEqual(gw.orders, [], "停牌票不下单")


# ════════════════════════ T5 参数接线 ════════════════════════

class TestT5ParamsWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = _load(os.path.join(_CFG, "params.py"), "cp_s1_test")

    def test_s1_flags_in_params_not_index_regime(self):
        """S1 开关必须落在 PARAMS（gm_main 闸函数读它）——B7 错放表前科的回归护栏。"""
        for k in ("s1_mode_enabled", "s1_open_align_enabled",
                  "s1_tail_enabled", "s1_legacy_engine_enabled"):
            self.assertIn(k, self.p.PARAMS, f"{k} 必须在 PARAMS")
            self.assertNotIn(k, self.p.INDEX_REGIME_PARAMS, f"{k} 不得在 INDEX_REGIME_PARAMS")
            self.assertIsInstance(self.p.PARAMS[k], bool, f"{k} 应为布尔")

    def test_s1_on_suspends_align_tail_legacy_by_default(self):
        """owner 账本切换裁决：S1_MODE=on 时 align/tail/旧引擎默认挂起。"""
        self.assertTrue(self.p.PARAMS["s1_mode_enabled"])
        self.assertFalse(self.p.PARAMS["s1_open_align_enabled"])
        self.assertFalse(self.p.PARAMS["s1_tail_enabled"])
        self.assertFalse(self.p.PARAMS["s1_legacy_engine_enabled"])

    def test_exec_params_present(self):
        for k in ("s1_proceeds_haircut", "s1_min_lot", "s1_order_poll_rounds",
                  "s1_order_poll_sleep_sec", "s1_sell_retries"):
            self.assertIn(k, self.p.PARAMS, k)

    def test_existing_keys_untouched(self):
        """S1 段是纯追加：OGR 影子/做T既有键不受影影响。"""
        self.assertIn("open_gap_reversal_shadow_enabled", self.p.PARAMS)
        self.assertIn("open_gap_reversal_live_enabled", self.p.PARAMS)
        self.assertIn("max_concurrent_positions", self.p.PARAMS)
        self.assertIn("000988", self.p.STOCK_PARAMS)


# ════════════════════════ T6 gm_main 静态检查 ════════════════════════

class TestT6GmMainStatics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(GMM_PATH, encoding="utf-8") as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)

    def test_open_align_gated_by_s1(self):
        """OPEN_ALIGN 触发条件必须带 _s1_open_align_allowed()。"""
        i = self.src.rfind("_force_open_align(context)")   # rfind=on_bar 调用点（非 def 行）
        self.assertGreater(i, 0)
        window = self.src[max(0, i - 700):i]
        self.assertIn("_s1_open_align_allowed()", window,
                      "OPEN_ALIGN 必须由 _s1_open_align_allowed() 门控（S1 接管挂起）")

    def test_s1_hook_has_double_gate(self):
        """S1 盘初钩子：s1_mode + MODE_LIVE 双闸 + 每日一次标记。"""
        i = self.src.rfind("_s1_open_exec(context, now, today)")   # rfind=调用点（非 def 行）
        self.assertGreater(i, 0)
        window = self.src[max(0, i - 500):i]
        self.assertIn("_s1_mode_on()", window)
        self.assertIn("MODE_LIVE", window)
        self.assertIn("_S1_EXEC_DONE_DATE", window)

    def test_s1_hook_fail_closed_on_picks_error(self):
        """picks 不可用 → 当日不下单 + risk 告警，绝不回退旧引擎。"""
        i = self.src.find("def _s1_open_exec")
        seg = self.src[i:i + 2200]
        self.assertIn("PicksError", seg)
        self.assertIn("write_risk", seg)
        self.assertIn("fail-closed", seg)

    def test_legacy_skip_after_bar_accumulation(self):
        """旧引擎跳过点必须在 _ogr_pc_cache 更新之后（保 OGR 影子/心跳数据链）。"""
        i_cache = self.src.find("context._ogr_pc_cache[code] = row[\"close\"]")
        i_skip = self.src.find("if not _s1_legacy_active():")
        self.assertGreater(i_cache, 0)
        self.assertGreater(i_skip, i_cache,
                           "_s1_legacy_active 跳过必须在 bar 累积/_ogr_pc_cache 之后")

    def test_gateway_writes_order_before_ordering(self):
        """Gateway 下单前必须 write_order（孤儿闸登记，6a96829c 前科）。"""
        i = self.src.find("class _S1GmGateway")
        seg = self.src[i:i + 5200]
        i_wo = seg.find('write_order(str(now), code, side')
        i_ov = seg.find("order_volume, symbol=sym")
        self.assertGreater(i_wo, 0)
        self.assertGreater(i_ov, i_wo, "write_order 必须先于 order_volume")
        self.assertIn('order_type="S1"', seg, "S1 订单事件需可区分账本")

    def test_s1_executor_imports_no_gm(self):
        """执行器核不 import gm.api（可单测/干跑）。"""
        with open(os.path.join(_AUTO, "s1_executor.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertFalse(a.name.startswith("gm"), f"禁 import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("gm"),
                                 f"禁 from {node.module}")

    def test_gm_main_parses_and_hook_funcs_exist(self):
        names = {n.name for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)}
        for fn in ("_s1_mode_on", "_s1_open_align_allowed", "_s1_legacy_active",
                   "_s1_open_exec"):
            self.assertIn(fn, names, fn)
        classes = {n.name for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)}
        self.assertIn("_S1GmGateway", classes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
