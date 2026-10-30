# -*- coding: utf-8 -*-
"""候选⑲ P0：委托状态、真实 ID、fills/day_state 与补单竞态的一致性行为测试。"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
AUTO = ROOT / "execution" / "auto"
for path in (str(ROOT), str(AUTO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from framework.gateway import GmGateway  # noqa: E402
from framework.services import GmServices  # noqa: E402
from oversold_gap_confirm import backfill, executor, fillbackfill, hooks  # noqa: E402

DATE = "2026-10-30"


def _write_json(directory, name, payload):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _read_json(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as f:
        return json.load(f)


def _buy(code, order_id="o1", status="submitted", qty=100, filled=0, price=10.0,
         filled_price=None):
    return {"code": code, "symbol": "SHSE." + code, "side": "BUY",
            "gap": 0.05, "price": price, "qty_target": qty, "qty_placed": qty,
            "status": status, "filled_qty": filled, "filled_price": filled_price,
            "order_id": order_id, "qty_remaining": qty - filled, "message": ""}


class _Ownership:
    def can_open(self, code, strategy_id, held_qty=0):
        return True, ""


class _Account:
    def cash(self):
        return {"available": 1_000_000.0}

    def positions(self, *args, **kwargs):
        return []


class _Context:
    def __init__(self):
        self.now = dt.datetime(2026, 10, 30, 10, 0)
        self.account = lambda: _Account()


class _FallbackGm:
    OrderSide_Buy, OrderSide_Sell = 1, 2
    OrderType_Market, OrderType_Limit = 1, 2
    PositionEffect_Open, PositionEffect_Close = 1, 2
    ADJUST_PREV = 1

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_orders(self, symbol=None):
        self.calls.append(symbol)
        if symbol is not None:
            raise TypeError("legacy get_orders takes no symbol")
        return list(self.rows)


def _services(gm, sequence=None):
    sequence = sequence if sequence is not None else []
    return GmServices(
        gm=gm, sdk_call=lambda desc, fn, *a, **k: fn(*a, **k),
        write_order=lambda *a, **k: sequence.append(("order", a, k)),
        write_order_ack=lambda *a, **k: sequence.append(("ack", a, k)),
        mark_pending_recon=lambda *a: sequence.append(("recon", a, {})),
        code_to_gm=lambda c: ("SHSE." if c[:1] in "569" else "SZSE.") + c,
        raw_code=lambda s: str(s).split(".")[-1],
        param=lambda k, d=None: d, clock=lambda ctx: ctx.now,
        reject_statuses=(4, 5, 6, 8, 12), ownership=_Ownership(),
    )


class TestGatewaySymbolAndAck(unittest.TestCase):
    def test_typeerror_fallback_filters_all_account_rows_and_status6_stays_open(self):
        rows = [
            {"id": "target", "symbol": "SHSE.600001", "status": 6, "side": 1},
            {"id": "other1", "symbol": "SHSE.600002", "status": 0, "side": 1},
            {"id": "other2", "symbol": "SZSE.000001", "status": 3, "side": 1},
            {"id": "other3", "symbol": "SZSE.300001", "status": 8, "side": 1},
        ]
        gm = _FallbackGm(rows)
        gw = GmGateway(_Context(), "osc", _services(gm), log=lambda *a: None)
        rep = gw.poll_order("target", "600001")
        self.assertEqual(rep["status"], "pending_cancel")
        self.assertEqual(rep["raw_status"], 6)
        self.assertEqual(gm.calls, ["SHSE.600001", None],
                         "必须先带 symbol 尝试，TypeError 后才全账户回退")

    def test_all_account_fallback_same_symbol_two_open_orders_is_ambiguous(self):
        rows = [
            {"id": "a", "symbol": "SHSE.600001", "status": 0, "side": 1},
            {"id": "b", "symbol": "SHSE.600001", "status": 6, "side": 1},
            {"id": "c", "symbol": "SHSE.600002", "status": 0, "side": 1},
        ]
        gw = GmGateway(_Context(), "osc", _services(_FallbackGm(rows)), log=lambda *a: None)
        self.assertEqual(gw._resolve_open_buy_order_id("600001"),
                         (None, "ambiguous_open_orders"))

    def test_all_account_row_without_symbol_is_not_guessed(self):
        rows = [{"id": "target", "status": 3, "filled_volume": 100}]
        gw = GmGateway(_Context(), "osc", _services(_FallbackGm(rows)), log=lambda *a: None)
        rep = gw.poll_order("target", "600001")
        self.assertEqual(rep["status"], "submitted")
        self.assertEqual(rep["message"], "order_not_found")

    def test_order_intent_then_broker_then_real_order_id_ack(self):
        sequence = []

        class _Gm(_FallbackGm):
            def order_volume(self, **kwargs):
                sequence.append(("broker", kwargs))
                return [{"order_id": "real-only", "status": 0}]

        gm = _Gm([])
        gw = GmGateway(_Context(), "osc", _services(gm, sequence), log=lambda *a: None)
        rep = gw.place_order("600001", "BUY", 100, 10.0)
        self.assertEqual(rep["order_id"], "real-only")
        kinds = [x[0] for x in sequence]
        self.assertEqual(kinds, ["order", "broker", "recon", "ack"])
        ack = sequence[-1]
        self.assertEqual(ack[2]["order_id"], "real-only")

    def test_sync_status6_is_pending_cancel_not_rejected(self):
        class _Gm(_FallbackGm):
            def order_volume(self, **kwargs):
                return [{"order_id": "pc-sync", "status": 6}]

        gw = GmGateway(_Context(), "osc", _services(_Gm([])), log=lambda *a: None)
        rep = gw.place_order("600001", "BUY", 100, 10.0)
        self.assertEqual(rep["status"], "pending_cancel")
        self.assertEqual(rep["raw_status"], 6)


class _FakeGateway:
    def __init__(self, mode="released", empty_new_id=False):
        self.mode = mode
        self.empty_new_id = empty_new_id
        self.cancelled = []
        self.placed = []

    def get_cash(self):
        return 1_000_000.0

    def get_positions(self):
        return {}

    def cancel_order(self, order_id, code):
        self.cancelled.append((order_id, code))
        if self.mode == "no_open":
            return {"ok": False, "order_id": order_id, "message": "no_open_order",
                    "not_found": True}
        return {"ok": True, "order_id": order_id or "resolved-1", "message": ""}

    def poll_order(self, order_id, code):
        if self.mode == "race":
            return {"order_id": order_id, "status": "filled", "raw_status": 3,
                    "filled_qty": 100, "filled_price": 10.5}
        return {"order_id": order_id, "status": "cancelled", "raw_status": 5,
                "filled_qty": 0, "filled_price": None}

    def place_order(self, code, side, qty, price):
        self.placed.append((code, side, qty, price))
        return {"order_id": None if self.empty_new_id else "new-1",
                "status": "submitted", "filled_qty": 0, "filled_price": None,
                "message": ""}


class _FakeRT:
    def __init__(self, book_dir, gateway):
        self.book_dir = book_dir
        self.gw = gateway
        self.gm = self
        self.audits = []
        self.logs = []

    def param(self, key, default=None):
        return {"osc_backfill_enabled": True, "osc_single_budget": 100000.0,
                "osc_daily_budget": 400000.0, "osc_cash_headroom": 0.95,
                "osc_order_poll_rounds": 1, "osc_order_poll_sleep_sec": 0.0}.get(key, default)

    def kill_switch(self):
        return False

    def gateway(self, context):
        return self.gw

    def code_to_gm(self, code):
        return "SHSE." + code

    def sdk_call(self, desc, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def current(self, symbols=None):
        return [{"symbol": s, "price": 10.0, "cum_amount": 10000.0,
                 "cum_volume": 1000, "quotes": [{"ask_v": 500}]} for s in symbols]

    def log(self, *args):
        self.logs.append(" ".join(str(x) for x in args))

    def audit(self, rec):
        self.audits.append(rec)

    def risk(self, *args, **kwargs):
        pass


class TestHooksFillsTruthAndRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        hooks.reset_day(DATE)

    def tearDown(self):
        self.tmp.cleanup()
        hooks.reset_day(DATE)

    def _seed(self, rec, gateway, spare=False, stale_pending=None):
        fills = {"date": DATE, "mode": "live", "picks": [], "buys": [rec], "summary": {}}
        _write_json(self.dir, "fills_%s.json" % DATE, fills)
        pending = backfill.pending_from_fills(fills) if stale_pending is None else stale_pending
        hooks._STATE.update({
            "date": DATE, "buy_done": True, "fills": fills, "pending": pending,
            "spares": ([{"code": "600002", "gap": 0.04, "board": "半导体", "price": 10.0}]
                       if spare else []),
            "candidates": {"600001": {"prev_close": 9.0}, "600002": {"prev_close": 9.0}},
            "done_codes": {"600001"}, "backfill": {},
        })
        hooks.save_day_state(_FakeRT(self.dir, gateway), DATE)
        return _FakeRT(self.dir, gateway)

    def _run(self, rt):
        return hooks.try_backfill(rt, None, dt.datetime(2026, 10, 30, 10, 8), DATE, 0)

    def test_latest_filled_fills_clears_stale_pending_before_any_cancel(self):
        rec = _buy("600001", status="filled_by_event", filled=100, filled_price=10.5)
        stale = [{"code": "600001", "order_id": "o1", "gap": 0.05, "price": 10.0,
                  "qty_placed": 100, "filled_qty": 0, "qty_remaining": 100, "ref_price": 10.0}]
        gw = _FakeGateway()
        rt = self._seed(rec, gw, stale_pending=stale)
        self.assertEqual(self._run(rt), 0)
        self.assertEqual(gw.cancelled, [], "最新 fills 已全成，补单前必须清除且零撤单调用")
        self.assertEqual(hooks._STATE["pending"], [])
        self.assertEqual(_read_json(self.dir, "day_state_%s.json" % DATE)["pending"], [])

    def test_no_open_order_with_submitted_fills_stays_unconfirmed(self):
        gw = _FakeGateway("no_open")
        rt = self._seed(_buy("600001"), gw)
        self.assertEqual(self._run(rt), 0)
        self.assertEqual(len(gw.cancelled), 1)
        self.assertEqual([p["code"] for p in hooks._STATE["pending"]], ["600001"])
        rec = _read_json(self.dir, "fills_%s.json" % DATE)["buys"][0]
        self.assertEqual(rec["status"], "submitted",
                         "no_open_order 自身不是终态证据，不得伪造 cancelled")

    def test_released_writes_cancelled_and_restart_does_not_resurrect(self):
        gw = _FakeGateway("released")
        rt = self._seed(_buy("600001"), gw)
        self.assertEqual(self._run(rt), 0)
        rec = _read_json(self.dir, "fills_%s.json" % DATE)["buys"][0]
        self.assertEqual(rec["status"], "cancelled")
        self.assertTrue(rec.get("cancel_confirmed_at"))
        self.assertEqual(_read_json(self.dir, "day_state_%s.json" % DATE)["pending"], [])
        hooks.reset_day(DATE)  # 清内存模拟进程重启，磁盘留痕不动
        self.assertEqual(self._run(rt), 0)
        self.assertEqual(len(gw.cancelled), 1, "重启后 cancelled 委托不得复活再撤")

    def test_new_shift_pending_does_not_require_order_id(self):
        gw = _FakeGateway("released", empty_new_id=True)
        rt = self._seed(_buy("600001"), gw, spare=True)
        self.assertEqual(self._run(rt), 1)
        self.assertEqual(len(gw.placed), 1)
        self.assertEqual(len(hooks._STATE["pending"]), 1)
        self.assertEqual(hooks._STATE["pending"][0]["code"], "600002")
        self.assertIsNone(hooks._STATE["pending"][0]["order_id"])

    def test_race_fill_updates_fills_book_and_restart_stays_closed(self):
        gw = _FakeGateway("race")
        rt = self._seed(_buy("600001"), gw)
        _write_json(self.dir, "positions.json", {"updated_at": "", "open": {
            "600001": {"entry_date": DATE, "entry_price": 10.0, "qty": 100,
                       "days_held": 0, "gap": 0.05, "board": "半导体"}}, "closed": {}})
        self.assertEqual(self._run(rt), 0)
        rec = _read_json(self.dir, "fills_%s.json" % DATE)["buys"][0]
        self.assertEqual(rec["status"], "filled_by_event")
        self.assertEqual(rec["filled_qty"], 100)
        self.assertEqual(rec["filled_price"], 10.5)
        self.assertEqual(_read_json(self.dir, "day_state_%s.json" % DATE)["pending"], [])
        self.assertEqual(_read_json(self.dir, "positions.json")["open"]["600001"]["entry_price"], 10.5)
        hooks.reset_day(DATE)
        self.assertEqual(self._run(rt), 0)
        self.assertEqual(len(gw.cancelled), 1, "竞态成交已了结，重启后不得再撤")

    def test_race_fill_without_price_never_fabricates_price(self):
        gw = _FakeGateway()
        self._seed(_buy("600001"), gw)
        _write_json(self.dir, "positions.json", {"updated_at": "", "open": {
            "600001": {"entry_date": DATE, "entry_price": 10.0, "qty": 100}}, "closed": {}})
        result = fillbackfill.apply_race_fill(self.dir, DATE, "600001", 100, price=None)
        self.assertEqual(result["action"], "buy_filled_without_price")
        rec = _read_json(self.dir, "fills_%s.json" % DATE)["buys"][0]
        self.assertIsNone(rec["filled_price"])
        self.assertEqual(rec["filled_qty"], 100)
        self.assertEqual(_read_json(self.dir, "positions.json")["open"]["600001"]["entry_price"], 10.0)


class TestTerminalIdentityAndStatusGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_order_id_selects_only_matching_record(self):
        fills = {"date": DATE, "buys": [_buy("600001", "A"), _buy("600001", "B")],
                 "summary": {}}
        _write_json(self.dir, "fills_%s.json" % DATE, fills)
        _write_json(self.dir, "positions.json", {"updated_at": "", "open": {}, "closed": {}})
        fillbackfill.apply_reject(self.dir, {
            "time": DATE + " 10:00:00", "code": "600001", "side": "BUY", "qty": 100,
            "status": 8, "order_id": "B", "rej_detail": ""}, log=lambda *a: None)
        recs = _read_json(self.dir, "fills_%s.json" % DATE)["buys"]
        self.assertEqual(recs[0]["status"], "submitted")
        self.assertEqual(recs[1]["status"], "rejected")

    def test_status6_apply_reject_is_hard_noop(self):
        fills = {"date": DATE, "buys": [_buy("600001")], "summary": {}}
        _write_json(self.dir, "fills_%s.json" % DATE, fills)
        before = json.dumps(fills, sort_keys=True)
        result = fillbackfill.apply_reject(self.dir, {
            "time": DATE + " 10:00:00", "code": "600001", "side": "BUY", "qty": 100,
            "status": 6, "order_id": "o1"}, log=lambda *a: None)
        self.assertEqual(result["reason"], "pending_cancel")
        after = json.dumps(_read_json(self.dir, "fills_%s.json" % DATE), sort_keys=True)
        self.assertEqual(after, before)


class TestGmMainFourStatusReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env["SMOKE_ROOT"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        driver = ROOT / "tests" / "phase3" / "gm_main_order_status_driver.py"
        proc = subprocess.run([sys.executable, str(driver)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=180)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if line is None:
            raise AssertionError("gm_main 回放驱动失败\nSTDOUT:\n%s\nSTDERR:\n%s" %
                                 (proc.stdout[-4000:], proc.stderr[-4000:]))
        cls.r = json.loads(line[7:])
        cls.by_code = {r["code"]: r for r in cls.r["fills"]["buys"]}

    def test_filled_pending_rejected_expired_have_precise_fills_and_pending(self):
        self.assertEqual(self.by_code["600001"]["status"], "filled_by_event")
        self.assertEqual(self.by_code["600001"]["order_id"], "fill-only")
        self.assertEqual(self.by_code["600001"]["filled_price"], 10.5)
        self.assertEqual(self.by_code["600002"]["status"], "submitted")
        self.assertEqual(self.by_code["600002"]["qty_remaining"], 100)
        self.assertEqual(self.by_code["600003"]["status"], "rejected")
        self.assertEqual(self.by_code["600004"]["status"], "expired")
        self.assertEqual([p["code"] for p in self.r["day_state"]["pending"]], ["600002"],
                         "只有 status=6 的委托保留 pending")
        self.assertEqual(self.r["rejected_order_count"], 1,
                         "status=6/12 不得增加 rejected_order_count")
        self.assertEqual(set(self.r["book"]["open"]), {"600001", "600002"})

    def test_bridge_fill_reject_and_terminal_keep_real_ids_and_do_not_mix_terms(self):
        events = self.r["events"]
        fills = [e for e in events if e.get("event") == "fill"]
        rejects = [e for e in events if e.get("event") == "reject"]
        terminals = [e for e in events if e.get("event") == "order_terminal"]
        self.assertEqual([(e["code"], e["order_id"]) for e in fills], [("600001", "fill-only")])
        self.assertEqual([(e["code"], e["order_id"]) for e in rejects], [("600003", "rej-only")])
        self.assertEqual([(e["code"], e["terminal"], e["order_id"]) for e in terminals],
                         [("600004", "expired", "exp-1")])
        self.assertFalse(any(e.get("code") == "600002" for e in rejects + terminals),
                         "status=6 不得写 reject/order_terminal")

    def test_empty_counterparty_detail_is_not_fabricated_and_ack_is_wired(self):
        self.assertEqual(self.by_code["600003"]["message"], "柜台拒单 status=8")
        self.assertEqual(self.by_code["600004"]["message"], "委托已过期 status=12")
        self.assertTrue(self.r["service_ack_wired"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
