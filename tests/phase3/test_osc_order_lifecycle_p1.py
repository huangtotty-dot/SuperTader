# -*- coding: utf-8 -*-
"""候选⑲独立审查 P1：六项委托链路阻断问题的行为回归。"""
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
from oversold_gap_confirm import backfill, executor, fillbackfill  # noqa: E402

DATE = "2026-10-30"


def _write_json(directory, name, payload):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _read_json(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as f:
        return json.load(f)


def _buy(code="600001", order_id="o1", status="submitted", qty=100,
         filled=0, price=10.0, filled_price=None):
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

    def positions(self):
        return []


class _Context:
    now = dt.datetime(2026, 10, 30, 10, 0)
    account = lambda self: _Account()


class _SyncGm:
    OrderSide_Buy, OrderSide_Sell = 1, 2
    OrderType_Market, OrderType_Limit = 1, 2
    PositionEffect_Open, PositionEffect_Close = 1, 2

    def __init__(self, response):
        self.response = response

    def order_volume(self, **kwargs):
        return [dict(self.response)]


def _services(gm):
    return GmServices(
        gm=gm, sdk_call=lambda desc, fn, *a, **k: fn(*a, **k),
        write_order=lambda *a, **k: None, write_order_ack=lambda *a, **k: None,
        mark_pending_recon=lambda *a: None,
        code_to_gm=lambda c: "SHSE." + c, raw_code=lambda s: str(s).split(".")[-1],
        param=lambda k, d=None: d, clock=lambda ctx: ctx.now,
        reject_statuses=(4, 5, 6, 8, 12), ownership=_Ownership(),
    )


class TestSyncFilledTruth(unittest.TestCase):
    def test_status3_reads_real_qty_price_and_full_fill_leaves_no_pending(self):
        responses = [
            {"order_id": "full-vol", "status": 3, "filled_volume": 100,
             "volume": 100, "filled_vwap": 10.5},
            {"order_id": "full-order-vol", "status": 3, "volume": 100,
             "filled_vwap": 10.6},
        ]
        for response in responses:
            with self.subTest(response=response):
                gw = GmGateway(_Context(), "osc", _services(_SyncGm(response)),
                               log=lambda *a: None)
                rep = gw.place_order("600001", "BUY", 100, 10.0)
                self.assertEqual(rep["status"], "filled")
                self.assertEqual(rep["filled_qty"], 100)
                self.assertEqual(rep["filled_price"], response["filled_vwap"])
                rec = _buy(status=rep["status"], filled=rep["filled_qty"],
                           filled_price=rep["filled_price"])
                self.assertEqual(backfill.pending_from_fills({"buys": [rec]}), [])

    def test_status3_without_real_volume_is_not_fake_filled(self):
        response = {"order_id": "missing-volume", "status": 3, "filled_vwap": 10.5}
        gw = GmGateway(_Context(), "osc", _services(_SyncGm(response)),
                       log=lambda *a: None)
        rep = gw.place_order("600001", "BUY", 100, 10.0)
        self.assertEqual(rep["status"], "submitted")
        self.assertEqual(rep["raw_status"], 3)
        self.assertEqual(rep["filled_qty"], 0)
        rec = _buy(status=rep["status"], filled=rep["filled_qty"])
        self.assertEqual(len(backfill.pending_from_fills({"buys": [rec]})), 1)

    def test_status3_with_only_partial_volume_stays_pending(self):
        response = {"order_id": "partial-volume", "status": 3,
                    "filled_volume": 40, "volume": 100, "filled_vwap": 10.5}
        gw = GmGateway(_Context(), "osc", _services(_SyncGm(response)),
                       log=lambda *a: None)
        rep = gw.place_order("600001", "BUY", 100, 10.0)
        self.assertEqual(rep["status"], "partial")
        rec = _buy(status=rep["status"], filled=rep["filled_qty"],
                   filled_price=rep["filled_price"])
        pending = backfill.pending_from_fills({"buys": [rec]})
        self.assertEqual(pending[0]["qty_remaining"], 60)


class TestTerminalRollbackOrdering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, rec, old=None):
        """只通过真实 register 路径播种账本，禁止手工虚构委托入账量。"""
        _write_json(self.dir, "positions.json",
                    {"updated_at": "", "open": old or {}, "closed": {}})
        executor.register_buys_to_book(
            self.dir, DATE, [rec], [{"code": rec["code"], "board": ""}],
            {rec["code"]: 9.0}, {rec["code"]: 10.0}, log=lambda *a: None)
        fills = {"date": DATE, "buys": [rec], "summary": {}}
        _write_json(self.dir, "fills_%s.json" % DATE, fills)
        _write_json(self.dir, "day_state_%s.json" % DATE,
                    {"date": DATE, "pending": backfill.pending_from_fills(fills)})
        return fills

    def test_apply_cancelled_then_host_event_rolls_back_exactly_once(self):
        rec = _buy()
        self._register(rec, old={"600009": {
            "entry_date": DATE, "entry_price": 20.0, "qty": 50}})
        self.assertEqual(rec["booked_qty"], 100, "初始 submitted 按计划量 100 入账")

        first = fillbackfill.apply_cancelled(self.dir, DATE, "600001", "o1",
                                             log=lambda *a: None)
        self.assertEqual(first["action"], "buy_cancelled_removed")
        self.assertEqual(first["qty"], 100)
        book_after = _read_json(self.dir, "positions.json")
        self.assertNotIn("600001", book_after["open"])
        self.assertEqual(book_after["open"]["600009"]["qty"], 50)
        snapshot = json.dumps(book_after, sort_keys=True)

        # 宿主 status=5 随后到达、同函数重放都不应再找到 submitted 留痕。
        second = fillbackfill.apply_reject(self.dir, {
            "time": DATE + " 10:08:01", "code": "600001", "side": "BUY",
            "qty": 100, "status": 5, "order_id": "o1"}, log=lambda *a: None)
        self.assertEqual(second["reason"], "semantic_guard")
        third = fillbackfill.apply_cancelled(self.dir, DATE, "600001", "o1",
                                             log=lambda *a: None)
        self.assertEqual(third["reason"], "semantic_guard")
        self.assertEqual(json.dumps(_read_json(self.dir, "positions.json"),
                                    sort_keys=True), snapshot)
        self.assertEqual(backfill.pending_from_fills(
            _read_json(self.dir, "fills_%s.json" % DATE)), [])
        self.assertEqual(_read_json(self.dir, "day_state_%s.json" % DATE)["pending"], [],
                         "重启只读磁盘也不得复活 cancelled pending")

    def test_sync_partial_released_without_old_position_keeps_real_fill(self):
        rec = _buy(status="partial", qty=100, filled=40, price=10.0, filled_price=11.0)
        self._register(rec)
        self.assertEqual(rec["booked_qty"], 40, "同步 partial 只把真实成交 40 股入账")
        before = _read_json(self.dir, "positions.json")
        self.assertEqual(before["open"]["600001"]["qty"], 40)
        self.assertEqual(before["open"]["600001"]["entry_price"], 11.0)

        first = fillbackfill.apply_cancelled(self.dir, DATE, "600001", "o1",
                                             log=lambda *a: None)
        self.assertEqual(first["action"], "buy_cancelled_no_rollback")
        self.assertEqual(first["qty"], 0, "booked 40 − filled 40，不得再回滚 60")
        snapshot = json.dumps(_read_json(self.dir, "positions.json"), sort_keys=True)
        self.assertEqual(snapshot, json.dumps(before, sort_keys=True))

        # released 后宿主 status=5、released 重放均不得再碰账本。
        host = fillbackfill.apply_reject(self.dir, {
            "time": DATE + " 10:08:01", "code": "600001", "side": "BUY",
            "qty": 100, "status": 5, "order_id": "o1"}, log=lambda *a: None)
        self.assertEqual(host["reason"], "semantic_guard")
        replay = fillbackfill.apply_cancelled(self.dir, DATE, "600001", "o1",
                                              log=lambda *a: None)
        self.assertEqual(replay["reason"], "semantic_guard")
        self.assertEqual(json.dumps(_read_json(self.dir, "positions.json"),
                                    sort_keys=True), snapshot)

    def test_sync_partial_terminal_with_old_position_preserves_weighted_cost(self):
        rec = _buy(status="partial", qty=100, filled=40, price=10.0, filled_price=11.0)
        self._register(rec, old={"600001": {
            "entry_date": "2026-10-29", "entry_price": 5.0, "qty": 100}})
        self.assertEqual(rec["booked_qty"], 40)
        before = _read_json(self.dir, "positions.json")
        expected_cost = (100 * 5.0 + 40 * 11.0) / 140
        self.assertEqual(before["open"]["600001"]["qty"], 140)
        self.assertAlmostEqual(before["open"]["600001"]["entry_price"], expected_cost, places=4)

        event = {"time": DATE + " 10:08:00", "code": "600001", "side": "BUY",
                 "qty": 100, "status": 12, "order_id": "o1"}
        result = fillbackfill.apply_reject(self.dir, event, log=lambda *a: None)
        self.assertEqual(result["action"], "buy_expired_no_rollback")
        self.assertEqual(result["qty"], 0)
        after = _read_json(self.dir, "positions.json")
        self.assertEqual(after["open"]["600001"]["qty"], 140, "旧仓和真实成交均不得被侵蚀")
        self.assertAlmostEqual(after["open"]["600001"]["entry_price"], expected_cost, places=4)
        snapshot = json.dumps(after, sort_keys=True)
        fillbackfill.apply_reject(self.dir, dict(event, time=DATE + " 10:08:02"),
                                  log=lambda *a: None)
        self.assertEqual(json.dumps(_read_json(self.dir, "positions.json"),
                                    sort_keys=True), snapshot, "异时重放也不得双扣")

    def test_submitted_then_partial_rolls_back_only_unfilled_booked_qty(self):
        # 初始 submitted 真实入账 100@10；随后得知 partial 40@11，终态只能回滚
        # booked 100 − filled 40 = 60，不得按整笔 100 删除，也不得误删旧仓。
        rec = _buy(status="submitted", qty=100, filled=0, price=10.0)
        self._register(rec, old={"600001": {
            "entry_date": "2026-10-29", "entry_price": 5.0, "qty": 100}})
        self.assertEqual(rec["booked_qty"], 100)
        rec.update({"status": "partial", "filled_qty": 40, "filled_price": 11.0,
                    "qty_remaining": 60})
        _write_json(self.dir, "fills_%s.json" % DATE,
                    {"date": DATE, "buys": [rec], "summary": {}})
        event = {"time": DATE + " 10:08:00", "code": "600001", "side": "BUY",
                 "qty": 100, "status": 5, "order_id": "o1"}
        result = fillbackfill.apply_reject(self.dir, event, log=lambda *a: None)
        self.assertEqual(result["action"], "buy_cancelled_unweighted")
        self.assertEqual(result["qty"], 60)
        entry = _read_json(self.dir, "positions.json")["open"]["600001"]
        self.assertEqual(entry["qty"], 140)
        self.assertAlmostEqual(entry["entry_price"], (500 + 440) / 140, places=4)
        snapshot = json.dumps(_read_json(self.dir, "positions.json"), sort_keys=True)
        fillbackfill.apply_reject(self.dir, dict(event, time=DATE + " 10:08:02"),
                                  log=lambda *a: None)
        self.assertEqual(json.dumps(_read_json(self.dir, "positions.json"),
                                    sort_keys=True), snapshot)

    def test_legacy_partial_without_booked_qty_conservatively_preserves_book(self):
        rec = _buy(status="partial", qty=100, filled=40, price=10.0, filled_price=11.0)
        self._register(rec, old={"600001": {
            "entry_date": "2026-10-29", "entry_price": 5.0, "qty": 100}})
        rec.pop("booked_qty")  # 模拟上线前旧留痕；账本仍由真实 register 生成。
        _write_json(self.dir, "fills_%s.json" % DATE,
                    {"date": DATE, "buys": [rec], "summary": {}})
        snapshot = json.dumps(_read_json(self.dir, "positions.json"), sort_keys=True)
        result = fillbackfill.apply_reject(self.dir, {
            "time": DATE + " 10:08:00", "code": "600001", "side": "BUY",
            "qty": 100, "status": 5, "order_id": "o1"}, log=lambda *a: None)
        self.assertEqual(result["action"], "buy_cancelled_no_rollback")
        self.assertEqual(result["qty"], 0, "旧留痕归属不明时保守不回滚，绝不删真实成交")
        self.assertEqual(json.dumps(_read_json(self.dir, "positions.json"),
                                    sort_keys=True), snapshot)


class TestPricelessRaceEnrichment(unittest.TestCase):
    def test_real_priced_fill_enriches_priceless_race_without_double_add(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_json(directory, "fills_%s.json" % DATE,
                        {"date": DATE, "buys": [_buy()], "summary": {}})
            _write_json(directory, "positions.json", {"updated_at": "", "open": {
                "600001": {"entry_date": DATE, "entry_price": 10.0, "qty": 100}
            }, "closed": {}})
            fillbackfill.apply_race_fill(directory, DATE, "600001", 100,
                                         price=None, order_id="o1", log=lambda *a: None)
            before = _read_json(directory, "fills_%s.json" % DATE)["buys"][0]
            self.assertEqual(before["status"], "filled_by_event")
            self.assertIsNone(before["filled_price"])

            event = {"time": DATE + " 10:08:01", "code": "600001", "side": "BUY",
                     "qty": 100, "price": 10.5, "order_id": "o1"}
            result = fillbackfill.apply_fill(directory, event, log=lambda *a: None)
            self.assertEqual(result["action"], "buy_corrected")
            rec = _read_json(directory, "fills_%s.json" % DATE)["buys"][0]
            entry = _read_json(directory, "positions.json")["open"]["600001"]
            self.assertEqual(rec["filled_price"], 10.5)
            self.assertEqual(rec["filled_qty"], 100)
            self.assertEqual(entry["qty"], 100)
            self.assertEqual(entry["entry_price"], 10.5)

            snapshot_book = json.dumps(_read_json(directory, "positions.json"), sort_keys=True)
            snapshot_fills = json.dumps(_read_json(directory, "fills_%s.json" % DATE),
                                        sort_keys=True)
            fillbackfill.apply_fill(directory, event, log=lambda *a: None)
            fillbackfill.apply_fill(directory, dict(event, time=DATE + " 10:08:03"),
                                    log=lambda *a: None)
            self.assertEqual(json.dumps(_read_json(directory, "positions.json"),
                                        sort_keys=True), snapshot_book)
            self.assertEqual(json.dumps(_read_json(directory, "fills_%s.json" % DATE),
                                        sort_keys=True), snapshot_fills)


class _BudgetGateway:
    def __init__(self):
        self.placed = []
        self.cancelled = []

    def get_cash(self):
        return 1_000_000.0

    def get_positions(self):
        return {}

    def place_order(self, code, side, qty, price):
        self.placed.append((code, qty, price))
        return {"order_id": None, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": ""}

    def cancel_order(self, order_id, code):
        self.cancelled.append(code)
        return {"ok": True, "order_id": "resolved-" + code, "message": ""}

    def poll_order(self, order_id, code):
        if code == "600001":
            return {"order_id": order_id, "status": "cancelled", "raw_status": 5,
                    "filled_qty": 0, "filled_price": None}
        return {"order_id": order_id, "status": "pending_cancel", "raw_status": 6,
                "filled_qty": 0, "filled_price": None}


class TestDailyBudgetTwoHundredK(unittest.TestCase):
    def test_default_is_200k_and_first_plus_backfill_active_notional_capped(self):
        self.assertEqual(executor.DEFAULTS["daily_budget"], 200000.0)
        codes = ["600001", "600002", "600003", "600004"]
        prev_close = {code: 9.0 for code in codes}
        ma20 = {code: 10.0 for code in codes}
        bars = {code: {"low": 9.0, "close": 10.0, "amount": 9900.0, "volume": 1000}
                for code in codes}
        gateway = _BudgetGateway()
        with tempfile.TemporaryDirectory() as directory:
            fills = executor.run_buy_exec(
                gateway, DATE, prev_close, ma20, bars, book_dir=directory,
                dry_run=True, sleep_fn=lambda seconds: None, log=lambda *a: None)
            self.assertEqual(fills["params"]["daily_budget"], 200000.0)
            self.assertEqual(fills["summary"]["notional_placed"], 199000.0)
            self.assertEqual([r["qty_placed"] for r in fills["buys"]],
                             [10000, 9500, 400, 0])

            # 600001 撤单释放、600002/600003 待撤仍冻结；补单只剩 10.1 万额度，
            # 实际候补下 9.5 万后，已成交+冻结+新补单累计活跃名义为 19.4 万。
            pending = backfill.pending_from_fills(fills)
            result = backfill.run_backfill_exec(
                gateway, DATE, fills, pending,
                [{"code": "600005", "gap": 0.04, "price": 10.0}],
                {"600005": 9.0},
                {"600005": {"price": 10.0, "vwap": 9.9, "ask_v": 100}},
                positions={}, params={"daily_budget": executor.DEFAULTS["daily_budget"],
                                      "single_budget": 100000.0,
                                      "order_poll_rounds": 1,
                                      "order_poll_sleep_sec": 0.0},
                sleep_fn=lambda seconds: None, log=lambda *a: None)
            self.assertEqual(result["cancel"]["released"], ["600001"])
            self.assertEqual(result["cancel"]["unconfirmed"], ["600002", "600003"])
            self.assertEqual(result["cash_budget"], 101000.0)
            self.assertEqual(len(result["placed"]), 1)
            new_notional = result["placed"][0]["qty"] * result["placed"][0]["price"]
            active = sum(result["committed"].values()) + new_notional
            self.assertEqual(active, 194000.0)
            self.assertLessEqual(active, 200000.0)


class TestGmMainPendingReconIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env["SMOKE_ROOT"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        driver = ROOT / "tests" / "phase3" / "gm_main_pending_recon_driver.py"
        proc = subprocess.run([sys.executable, str(driver)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=180)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if line is None:
            raise AssertionError("pending_recon 回放失败\nSTDOUT:\n%s\nSTDERR:\n%s" %
                                 (proc.stdout[-4000:], proc.stderr[-4000:]))
        cls.r = json.loads(line[7:])

    def test_A_other_symbol_fill_cannot_close_current_recon(self):
        self.assertFalse(self.r["closed"]["SHSE.600001"])
        self.assertFalse(any(x["symbol"] == "SHSE.600001" for x in self.r["seen"]))
        self.assertFalse(any(x["symbol"] == "SHSE.600009" for x in self.r["seen"]))

    def test_B_same_symbol_requires_matching_order_id(self):
        self.assertEqual([x for x in self.r["seen"] if x["symbol"] == "SHSE.600002"], [
            {"symbol": "SHSE.600002", "id": "B", "volume": 200}
        ])
        self.assertTrue(self.r["closed"]["SHSE.600002"])
        self.assertEqual(self.r["calls"], [
            "SHSE.600001", None, "SHSE.600002", None,
            "SHSE.600003", None, "SHSE.600004", None,
        ])

    def test_C_same_symbol_multiple_fills_without_id_stay_pending(self):
        self.assertFalse(self.r["closed"]["SHSE.600003"])
        self.assertFalse(any(x["symbol"] == "SHSE.600003" for x in self.r["seen"]),
                         "当前无 ID 且同票多个成交候选，绝不能猜测认领")

    def test_D_same_symbol_unique_fill_without_id_can_recover(self):
        self.assertEqual([x for x in self.r["seen"] if x["symbol"] == "SHSE.600004"], [
            {"symbol": "SHSE.600004", "id": "D-UNIQUE", "volume": 100}
        ])
        self.assertTrue(self.r["closed"]["SHSE.600004"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
