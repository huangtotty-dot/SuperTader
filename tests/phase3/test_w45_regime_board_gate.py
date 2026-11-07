# -*- coding: utf-8 -*-
"""W45 ㉖+⑳：forming-bar 输入审计与分板买闸定向回归。

执行闸全部注入冻结的 board_regime，不用修复后的 forming 重放结果代替执行验收。
"""
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
_GM = _AUTO / "_gm"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import hooks  # noqa: E402
from tests.phase3.regime_fixtures import valid_board_regime_ctx, valid_regime_ctx  # noqa: E402


def _load_gm_index_regime():
    if str(_GM) not in sys.path:
        sys.path.insert(0, str(_GM))
    name = "_w45_test_gm_index_regime"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _GM / "analysis" / "index_regime.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestFormingBarInputAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ir = _load_gm_index_regime()

    def setUp(self):
        self.ir.GM_DATA_READY = True
        self.ir.GM_INDEX_CACHE.clear()
        self.ir._IR_MEM_CACHE.clear()
        self.target = "2026-11-05"
        self.df = pd.DataFrame([
            {"date": "2026-11-03", "open": 10.0, "high": 10.4, "low": 9.8,
             "close": 10.2, "volume": 1000},
            {"date": "2026-11-04", "open": 10.2, "high": 10.5, "low": 10.0,
             "close": 10.3, "volume": 1100},
            {"date": self.target, "open": 10.1, "high": 10.2, "low": 9.0,
             "close": 9.2, "volume": 900},
        ])

    def _detect(self, mode, df=None):
        self.ir.GM_INDEX_CACHE["SHSE.000688"] = (self.df if df is None else df).copy()
        return self.ir._IndexRegimeEngine().detect(
            as_of=self.target, force=True, mode=mode, index_symbol="sh000688")

    def test_morning_removes_target_forming_bar_and_versions_actual_input(self):
        with mock.patch.object(self.ir, "_ir_now", side_effect=AssertionError("不得取当前时间")):
            _, _, first = self._detect("morning")
            _, _, second = self._detect("morning")
        self.assertEqual(first["requested_date"], self.target)
        self.assertEqual(first["input_last_date"], self.target)
        self.assertEqual(first["effective_date"], "2026-11-04")
        self.assertEqual(first["date"], "2026-11-04")
        self.assertTrue(first["forming_bar_removed"])
        self.assertEqual(first["input_rows"], 3)
        self.assertEqual(first["effective_rows"], 2)
        self.assertEqual(first["mode"], "morning")
        self.assertEqual(first["degraded"], [])
        self.assertTrue(first["input_version"].startswith("sha256:"))
        self.assertEqual(first["input_version"], second["input_version"])

    def test_live_compatibility_and_eod_tail_retention(self):
        _, _, live = self._detect("live")
        self.assertTrue(live["forming_bar_removed"])
        self.assertEqual(live["effective_date"], "2026-11-04")
        for mode in ("eod", "tail"):
            with self.subTest(mode=mode):
                _, _, ctx = self._detect(mode)
                self.assertFalse(ctx["forming_bar_removed"])
                self.assertEqual(ctx["input_last_date"], self.target)
                self.assertEqual(ctx["effective_date"], self.target)
                self.assertEqual(ctx["effective_rows"], 3)
                self.assertEqual(ctx["mode"], mode)

    def test_empty_after_morning_removal_is_degraded_with_same_audit_schema(self):
        _, _, ctx = self._detect("morning", self.df.iloc[-1:].copy())
        self.assertIn("剔除未成形K线后无数据", ctx["degraded"])
        self.assertTrue(ctx["forming_bar_removed"])
        self.assertEqual(ctx["requested_date"], self.target)
        self.assertEqual(ctx["input_last_date"], self.target)
        self.assertIsNone(ctx["effective_date"])
        self.assertEqual(ctx["effective_rows"], 0)
        self.assertTrue(ctx["input_version"].startswith("sha256:"))

    def test_internal_exception_has_consistent_honest_audit_fields(self):
        engine = self.ir._IndexRegimeEngine()
        with mock.patch.object(engine, "_detect_inner", side_effect=RuntimeError("frozen error")):
            _, _, ctx = engine.detect(as_of=self.target, force=True, mode="morning",
                                      index_symbol="sh000688")
        self.assertEqual(ctx["degraded"], ["internal_error"])
        self.assertEqual(ctx["requested_date"], self.target)
        self.assertIsNone(ctx["input_last_date"])
        self.assertIsNone(ctx["effective_date"])
        self.assertFalse(ctx["forming_bar_removed"])
        self.assertIsNone(ctx["input_version"], "异常时不得用当前时间伪造输入版本")
        self.assertEqual(ctx["mode"], "morning")
        self.assertEqual(ctx["index_symbol"], "sh000688")


_REAL_STAR50_CACHE = _ROOT / "t_io" / "cache" / "daily_kline" / "index_sh000688.json"


def build_real_cache_forming_replay():
    """只读本地科创50日线，输出 11-05/11-06 forming 前后可复核证据。"""
    rows = json.loads(_REAL_STAR50_CACHE.read_text(encoding="utf-8")).get("rows") or []
    df = pd.DataFrame(rows)
    ir = _load_gm_index_regime()
    old_ready = ir.GM_DATA_READY
    old_cache = dict(ir.GM_INDEX_CACHE)
    payload = {"index_symbol": "SHSE.000688",
               "source": "t_io/cache/daily_kline/index_sh000688.json",
               "source_rows": len(df), "cases": {}}
    try:
        ir.GM_DATA_READY = True
        ir.GM_INDEX_CACHE.clear()
        ir.GM_INDEX_CACHE["SHSE.000688"] = df
        ir._IR_MEM_CACHE.clear()
        for day in ("2026-11-05", "2026-11-06"):
            payload["cases"][day] = {}
            for mode in ("morning", "eod"):
                regime, score, ctx = ir.detect_index_regime(
                    as_of=day, force=True, mode=mode, index_symbol="SHSE.000688")
                payload["cases"][day][mode] = {
                    "regime": regime.value if hasattr(regime, "value") else str(regime),
                    "score": score, "score_rounded": ctx.get("score"),
                    "input_version": ctx.get("input_version"),
                    "input_last_date": ctx.get("input_last_date"),
                    "effective_date": ctx.get("effective_date"),
                    "forming_bar_removed": ctx.get("forming_bar_removed"),
                    "input_rows": ctx.get("input_rows"),
                    "effective_rows": ctx.get("effective_rows"),
                    "degraded": ctx.get("degraded"),
                }
    finally:
        ir.GM_DATA_READY = old_ready
        ir.GM_INDEX_CACHE.clear()
        ir.GM_INDEX_CACHE.update(old_cache)
    return payload


@unittest.skipUnless(_REAL_STAR50_CACHE.exists(), "本地科创50日线缓存不存在")
class TestRealCacheFormingReplay(unittest.TestCase):
    def test_1105_1106_regime_score_version_effective_date_and_temp_artifact(self):
        payload = build_real_cache_forming_replay()
        expected = {
            ("2026-11-05", "morning"): ("uni_up", -7.643999999999998, "2026-11-04",
                                        "e913b1b6050cbd2a5b0cc961c39dbe7c2a654d392af6f8d87096bb759949899a"),
            ("2026-11-05", "eod"): ("uni_up", 25.515272727272727, "2026-11-05",
                                    "e913b1b6050cbd2a5b0cc961c39dbe7c2a654d392af6f8d87096bb759949899a"),
            ("2026-11-06", "morning"): ("uni_up", 25.515272727272727, "2026-11-05",
                                        "9d58ad18a1ec2d1d9ff946b7a110fdaf2cefaacd523496e796a7b37fc3a5394a"),
            ("2026-11-06", "eod"): ("uni_down", -7.643999999999998, "2026-11-06",
                                    "9d58ad18a1ec2d1d9ff946b7a110fdaf2cefaacd523496e796a7b37fc3a5394a"),
        }
        for (day, mode), (regime, score, effective, version) in expected.items():
            with self.subTest(day=day, mode=mode):
                rec = payload["cases"][day][mode]
                self.assertEqual(rec["regime"], regime)
                self.assertAlmostEqual(rec["score"], score, places=10)
                self.assertEqual(rec["effective_date"], effective)
                self.assertEqual(rec["input_version"], "sha256:" + version)
                self.assertEqual(rec["input_last_date"], day)
                self.assertEqual(rec["degraded"], [])
                self.assertEqual(rec["forming_bar_removed"], mode == "morning")
        for day in ("2026-11-05", "2026-11-06"):
            self.assertEqual(payload["cases"][day]["morning"]["input_version"],
                             payload["cases"][day]["eod"]["input_version"],
                             "同一实际输入必须先计算版本，再剔除 forming bar")
        self.assertEqual(payload["cases"]["2026-11-05"]["eod"]["score"],
                         payload["cases"]["2026-11-06"]["morning"]["score"])
        self.assertNotEqual(payload["cases"]["2026-11-05"]["eod"]["input_version"],
                            payload["cases"]["2026-11-06"]["morning"]["input_version"])
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td, "w45_regime_forming_replay.json")
            artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            restored = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(restored, payload)


class _Gateway:
    def __init__(self):
        self.cash = 1_000_000.0
        self.placed = []
        self.cancelled = []
        self.account_positions = {}

    def get_cash(self):
        return self.cash

    def get_positions(self):
        return self.account_positions

    def place_order(self, code, side, qty, price, execution_limit_price=None):
        limit = execution_limit_price if execution_limit_price is not None else price
        self.placed.append((code, side, qty, limit))
        return {"order_id": "o_%d" % len(self.placed), "status": "submitted",
                "filled_qty": 0, "filled_price": None, "message": ""}

    def poll_order(self, order_id, code):
        return {"order_id": order_id, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": ""}

    def cancel_order(self, order_id, code):
        self.cancelled.append((order_id, code))
        return {"ok": True, "order_id": order_id, "message": ""}


class _RT:
    def __init__(self, book_dir, now):
        self.book_dir = book_dir
        self.now = now
        self.gw = _Gateway()
        self.gm = self
        self.ADJUST_PREV = 1
        self.audits = []
        self.logs = []
        self.risks = []
        self.sdk_calls = []
        self.params = {"osc_top_n": 1, "osc_single_budget": 100000.0,
                       "osc_daily_budget": 200000.0, "osc_cash_headroom": 0.95,
                       "osc_order_poll_rounds": 0, "osc_order_poll_sleep_sec": 0.0,
                       "osc_backfill_enabled": True, "osc_hold_days": 5,
                       "osc_stop_pct": 0.05, "osc_stop_deep_dev20": -0.07,
                       "osc_pop_target_pct": 0.03}

    def param(self, key, default=None):
        return self.params.get(key, default)

    def kill_switch(self):
        return False

    def gateway(self, context):
        return self.gw

    def code_to_gm(self, code):
        return ("SHSE." if str(code)[:1] in "56" else "SZSE.") + str(code)

    def sdk_call(self, desc, fn, *args, **kwargs):
        self.sdk_calls.append(desc)
        return fn(*args, **kwargs)

    def history(self, symbol=None, **kwargs):
        symbols = symbol if isinstance(symbol, (list, tuple)) else [symbol]
        return pd.DataFrame([
            {"symbol": s, "eob": self.now, "open": 11.0, "high": 11.8,
             "low": 10.0, "close": 11.5, "amount": 11.4 * 1000, "volume": 1000}
            for s in symbols])

    def current(self, symbols=None):
        return [{"symbol": s, "price": 11.5, "cum_amount": 11.4 * 1000,
                 "cum_volume": 1000, "quotes": [{"ask_v": 500}]} for s in symbols]

    def audit(self, rec):
        self.audits.append(rec)

    def log(self, *args, **kwargs):
        self.logs.append(" ".join(str(a) for a in args))

    def risk(self, *args, **kwargs):
        self.risks.append((args, kwargs))


_NORMAL = ("600000", "000001", "600001")
_ALL_SYMBOLS = ("SHSE.000001", "SZSE.399001", "SZSE.399006", "SHSE.000688")


def _context(day, regimes=None, contexts=None, last="range"):
    board = {s: "range" for s in _ALL_SYMBOLS}
    board.update(regimes or {})
    valid_contexts = valid_board_regime_ctx(day, board)
    valid_contexts.update(contexts or {})
    market_ctx = valid_regime_ctx(day, "SHSE.000001", last)
    return type("_Ctx", (), {
        "last_index_regime": last, "board_regime": board,
        "board_regime_ctx": valid_contexts, "last_index_regime_ctx": market_ctx,
    })()


def _candidate_map(codes):
    return {c: {"prev_close": 10.0, "ma20": 12.0, "board": "测试", "name": c}
            for c in codes}


def _first_buy_result(day, spares):
    return {"date": day, "picks": [{"code": "600001"}],
            "buys": [{"code": "600001", "order_id": "old", "status": "submitted",
                      "gap": 0.05, "price": 10.0, "qty_placed": 100, "filled_qty": 0}],
            "spares": list(spares), "summary": {}}


class TestFrozenBoardBuyGate(unittest.TestCase):
    def _first_buy(self, day, affected, regimes, contexts=None, last="range"):
        hooks.reset_day(day)
        codes = list(affected) + list(_NORMAL)
        original = _candidate_map(codes)
        hooks._STATE["candidates"] = dict(original)
        now = dt.datetime.fromisoformat(day + "T10:00:00")
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            n = hooks.try_buy(rt, _context(day, regimes, contexts, last), now)
            fills = json.loads(Path(td, "fills_%s.json" % day).read_text(encoding="utf-8"))
            return n, rt, fills, original

    def test_1103_and_1105_first_buy_block_only_frozen_uni_down_board(self):
        cases = (("2026-11-03", ("300085",), {"SZSE.399006": "uni_down"}),
                 ("2026-11-05", ("688652", "688185"), {"SHSE.000688": "uni_down"}))
        for day, affected, regimes in cases:
            with self.subTest(day=day):
                n, rt, fills, original = self._first_buy(day, affected, regimes)
                placed_codes = [c for c, side, qty, px in rt.gw.placed if side == "BUY"]
                self.assertEqual(n, 1, "过滤后仍有三只正常票，不应被 MIN_POOL 误伤")
                self.assertEqual(len(placed_codes), 1)
                self.assertTrue(set(placed_codes).isdisjoint(affected))
                trail_codes = {p["code"] for p in fills["picks"] + fills["spares"]}
                self.assertTrue(trail_codes.isdisjoint(affected), "被过滤票不得进入 picks/spares")
                self.assertTrue(set(affected) <= set(hooks._STATE["candidates"]),
                                "局部执行过滤不得改写预筛 candidates")
                self.assertEqual(set(hooks._STATE["candidates"]), set(original))
                events = [a for a in rt.audits if a.get("event") == "osc_board_regime_filter"]
                self.assertTrue(events, "有冻结 uni_down 拦截就必须存在 regime 审计，禁止空集合恒真")
                blocked = [b for a in events for b in a.get("blocked", [])]
                self.assertEqual({b["code"] for b in blocked}, set(affected))
                for event in events:
                    regime_context = event.get("regime_context") or {}
                    self.assertEqual(set(regime_context.get("boards") or {}), set(_ALL_SYMBOLS))
                    self.assertEqual(regime_context.get("market", {}).get("requested_date"), day)
                    for item in event.get("blocked", []):
                        symbol = item["index_symbol"]
                        self.assertEqual(regime_context["board_regime"][symbol], "uni_down")
                        board_ctx = regime_context["boards"][symbol]
                        self.assertEqual(board_ctx["requested_date"], day)
                        self.assertEqual(board_ctx["index_symbol"], symbol)
                        self.assertEqual(board_ctx["regime"], "uni_down")
                        self.assertTrue(board_ctx["input_version"].startswith("sha256:"))
                        self.assertEqual(board_ctx["effective_date"], board_ctx["expected_effective_date"])

    def test_range_and_uni_up_controls_do_not_block(self):
        for regime in ("range", "uni_up"):
            with self.subTest(regime=regime):
                n, rt, fills, _ = self._first_buy(
                    "2026-11-05", ("688652", "688185"), {"SHSE.000688": regime})
                self.assertEqual(n, 1)
                self.assertTrue(rt.gw.placed)
                self.assertFalse([a for a in rt.audits if a.get("event") == "osc_board_regime_filter"])

    def test_missing_or_degraded_board_regime_fails_closed_before_market(self):
        for label, regimes, contexts in (
                ("missing", {"SZSE.399006": None}, {}),
                ("degraded", {}, {"SZSE.399006": {"degraded": ["internal_error"]}})):
            with self.subTest(label=label):
                hooks.reset_day("2026-11-03")
                hooks._STATE["candidates"] = _candidate_map(["300085"])
                now = dt.datetime(2026, 11, 3, 10, 0)
                with tempfile.TemporaryDirectory() as td:
                    rt = _RT(td, now)
                    n = hooks.try_buy(rt, _context("2026-11-03", regimes, contexts), now)
                    fills = json.loads(Path(td, "fills_2026-11-03.json").read_text(encoding="utf-8"))
                self.assertEqual(n, 0)
                self.assertEqual(rt.gw.placed, [])
                self.assertNotIn("osc_10am_bars", rt.sdk_calls)
                self.assertEqual(fills["summary"]["skip_reason"], "board_regime_unavailable")
                self.assertTrue(any(a.get("reason") == "board_regime_unavailable"
                                    for a in rt.audits))

    def test_invalid_same_round_context_fails_closed_but_normal_candidates_survive(self):
        day = "2026-11-05"
        valid = valid_regime_ctx(day, "SHSE.000688", "range")
        cases = []
        for label, patch in (
                ("context_missing", None),
                ("wrong_round", {"requested_date": "2026-11-04"}),
                ("index_symbol_mismatch", {"index_symbol": "SHSE.000001"}),
                ("regime_mismatch", {"regime": "uni_up"}),
                ("stale_input", {"effective_date": "2026-11-03", "input_last_date": "2026-11-03"}),
                ("input_version_missing", {"input_version": None}),
                ("forming_bar_not_removed", {"forming_bar_removed": False})):
            ctx = {} if patch is None else dict(valid, **patch)
            cases.append((label, ctx))
        for label, board_ctx in cases:
            with self.subTest(label=label):
                n, rt, fills, _ = self._first_buy(
                    day, ("688652",), {}, {"SHSE.000688": board_ctx})
                placed = [c for c, side, qty, px in rt.gw.placed if side == "BUY"]
                self.assertEqual(n, 1)
                self.assertTrue(placed and "688652" not in placed)
                events = [a for a in rt.audits if a.get("event") == "osc_board_regime_filter"]
                self.assertTrue(events)
                errors = [e for a in events for u in a.get("unavailable", [])
                          if u["code"] == "688652" for e in u.get("context_errors", [])]
                self.assertIn(label, errors)

    def test_1106_all_board_uni_down_zero_buy_and_zero_stock_market_call(self):
        regimes = {s: "uni_down" for s in _ALL_SYMBOLS}
        n, rt, fills, _ = self._first_buy(
            "2026-11-06", ("300085", "688652"), regimes, last="uni_down")
        self.assertEqual(n, 0)
        self.assertEqual(rt.gw.placed, [])
        self.assertEqual(rt.sdk_calls, [])
        self.assertEqual(fills["summary"]["skip_reason"], "index_regime_uni_down")

    def _run_backfill(self, day, affected, regimes, round_idx):
        hooks.reset_day(day)
        now = dt.datetime.fromisoformat(day + ("T10:08:00" if round_idx == 0 else "T10:14:00"))
        codes = list(affected) + list(_NORMAL)
        hooks._STATE.update({
            "buy_done": True, "candidates": _candidate_map(codes),
            "pending": [{"code": "600001", "order_id": "old", "gap": 0.05,
                         "price": 10.0, "qty_placed": 100, "filled_qty": 0,
                         "qty_remaining": 100, "ref_price": 10.0}],
            "fills": {"date": day, "buys": []},
            "spares": [{"code": c, "gap": 0.2 - i * 0.01, "board": "测试", "price": 11.5}
                       for i, c in enumerate(affected)],
            "done_codes": {"600001"}, "backfill": {}})
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            captured = []

            def _fake_backfill(gateway, date, fills, pending, spares, pc, quotes, **kwargs):
                captured.append(list(spares))
                return {"cancel": {}, "placed": [], "skipped": [], "note": "frozen", "cash_budget": 0}

            with mock.patch.object(hooks.fillbackfill, "reconcile_day_state", return_value=None), \
                 mock.patch.object(hooks.backfill, "run_backfill_exec", side_effect=_fake_backfill), \
                 mock.patch.object(hooks.executor, "merge_backfill_fills", return_value=None):
                n = hooks.try_backfill(rt, _context(day, regimes), now, day, round_idx)
        self.assertEqual(n, 0)
        self.assertEqual(rt.gw.placed, [])
        self.assertEqual(captured, [[]], "uni_down 候补不得进入复检/顺移执行核")
        self.assertTrue(any(a.get("event") == "osc_board_regime_filter"
                            and a.get("stage") == "backfill" and a.get("round") == round_idx
                            for a in rt.audits))

    def test_1103_and_1105_both_backfill_rounds_zero_new_buy(self):
        cases = (("2026-11-03", ("300085",), {"SZSE.399006": "uni_down"}),
                 ("2026-11-05", ("688652", "688185"), {"SHSE.000688": "uni_down"}))
        for day, affected, regimes in cases:
            for round_idx in (0, 1):
                with self.subTest(day=day, round=round_idx + 1):
                    self._run_backfill(day, affected, regimes, round_idx)

    def test_normal_spare_coexists_and_blocked_spare_does_not_revive_after_restart(self):
        day = "2026-11-05"
        now = dt.datetime(2026, 11, 5, 10, 8)
        hooks.reset_day(day)
        hooks._STATE.update({
            "buy_done": True, "candidates": _candidate_map(["600001", "600000", "688652"]),
            "pending": [{"code": "600001", "order_id": "old", "gap": 0.05,
                         "price": 10.0, "qty_placed": 100, "filled_qty": 0,
                         "qty_remaining": 100, "ref_price": 10.0}],
            "fills": {"date": day, "buys": []},
            "spares": [{"code": "688652", "gap": 0.2, "board": "测试", "price": 11.5},
                       {"code": "600000", "gap": 0.19, "board": "测试", "price": 11.5}],
            "done_codes": {"600001"}, "backfill": {}})
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            fetched = []
            with mock.patch.object(hooks.fillbackfill, "reconcile_day_state", return_value=None), \
                 mock.patch.object(hooks, "fetch_current_snapshot",
                                   side_effect=lambda rt, codes, now: fetched.extend(codes) or {}):
                self.assertEqual(hooks.try_backfill(
                    rt, _context(day, {"SHSE.000688": "uni_down"}), now, day, 0), 0)
            self.assertIn("600000", fetched, "正常候补必须与 blocked 候补共存并进入后续复检")
            self.assertNotIn("688652", fetched)
            state = json.loads(Path(td, "day_state_%s.json" % day).read_text(encoding="utf-8"))
            self.assertEqual([s["code"] for s in state["spares"]], ["600000"])
            self.assertTrue(state["board_gate"]["filter_complete"])
            self.assertEqual(state["board_gate_blocked_codes"], ["688652"])

            hooks.reset_day(day)  # 模拟进程重启；即使之后恢复 range，旧 blocked 候补也不得复活。
            self.assertTrue(hooks.load_day_state(rt, day))
            self.assertEqual([s["code"] for s in hooks._STATE["spares"]], ["600000"])
            self.assertNotIn("688652", hooks._STATE["spares"])

    def test_initial_gate_restart_range_replays_filter_and_enters_exec(self):
        day = "2026-11-05"
        buy_now = dt.datetime(2026, 11, 5, 10, 0)
        now = dt.datetime(2026, 11, 5, 10, 8)
        hooks.reset_day(day)
        context = _context(day)  # 全部合法 range，无 blocked。
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            hooks._stamp_after_buy(rt, context, buy_now, _first_buy_result(day, [
                {"code": "600000", "gap": 0.19, "board": "测试", "price": 11.5}]), {})
            state = json.loads(Path(td, "day_state_%s.json" % day).read_text(encoding="utf-8"))
            self.assertEqual(state["board_gate"]["version"], 2)
            self.assertEqual(state["board_gate"]["stage"], "initial")
            self.assertFalse(state["board_gate"]["filter_complete"])
            self.assertEqual(state["board_gate"]["input_proof"],
                             hooks._regime_input_proof(context, buy_now))
            self.assertTrue(state["board_gate"]["input_proof"]["boards"]
                            ["SHSE.000001"]["input_version"].startswith("sha256:"))

            hooks.reset_day(day)  # 首轮真实落盘后、第一次补量前重启。
            exec_result = {"cancel": {}, "placed": [], "skipped": [],
                           "note": "normal", "cash_budget": 0}
            with mock.patch.object(hooks.fillbackfill, "reconcile_day_state", return_value=None), \
                 mock.patch.object(hooks, "fetch_current_snapshot",
                                   return_value={"600000": {"price": 11.5}}) as fetch, \
                 mock.patch.object(hooks.backfill, "run_backfill_exec",
                                   return_value=exec_result) as exec_core, \
                 mock.patch.object(hooks.executor, "merge_backfill_fills", return_value=None):
                self.assertEqual(hooks.try_backfill(rt, context, now, day, 0), 0)
            fetch.assert_called_once()
            exec_core.assert_called_once()
            self.assertEqual([s["code"] for s in exec_core.call_args.args[4]], ["600000"])

    def test_backfill_filter_persist_failure_survives_restart_and_range(self):
        day = "2026-11-05"
        buy_now = dt.datetime(2026, 11, 5, 10, 0)
        now = dt.datetime(2026, 11, 5, 10, 8)
        hooks.reset_day(day)
        down_context = _context(day, {"SHSE.000688": "uni_down"})
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            hooks._stamp_after_buy(rt, down_context, buy_now, _first_buy_result(day, [
                {"code": "688652", "gap": 0.2, "board": "测试", "price": 11.5}]), {})
            real_save = hooks.save_day_state
            save_calls = []

            def _fail_only_filtered_save(rt_arg, date_arg):
                save_calls.append(date_arg)
                if len(save_calls) == 1:
                    return real_save(rt_arg, date_arg)  # filtering 开始证据真实落盘
                return False                              # filtered/tombstone 保存失败

            with mock.patch.object(hooks.fillbackfill, "reconcile_day_state", return_value=None), \
                 mock.patch.object(hooks, "save_day_state", side_effect=_fail_only_filtered_save), \
                 mock.patch.object(hooks, "fetch_current_snapshot") as fetch, \
                 mock.patch.object(hooks.backfill, "run_backfill_exec") as exec_core:
                self.assertEqual(hooks.try_backfill(rt, down_context, now, day, 0), 0)
            self.assertEqual(len(save_calls), 2)
            fetch.assert_not_called()
            exec_core.assert_not_called()
            self.assertEqual(rt.gw.cancelled, [])
            self.assertEqual(rt.gw.placed, [])
            self.assertIn("688652", hooks._STATE["board_gate_blocked_codes"])
            self.assertTrue(hooks._STATE["board_gate_persist_failed"])
            state = json.loads(Path(td, "day_state_%s.json" % day).read_text(encoding="utf-8"))
            self.assertEqual(state["board_gate"]["stage"], "filtering")
            self.assertFalse(state["board_gate"]["filter_complete"])

            hooks.reset_day(day)  # 磁盘是 filtering 未完成；后续 range 不得借首轮证明洗白。
            with mock.patch.object(hooks.fillbackfill, "reconcile_day_state", return_value=None), \
                 mock.patch.object(hooks, "fetch_current_snapshot") as fetch_after_restart, \
                 mock.patch.object(hooks.backfill, "run_backfill_exec") as exec_after_restart:
                self.assertEqual(hooks.try_backfill(
                    rt, _context(day, {"SHSE.000688": "range"}), now, day, 0), 0)
                self.assertEqual(hooks.try_backfill(
                    rt, _context(day, {"SHSE.000688": "range"}), now, day, 0), 0)
            fetch_after_restart.assert_not_called()
            exec_after_restart.assert_not_called()
            self.assertEqual(rt.gw.cancelled, [])
            self.assertEqual(rt.gw.placed, [])
            reasons = [a.get("reason") for a in rt.audits
                       if a.get("event") == "osc_backfill_skip"]
            self.assertIn("board_gate_persist_failed", reasons)
            self.assertIn("board_gate_unproved_after_reload", reasons)
            risk_reasons = [args[1] for args, _ in rt.risks if len(args) > 1]
            self.assertIn("board_gate_persist_failed", risk_reasons)
            self.assertIn("board_gate_unproved_after_reload", risk_reasons)

    def test_initial_gate_input_proof_mismatch_fails_closed(self):
        day = "2026-11-05"
        buy_now = dt.datetime(2026, 11, 5, 10, 0)
        now = dt.datetime(2026, 11, 5, 10, 8)
        hooks.reset_day(day)
        context = _context(day)
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            hooks._stamp_after_buy(rt, context, buy_now, _first_buy_result(day, [
                {"code": "600000", "gap": 0.19, "board": "测试", "price": 11.5}]), {})
            hooks.reset_day(day)
            changed = _context(day)
            changed.board_regime_ctx["SHSE.000001"]["input_version"] = "sha256:changed"
            with mock.patch.object(hooks, "fetch_current_snapshot") as fetch, \
                 mock.patch.object(hooks.backfill, "run_backfill_exec") as exec_core:
                self.assertEqual(hooks.try_backfill(rt, changed, now, day, 0), 0)
            fetch.assert_not_called()
            exec_core.assert_not_called()
            self.assertTrue(any(a.get("reason") == "board_gate_input_proof_mismatch"
                                for a in rt.audits))

    def test_legacy_empty_gate_remains_fail_closed(self):
        day = "2026-11-05"
        now = dt.datetime(2026, 11, 5, 10, 8)
        hooks.reset_day(day)
        hooks._STATE.update({
            "buy_done": True,
            "pending": [{"code": "600001", "order_id": "old", "gap": 0.05,
                         "price": 10.0, "qty_placed": 100, "filled_qty": 0,
                         "qty_remaining": 100, "ref_price": 10.0}],
            "spares": [{"code": "600000", "gap": 0.19, "board": "测试", "price": 11.5}]})
        with tempfile.TemporaryDirectory() as td:
            rt = _RT(td, now)
            self.assertTrue(hooks.save_day_state(rt, day))  # 旧版 board_gate={}
            hooks.reset_day(day)
            with mock.patch.object(hooks, "fetch_current_snapshot") as fetch, \
                 mock.patch.object(hooks.backfill, "run_backfill_exec") as exec_core:
                self.assertEqual(hooks.try_backfill(rt, _context(day), now, day, 0), 0)
            fetch.assert_not_called()
            exec_core.assert_not_called()
            self.assertTrue(any(a.get("reason") == "board_gate_unproved_after_reload"
                                for a in rt.audits))

    def test_uni_down_does_not_block_morning_pop_or_close_sell(self):
        day = "2026-11-06"
        now = dt.datetime(2026, 11, 6, 14, 50)
        with tempfile.TemporaryDirectory() as td:
            Path(td, "positions.json").write_text(json.dumps({
                "updated_at": "", "open": {"600000": {
                    "qty": 100, "entry_date": "2026-11-05", "entry_price": 10.0,
                    "dev20": -0.2, "days_held": 1}}}), encoding="utf-8")
            rt = _RT(td, now)
            rt.gw.account_positions = {"600000": {"qty": 100, "available": 100, "cost": 10.0}}
            ctx = _context(day, {s: "uni_down" for s in _ALL_SYMBOLS}, last="uni_down")
            sell_result = {"summary": {"sells_placed": 1}}
            with mock.patch.object(hooks.executor, "run_sell_exec", return_value=sell_result) as close_sell, \
                 mock.patch.object(hooks.executor, "run_morning_sell_exec", return_value=sell_result) as pop_sell:
                self.assertEqual(hooks.try_sell(rt, ctx, now, day), 1)
                self.assertEqual(hooks.try_morning_sell(rt, ctx, now, day), 1)
            close_sell.assert_called_once()
            pop_sell.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
