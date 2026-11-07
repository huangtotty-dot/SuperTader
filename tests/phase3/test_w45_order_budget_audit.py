# -*- coding: utf-8 -*-
"""W45 OSC 限价执行、预算真值、崩溃恢复与委托终态离线审计。

全部账本写入均发生在临时目录；生产 fills/events 只读，不连接交易终端、不下单。
本文件不再验收“按涨停价预留并动态缩量”：数量只在 build_buy_plan 计算一次，
执行阶段保持原数量，daily/cash/single 任一余量不足即整笔零 SDK。
"""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
AUTO = ROOT / "execution" / "auto"
for path in (str(ROOT), str(AUTO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from framework.gateway import GmGateway  # noqa: E402
from oversold_gap_confirm import backfill, executor, fillbackfill, order_intents  # noqa: E402

DATE = "2026-11-03"


def _write_json(directory, name, payload):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _read_json(directory, name):
    with open(os.path.join(directory, name), encoding="utf-8") as f:
        return json.load(f)


def _log(*args, **kwargs):
    return None


def _plan(code="600001", qty=1000, price=10.0):
    return {"code": code, "gap": 0.05, "price": price,
            "qty_target": qty, "qty_place": qty,
            "truncated": False, "defer_reason": None}


def _cfg(**overrides):
    base = dict(executor.DEFAULTS, daily_budget=200000.0, single_budget=100000.0,
                cash_headroom=0.95, min_lot=100, order_poll_rounds=0,
                order_poll_sleep_sec=0.0)
    base.update(overrides)
    return base


def _buy_record(code="600001", order_id="o1", qty=1000, price=10.0,
                status="submitted", filled=0, filled_price=None):
    return {"code": code, "symbol": executor.gm_symbol_of(code), "side": "BUY",
            "gap": 0.05, "price": price, "execution_limit_price": price,
            "execution_ceiling_price": price, "budget_price_kind": "reference_limit",
            "qty_target": qty, "qty_placed": qty, "status": status,
            "filled_qty": filled, "filled_price": filled_price,
            "filled_notional": (filled * filled_price if filled and filled_price else 0.0),
            "reserved_notional": ((qty - filled) * price if status == "submitted" else 0.0),
            "fee_est": 0.0, "order_id": order_id, "qty_remaining": qty - filled,
            "booked_qty": qty, "message": "", "defer_reason": None}


class _Gateway:
    def __init__(self, replies=None, poll_replies=None, cash=1000000.0):
        self.replies = list(replies or [])
        self.poll_replies = dict(poll_replies or {})
        self.cash = cash
        self.calls = []
        self.cancelled = set()

    def get_cash(self):
        return self.cash

    def get_positions(self):
        return {}

    def place_order(self, code, side, qty, price, execution_limit_price=None):
        self.calls.append({"kind": "place", "code": code, "side": side, "qty": qty,
                           "price": price, "limit": execution_limit_price})
        if self.replies:
            value = self.replies.pop(0)
            if isinstance(value, BaseException):
                raise value
            return dict(value)
        return {"order_id": "sdk-%d" % len(self.calls), "status": "submitted",
                "filled_qty": 0, "filled_price": None, "message": ""}

    def poll_order(self, order_id, code):
        self.calls.append({"kind": "poll", "code": code, "order_id": order_id})
        value = self.poll_replies.get(str(order_id))
        if callable(value):
            return value(order_id, code)
        if value is not None:
            return dict(value)
        if str(order_id) in self.cancelled:
            return {"order_id": order_id, "status": "cancelled", "raw_status": 5,
                    "filled_qty": 0, "filled_price": None}
        return {"order_id": order_id, "status": "submitted", "raw_status": 1,
                "filled_qty": 0, "filled_price": None}

    def cancel_order(self, order_id, code):
        self.calls.append({"kind": "cancel", "code": code, "order_id": order_id})
        self.cancelled.add(str(order_id))
        return {"ok": True, "order_id": order_id, "message": ""}

    @property
    def places(self):
        return [c for c in self.calls if c["kind"] == "place"]


class TestOrderTerminalColumns(unittest.TestCase):
    def test_four_terminal_states_have_independent_columns(self):
        records = [
            {"code": "600001", "status": "rejected", "qty_placed": 100},
            {"code": "600002", "status": "cancelled", "qty_placed": 100},
            {"code": "600003", "status": "expired", "qty_placed": 100},
            {"code": "600004", "status": "error", "qty_placed": 100},
            {"code": "600005", "status": "submitted", "qty_placed": 100,
             "price": 10.0, "execution_limit_price": 10.0},
        ]
        summary = executor._buys_summary(records)
        self.assertEqual(summary["rejected_count"], 1)
        self.assertEqual(summary["cancelled_count"], 1)
        self.assertEqual(summary["expired_count"], 1)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["terminal_counts"], {
            "rejected": 1, "cancelled": 1, "expired": 1, "error": 1})
        self.assertEqual(summary["buys_placed"], 2,
                         "error 只保留历史报表计数，不再被当作已结算拒单")

    def test_three_real_days_recompute_cancelled_not_rejected(self):
        book_dir = ROOT / "t_io" / "state" / "weak_strong_book"
        for day in ("2026-11-02", "2026-11-03", "2026-11-04"):
            with self.subTest(day=day):
                path = book_dir / ("fills_%s.json" % day)
                self.assertTrue(path.is_file(), "真实留痕必须存在，不允许 skip: %s" % path)
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
                summary = executor._buys_summary(payload.get("buys") or [])
                self.assertEqual(summary["rejected_count"], 0)
                self.assertEqual(summary["cancelled_count"], 1)
                self.assertEqual(summary["expired_count"], 0)
                self.assertEqual(summary["error_count"], 0)
                self.assertEqual(summary["terminal_counts"], {
                    "rejected": 0, "cancelled": 1, "expired": 0, "error": 0})

    def test_submitted_to_cancelled_rolls_back_and_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = _buy_record(order_id="cancel-me", qty=100)
            _write_json(directory, "positions.json", {"updated_at": "", "open": {}, "closed": {}})
            executor.register_buys_to_book(
                directory, DATE, [rec], [{"code": "600001", "board": "半导体"}],
                {"600001": 10.0}, {"600001": 12.0}, log=_log)
            fills = {"date": DATE, "buys": [rec], "summary": {}}
            _write_json(directory, "fills_%s.json" % DATE, fills)
            _write_json(directory, "day_state_%s.json" % DATE,
                        {"date": DATE, "pending": backfill.pending_from_fills(fills)})
            event = {"time": DATE + " 10:08:00", "code": "600001", "side": "BUY",
                     "qty": 100, "status": 5, "order_id": "cancel-me"}

            result = fillbackfill.apply_reject(directory, event, log=_log)
            self.assertEqual(result["action"], "buy_cancelled_removed")
            final_fills = _read_json(directory, "fills_%s.json" % DATE)
            final_rec = final_fills["buys"][0]
            self.assertEqual(final_rec["status"], "cancelled")
            self.assertEqual(final_rec["reserved_notional"], 0.0)
            self.assertEqual(final_rec["qty_remaining"], 0)
            self.assertEqual(final_fills["summary"]["cancelled_count"], 1)
            self.assertEqual(final_fills["summary"]["rejected_count"], 0)
            self.assertEqual(backfill.pending_from_fills(final_fills), [])
            self.assertEqual(_read_json(directory, "day_state_%s.json" % DATE)["pending"], [])
            self.assertNotIn("600001", _read_json(directory, "positions.json")["open"])

            snapshots = [json.dumps(_read_json(directory, name), sort_keys=True)
                         for name in ("positions.json", "fills_%s.json" % DATE)]
            replay = fillbackfill.apply_reject(directory, event, log=_log)
            self.assertEqual(replay["action"], "skipped")
            self.assertEqual(replay["reason"], "duplicate_key")
            self.assertEqual(snapshots, [json.dumps(_read_json(directory, name), sort_keys=True)
                                         for name in ("positions.json", "fills_%s.json" % DATE)])

    def test_sell_partial_fill_terminal_preserves_real_fill_and_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = {"code": "600002", "symbol": "SHSE.600002", "side": "SELL",
                   "order_id": "sell-partial", "reason": "pop_sell", "price": 10.0,
                   "qty_placed": 1000, "status": "partial", "filled_qty": 400,
                   "filled_price": 9.9, "qty_remaining": 600, "fee_est": 0.0,
                   "message": "", "intent_id": "sell-intent"}
            fills = {"date": DATE, "sells": [rec], "summary": {}}
            _write_json(directory, "sells_close_%s.json" % DATE, fills)
            _write_json(directory, "positions.json", {
                "updated_at": "", "open": {"600002": {"qty": 600, "entry_price": 8.0}},
                "closed": {"sentinel": {"qty": 7, "price": 1.23}}})
            order_intents.upsert_fill_record(directory, DATE, rec, creating=True)

            result = fillbackfill.apply_reject(directory, {
                "time": DATE + " 14:50:01", "code": "600002", "side": "SELL",
                "qty": 1000, "status": 5, "order_id": "sell-partial"}, log=_log)
            self.assertEqual(result["action"], "sell_cancelled")
            final = _read_json(directory, "sells_close_%s.json" % DATE)
            final_rec = final["sells"][0]
            self.assertEqual(final_rec["status"], "cancelled")
            self.assertEqual(final_rec["filled_qty"], 400)
            self.assertEqual(final_rec["filled_price"], 9.9)
            self.assertEqual(final_rec["qty_remaining"], 0)
            self.assertEqual(final["summary"]["sells_filled"], 1)
            self.assertEqual(final["summary"]["cancelled_count"], 1)
            self.assertEqual(final["summary"]["order_expired_count"], 0)
            book = _read_json(directory, "positions.json")
            self.assertEqual(book["open"]["600002"]["qty"], 600)
            self.assertEqual(book["closed"], {"sentinel": {"qty": 7, "price": 1.23}})
            intent = order_intents.load(directory, DATE, strict=True)[0]
            self.assertEqual(intent["side"], "SELL")
            self.assertEqual(intent["status"], "cancelled")
            self.assertEqual(intent["filled_qty"], 400)


class TestReferenceLimitPlanning(unittest.TestCase):
    def test_decimal_integer_lot_and_reference_price_floor_without_epsilon(self):
        enough = executor.build_buy_plan(
            [_plan(price=10.009, qty=0)], {}, 1001.0, 1001.0,
            min_lot=100, cash_headroom=1.0)[0]
        short = executor.build_buy_plan(
            [_plan(price=10.009, qty=0)], {}, 1000.89, 1000.89,
            min_lot=100, cash_headroom=1.0)[0]
        self.assertEqual(enough["qty_place"], 100)
        self.assertEqual(short["qty_place"], 0, "少 0.01 元也不得用 epsilon 凑一手")
        self.assertEqual(executor.execution_limit_price(10.009), 10.0)
        self.assertEqual(executor.execution_limit_price("28.129999999"), 28.12)

        gateway = _Gateway()
        view = {"date": DATE, "buys": []}
        records, issues = executor._execute_buy_plan_with_budget(
            gateway, [enough], {}, view, 1001.0, 10000.0, 10000.0,
            _cfg(cash_headroom=1.0), sleep_fn=lambda _: None, log=_log)
        self.assertEqual(issues, [])
        self.assertEqual(records[0]["qty_placed"], 100)
        self.assertEqual(records[0]["execution_limit_price"], 10.0)
        self.assertEqual(gateway.places[0]["qty"], 100)
        self.assertEqual(gateway.places[0]["limit"], 10.0)

    def test_daily_cash_and_single_shortfall_each_skip_whole_order_with_zero_sdk(self):
        scenarios = {
            "daily": dict(daily_budget=9999.99, cash=100000.0, single_budget=100000.0),
            "cash": dict(daily_budget=200000.0, cash=10000.0, single_budget=100000.0),
            "single": dict(daily_budget=200000.0, cash=100000.0, single_budget=9999.99),
        }
        for name, scenario in scenarios.items():
            with self.subTest(kind=name):
                gateway = _Gateway(cash=scenario["cash"])
                view = {"date": DATE, "buys": []}
                records, issues = executor._execute_buy_plan_with_budget(
                    gateway, [_plan(qty=1000, price=10.009)], {}, view,
                    scenario["cash"], scenario["daily_budget"], scenario["single_budget"],
                    _cfg(cash_headroom=(0.95 if name == "cash" else 1.0)),
                    sleep_fn=lambda _: None, log=_log)
                self.assertEqual(issues, [])
                self.assertEqual(gateway.places, [])
                rec = records[0]
                self.assertEqual(rec["qty_target"], 1000)
                self.assertEqual(rec["qty_placed"], 0)
                self.assertEqual(rec["required_notional"], 10000.0)
                self.assertEqual(rec["defer_reason"], "budget_exceeds_original_order")


class TestBudgetTruthAndBreaches(unittest.TestCase):
    def _execute(self, reply):
        gateway = _Gateway(replies=[reply])
        view = {"date": DATE, "buys": []}
        records, issues = executor._execute_buy_plan_with_budget(
            gateway, [_plan(qty=1000, price=10.0)], {}, view,
            100000.0, 200000.0, 100000.0, _cfg(cash_headroom=1.0),
            sleep_fn=lambda _: None, log=_log)
        return records[0], issues, view

    def test_limit_fill_and_bounded_not_filled_have_separate_actual_reserved(self):
        filled, issues, _ = self._execute(
            {"order_id": "within", "status": "partial", "filled_qty": 400,
             "filled_price": 9.99})
        self.assertEqual(issues, [])
        self.assertEqual(filled["filled_notional"], 3996.0)
        self.assertEqual(filled["reserved_notional"], 6000.0)
        summary = executor._buys_summary([filled])
        self.assertEqual(summary["actual"], 3996.0)
        self.assertEqual(summary["reserved"], 6000.0)
        self.assertEqual(summary["committed"], 9996.0)

        pending, issues, _ = self._execute(
            {"order_id": "not-filled", "status": "submitted", "raw_status": 1,
             "filled_qty": 0, "filled_price": None})
        self.assertEqual(issues, [])
        self.assertEqual(pending["status"], "submitted")
        self.assertEqual(pending["filled_qty"], 0)
        self.assertEqual(pending["reserved_notional"], 10000.0)
        summary = executor._buys_summary([pending])
        self.assertEqual(summary["actual"], 0.0)
        self.assertEqual(summary["committed"], 10000.0)

    def test_missing_vwap_does_not_fabricate_actual_but_commits_conservatively(self):
        rec, issues, view = self._execute(
            {"order_id": "priceless", "status": "partial", "filled_qty": 400,
             "filled_price": None})
        self.assertEqual(issues, [])
        self.assertEqual(rec["filled_qty"], 400)
        self.assertIsNone(rec["filled_price"])
        self.assertIsNone(rec["filled_notional"])
        self.assertEqual(backfill.actual_buy_notional(view), 0.0)
        self.assertEqual(backfill.committed_notional(view)["600001"], 10000.0)
        summary = executor._buys_summary([rec])
        self.assertEqual(summary["actual"], 0.0)
        self.assertEqual(summary["committed"], 10000.0)

    def test_sync_over_limit_fill_preserves_price_qty_fee_and_marks_breach(self):
        rec, issues, view = self._execute(
            {"order_id": "sync-breach", "status": "filled", "filled_qty": 1000,
             "filled_price": 10.01, "fee_actual": 1.23})
        self.assertEqual(rec["filled_qty"], 1000)
        self.assertEqual(rec["filled_price"], 10.01)
        self.assertEqual(rec["filled_notional"], 10010.0)
        self.assertEqual(rec["fee_actual"], 1.23)
        self.assertEqual(rec["budget_bound_breach"]["kind"], "fill_above_execution_limit")
        self.assertEqual(rec["budget_bound_breach"]["source"], "sync_or_poll")
        self.assertTrue(issues)
        self.assertEqual(backfill.actual_buy_notional(view), 10010.0)

    def test_async_over_limit_fill_preserves_truth_fee_intent_and_breach(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = _buy_record(order_id="async-breach", qty=1000)
            rec["intent_id"] = "async-intent"
            fills = {"date": DATE, "picks": [{"code": "600001", "gap": 0.05}],
                     "buys": [rec], "summary": executor._buys_summary([rec])}
            _write_json(directory, "fills_%s.json" % DATE, fills)
            _write_json(directory, "exec_report_%s.json" % DATE,
                        {"date": DATE, "summary": fills["summary"]})
            _write_json(directory, "positions.json", {
                "updated_at": "", "open": {"600001": {
                    "entry_date": DATE, "entry_price": 10.0, "qty": 1000,
                    "board": "半导体", "days_held": 0}}, "closed": {}})
            order_intents.upsert_fill_record(directory, DATE, rec, creating=True)
            result = fillbackfill.apply_fill(directory, {
                "time": DATE + " 10:01:00", "code": "600001", "side": "BUY",
                "qty": 1000, "price": 10.01, "fill_vwap": 10.01,
                "fee": 1.23, "order_id": "async-breach"}, log=_log)
            self.assertEqual(result["action"], "buy_corrected")
            final = _read_json(directory, "fills_%s.json" % DATE)
            out = final["buys"][0]
            self.assertEqual(out["filled_qty"], 1000)
            self.assertEqual(out["filled_price"], 10.01)
            self.assertEqual(out["filled_notional"], 10010.0)
            self.assertEqual(out["fee_actual"], 1.23)
            self.assertEqual(out["budget_bound_breach"]["source"], "async_fill")
            self.assertEqual(final["summary"]["actual"], 10010.0)
            self.assertEqual(final["summary"]["budget_bound_breaches"], [out["budget_bound_breach"]])
            self.assertEqual(_read_json(directory, "positions.json")["open"]["600001"], {
                "entry_date": DATE, "entry_price": 10.01, "qty": 1000,
                "board": "半导体", "days_held": 0})
            intent = order_intents.load(directory, DATE, strict=True)[0]
            self.assertEqual(intent["filled_notional"], 10010.0)
            self.assertEqual(intent["budget_bound_breach"]["source"], "async_fill")


class TestOrderIntentRecovery(unittest.TestCase):
    def test_sdk_pre_reservation_write_failure_means_zero_sdk_and_blocks_following_buys(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = _Gateway()
            view = {"date": DATE, "buys": []}
            with mock.patch.object(order_intents, "upsert_fill_record",
                                   side_effect=OSError("injected_reserve_write_failure")):
                records, issues = executor._execute_buy_plan_with_budget(
                    gateway, [_plan("600001"), _plan("600002")], {}, view,
                    100000.0, 200000.0, 100000.0, _cfg(),
                    book_dir=directory, date=DATE, sleep_fn=lambda _: None, log=_log)
            self.assertEqual(gateway.places, [])
            self.assertEqual(records[0]["status"], "error")
            self.assertEqual(records[0]["defer_reason"], "order_intent_persist_failed")
            self.assertEqual(records[0]["qty_placed"], 0)
            self.assertEqual(records[1]["defer_reason"], "budget_or_recovery_blocked")
            self.assertEqual(issues[0]["kind"], "order_intent_persist_failed")

    @staticmethod
    def _seed_unacked_intent(directory, code="600001"):
        rec = _buy_record(code=code, order_id=None, qty=1000)
        rec["status"] = "submitted"  # SDK 已发、ack/order_id 尚未落盘的崩溃现场
        rec.pop("order_id", None)
        order_intents.upsert_fill_record(directory, DATE, rec, creating=True)

    def test_first_round_and_backfill_crash_without_order_id_are_fail_closed(self):
        for mode in ("first_round", "backfill"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self._seed_unacked_intent(directory)
                gateway = _Gateway()
                if mode == "first_round":
                    view = {"date": DATE, "buys": []}
                    records, issues = executor._execute_buy_plan_with_budget(
                        gateway, [_plan("600002")], {}, view, 100000.0,
                        200000.0, 100000.0, _cfg(), book_dir=directory, date=DATE,
                        sleep_fn=lambda _: None, log=_log)
                    self.assertEqual(records[0]["defer_reason"], "budget_or_recovery_blocked")
                else:
                    result = backfill.run_backfill_exec(
                        gateway, DATE, {"date": DATE, "buys": []}, [],
                        [{"code": "600002", "gap": 0.04, "price": 10.0}],
                        {"600002": 9.0},
                        {"600002": {"price": 10.0, "vwap": 9.5, "ask_v": 100}},
                        positions={}, params=_cfg(), book_dir=directory,
                        sleep_fn=lambda _: None, log=_log)
                    issues = result["budget_bound_breaches"]
                    self.assertEqual(result["placed"], [])
                    self.assertEqual(result["note"], "budget_bound_breach")
                self.assertEqual(gateway.places, [])
                self.assertFalse([c for c in gateway.calls if c["kind"] == "poll"],
                                 "无 ID 极端窗不能猜测轮询对象")
                self.assertTrue(any(i.get("kind") == "recovery_order_id_missing"
                                    for i in issues))

    def test_known_order_id_polls_first_and_recovers_even_when_fills_file_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = _buy_record(order_id="known-id", qty=1000)
            order_intents.upsert_fill_record(directory, DATE, rec, creating=True)
            gateway = _Gateway(poll_replies={"known-id": {
                "order_id": "known-id", "status": "cancelled", "raw_status": 5,
                "filled_qty": 0, "filled_price": None}})
            view = {"date": DATE, "buys": []}
            records, issues = executor._execute_buy_plan_with_budget(
                gateway, [_plan("600002")], {}, view, 100000.0,
                200000.0, 100000.0, _cfg(), book_dir=directory, date=DATE,
                sleep_fn=lambda _: None, log=_log)
            self.assertEqual(issues, [])
            self.assertEqual([c["kind"] for c in gateway.calls], ["poll", "place"])
            self.assertEqual(view["buys"][0]["order_id"], "known-id")
            self.assertEqual(view["buys"][0]["status"], "cancelled")
            self.assertEqual(records[0]["code"], "600002")
            self.assertGreater(records[0]["qty_placed"], 0)
            self.assertFalse(os.path.exists(os.path.join(directory, "fills_%s.json" % DATE)),
                             "本测试刻意不创建 fills，恢复真源只能是 order_intents")


class TestUnresolvedOrderFailure(unittest.TestCase):
    def test_place_timeout_and_other_exception_keep_reservation_and_block_restart(self):
        for exc in (TimeoutError("offline_timeout"), RuntimeError("offline_unknown")):
            with self.subTest(exc=type(exc).__name__), tempfile.TemporaryDirectory() as directory:
                gateway = _Gateway(replies=[exc])
                view = {"date": DATE, "buys": []}
                records, issues = executor._execute_buy_plan_with_budget(
                    gateway, [_plan("600001"), _plan("600002")], {}, view,
                    100000.0, 200000.0, 100000.0, _cfg(), book_dir=directory,
                    date=DATE, sleep_fn=lambda _: None, log=_log)
                first = records[0]
                self.assertEqual(len(gateway.places), 1)
                self.assertEqual(first["status"], "unresolved")
                self.assertEqual((first["qty_placed"], first["qty_remaining"]), (1000, 1000))
                self.assertEqual(first["reserved_notional"], 10000.0)
                self.assertEqual(records[1]["defer_reason"], "budget_or_recovery_blocked")
                self.assertTrue(any(i.get("kind") == "order_result_unresolved" for i in issues))
                saved = order_intents.load(directory, DATE, strict=True)[0]
                self.assertEqual(saved["status"], "unresolved")
                self.assertEqual(saved["qty_remaining"], 1000)
                self.assertEqual(saved["reserved_notional"], 10000.0)

                restarted = _Gateway()
                records2, issues2 = executor._execute_buy_plan_with_budget(
                    restarted, [_plan("600003")], {}, {"date": DATE, "buys": []},
                    100000.0, 200000.0, 100000.0, _cfg(), book_dir=directory,
                    date=DATE, sleep_fn=lambda _: None, log=_log)
                self.assertEqual(restarted.places, [])
                self.assertEqual(records2[0]["defer_reason"], "budget_or_recovery_blocked")
                self.assertTrue(any(i.get("kind") == "recovery_order_id_missing"
                                    for i in issues2))
                saved2 = order_intents.load(directory, DATE, strict=True)[0]
                self.assertEqual(saved2["reserved_notional"], 10000.0)

    def test_poll_exception_is_unresolved_not_a_submitted_pass_through(self):
        def _fail(_oid, _code):
            raise TimeoutError("offline_poll_timeout")

        with tempfile.TemporaryDirectory() as directory:
            gateway = _Gateway(
                replies=[{"order_id": "poll-id", "status": "submitted", "filled_qty": 0}],
                poll_replies={"poll-id": _fail})
            records, issues = executor._execute_buy_plan_with_budget(
                gateway, [_plan("600001"), _plan("600002")], {}, {"date": DATE, "buys": []},
                100000.0, 200000.0, 100000.0, _cfg(order_poll_rounds=1),
                book_dir=directory, date=DATE, sleep_fn=lambda _: None, log=_log)
            self.assertEqual(records[0]["status"], "unresolved")
            self.assertEqual(records[0]["reserved_notional"], 10000.0)
            self.assertEqual(records[1]["defer_reason"], "budget_or_recovery_blocked")
            self.assertEqual(len(gateway.places), 1)
            self.assertTrue(any(i.get("kind") == "order_result_unresolved" for i in issues))

    def test_backfill_place_exception_blocks_following_spare(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = _Gateway(replies=[TimeoutError("offline_backfill_timeout")])
            spares = [{"code": "600001", "gap": 0.05, "price": 10.0},
                      {"code": "600002", "gap": 0.04, "price": 10.0}]
            quotes = {code: {"price": 10.0, "vwap": 9.5, "ask_v": 100}
                      for code in ("600001", "600002")}
            result = backfill.run_backfill_exec(
                gateway, DATE, {"date": DATE, "buys": []}, [], spares,
                {"600001": 9.0, "600002": 9.0}, quotes, positions={},
                params=_cfg(single_budget=10000.0),
                book_dir=directory, sleep_fn=lambda _: None, log=_log)
            self.assertEqual(len(gateway.places), 1)
            self.assertEqual(result["placed"][0]["status"], "unresolved")
            self.assertEqual(result["placed"][0]["reserved_notional"], 10000.0)
            self.assertEqual(result["skipped"][0]["reason"], "budget_or_recovery_blocked")
            self.assertTrue(any(i.get("kind") == "order_result_unresolved"
                                for i in result["budget_bound_breaches"]))

    def test_ack_second_write_failure_and_restart_poll_failure_keep_reservation(self):
        def _flaky_ack(book_dir, date, rec):
            order_intents.upsert_fill_record(book_dir, date, rec)  # 第一写已落 order_id
            raise OSError("offline_ack_second_write_failure")

        def _poll_fail(_oid, _code):
            raise TimeoutError("offline_recovery_poll_failure")

        with tempfile.TemporaryDirectory() as directory:
            gateway = _Gateway(replies=[{
                "order_id": "ack-id", "status": "submitted", "filled_qty": 0}])
            with mock.patch.object(order_intents, "acknowledge", side_effect=_flaky_ack):
                records, issues = executor._execute_buy_plan_with_budget(
                    gateway, [_plan("600001"), _plan("600002")], {}, {"date": DATE, "buys": []},
                    100000.0, 200000.0, 100000.0, _cfg(), book_dir=directory,
                    date=DATE, sleep_fn=lambda _: None, log=_log)
            self.assertEqual(records[0]["status"], "unresolved")
            self.assertEqual(records[0]["reserved_notional"], 10000.0)
            self.assertEqual(records[1]["defer_reason"], "budget_or_recovery_blocked")
            self.assertTrue(any(i.get("kind") == "order_intent_ack_persist_failed"
                                for i in issues))
            saved = order_intents.load(directory, DATE, strict=True)[0]
            self.assertEqual((saved["order_id"], saved["status"]), ("ack-id", "unresolved"))

            restarted = _Gateway(poll_replies={"ack-id": _poll_fail})
            records2, issues2 = executor._execute_buy_plan_with_budget(
                restarted, [_plan("600003")], {}, {"date": DATE, "buys": []},
                100000.0, 200000.0, 100000.0, _cfg(), book_dir=directory,
                date=DATE, sleep_fn=lambda _: None, log=_log)
            self.assertEqual(restarted.places, [])
            self.assertEqual(records2[0]["defer_reason"], "budget_or_recovery_blocked")
            self.assertTrue(any(i.get("kind") == "recovery_order_unresolved"
                                for i in issues2))
            saved2 = order_intents.load(directory, DATE, strict=True)[0]
            self.assertEqual(saved2["reserved_notional"], 10000.0)

    def test_recovery_writeback_failure_does_not_impersonate_disk_release(self):
        with tempfile.TemporaryDirectory() as directory:
            rec = _buy_record(order_id="recover-id", qty=1000)
            order_intents.upsert_fill_record(directory, DATE, rec, creating=True)
            gateway = _Gateway(poll_replies={"recover-id": {
                "order_id": "recover-id", "status": "cancelled", "raw_status": 5,
                "filled_qty": 0, "filled_price": None}})
            with mock.patch.object(order_intents, "write_all",
                                   side_effect=OSError("offline_recovery_write_failure")):
                records, issues = executor._execute_buy_plan_with_budget(
                    gateway, [_plan("600002")], {}, {"date": DATE, "buys": []},
                    100000.0, 200000.0, 100000.0, _cfg(), book_dir=directory,
                    date=DATE, sleep_fn=lambda _: None, log=_log)
            self.assertEqual(gateway.places, [])
            self.assertEqual(records[0]["defer_reason"], "budget_or_recovery_blocked")
            self.assertTrue(any(i.get("kind") == "order_intent_recovery_persist_failed"
                                for i in issues))
            saved = order_intents.load(directory, DATE, strict=True)[0]
            self.assertEqual(saved["status"], "submitted")
            self.assertEqual(saved["qty_remaining"], 1000)
            self.assertEqual(saved["reserved_notional"], 10000.0)


class TestOrderIdentityAndRawStatus(unittest.TestCase):
    def test_same_symbol_multiple_orders_settle_by_order_id_not_code(self):
        first = _buy_record(order_id="order-a", qty=1000, price=10.0,
                            filled=200, filled_price=9.9)
        second = _buy_record(order_id="order-b", qty=2000, price=20.0)
        fills = {"date": DATE, "buys": [first, second]}
        gateway = _Gateway(poll_replies={
            "order-a": {"order_id": "order-a", "status": "cancelled", "raw_status": 5,
                        "filled_qty": 200, "filled_price": 9.9},
            "order-b": {"order_id": "order-b", "status": "pending_cancel", "raw_status": 6,
                        "filled_qty": 0, "filled_price": None},
        })
        result = backfill.run_backfill_exec(
            gateway, DATE, fills, backfill.pending_from_fills(fills), [], {},
            {}, positions={}, params=_cfg(order_poll_rounds=1),
            sleep_fn=lambda _: None, log=_log)
        self.assertEqual(result["cancel"]["released"], ["600001"])
        self.assertEqual(result["cancel"]["unconfirmed"], ["600001"])
        self.assertEqual(result["committed"]["600001"], 41980.0,
                         "只释放 order-a 余量；order-b 的 4 万不得按 code 一并释放")
        self.assertEqual(result["reserved"]["600001"], 40000.0)

    def test_raw_5_6_12_volume_is_not_fill_volume(self):
        for raw, expected in ((5, "cancelled"), (6, "pending_cancel"), (12, "expired")):
            with self.subTest(raw_status=raw):
                gm = types.SimpleNamespace(get_orders=lambda **kw: [{
                    "id": "raw-order", "symbol": kw.get("symbol"), "status": raw,
                    "volume": 1000, "vwap": 11.0, "price": 11.0}])
                services = types.SimpleNamespace(
                    gm=gm, sdk_call=lambda desc, fn, *a, **k: fn(*a, **k),
                    code_to_gm=lambda code: "SHSE." + code,
                    reject_statuses=(4, 5, 6, 8, 12))
                gateway = GmGateway(None, "osc", services, log=_log)
                result = gateway.poll_order("raw-order", "600001")
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["raw_status"], raw)
                self.assertEqual(result["filled_qty"], 0,
                                 "非全成状态的 volume 是委托量，不能冒充成交量")


class TestNovemberThirdLimitReplay(unittest.TestCase):
    PREV = {"605388": 5.98, "300085": 26.67, "002558": 20.68, "601999": 6.66,
            "600881": 1.56, "000848": 7.57, "600515": 2.60}
    REFS = {"605388": 6.58, "300085": 28.12, "002558": 21.67, "601999": 6.85,
            "600881": 1.60, "000848": 7.75, "600515": 2.64}
    INITIAL = ["605388", "300085", "002558", "601999"]
    SPARES = ["600881", "000848", "600515"]
    PRODUCTION_VWAPS = {
        "300085": 28.040000915527344, "002558": 21.610000610351562,
        "600881": 1.6200000047683716,
        "000848": 7.755000114440918, "600515": 2.640000104904175,
    }
    PRODUCTION_FEES = {
        "300085": 19.431720480203364, "002558": 5.0,
        "600881": 20.241899898892175, "000848": 5.0, "600515": 5.0,
    }
    # 仅用于验证硬顶的合成控制组，绝不代表生产成交，也不替代生产锚点。
    SYNTHETIC_WITHIN_LIMIT_VWAPS = {
        "300085": 28.040000915527344, "002558": 21.610000610351562,
        "600881": 1.60, "000848": 7.70, "600515": 2.60,
    }
    SYNTHETIC_FEES = {code: 0.01 for code in PRODUCTION_FEES}

    class _Gateway(_Gateway):
        def get_positions(self):
            return {"601999": {"qty": 100, "available": 0, "cost": 6.50}}

    @staticmethod
    def _fill(directory, rec, raw_vwap, minute, fee):
        return fillbackfill.apply_fill(directory, {
            "time": DATE + " 10:%02d:00" % minute, "code": rec["code"], "side": "BUY",
            "qty": int(rec["qty_placed"]), "price": raw_vwap, "fill_vwap": raw_vwap,
            "fee": fee, "order_id": rec.get("order_id") or ""}, log=_log)

    def _replay(self, directory, vwaps, fees):
        ma20 = {code: value * 1.20 for code, value in self.PREV.items()}
        bars = {}
        for code, close in self.REFS.items():
            vwap = close * 0.99
            bars[code] = {"low": self.PREV[code], "close": close,
                          "vwap": vwap, "volume": 10000, "amount": vwap * 10000}
        board = {code: "半导体" for code in self.PREV}
        params = _cfg(top_n=4, order_poll_rounds=1)
        # 生产锚点现金：199263.07 元；不得用人工抬价或合成组替代真实 VWAP/费用。
        gateway = self._Gateway(cash=199263.07)
        first = executor.run_buy_exec(
            gateway, DATE, self.PREV, ma20, bars, board=board, book_dir=directory,
            params=params, sleep_fn=lambda _: None, log=_log)

        self.assertEqual([r["code"] for r in first["buys"]], self.INITIAL)
        self.assertEqual([r["code"] for r in first["spares"]], self.SPARES)
        expected_first = {"605388": (15100, 6.58), "300085": (3300, 28.12),
                          "002558": (300, 21.67), "601999": (0, 6.85)}
        for rec in first["buys"]:
            qty, limit = expected_first[rec["code"]]
            self.assertEqual(rec["qty_placed"], qty)
            self.assertEqual(rec["execution_limit_price"], limit)
        self.assertEqual(first["buys"][-1]["defer_reason"],
                         "t1_locked(当日已持无可卖,加仓暂缓)")
        self.assertEqual(first["summary"]["committed"], 198655.0)

        for minute, code in enumerate(("300085", "002558"), start=1):
            rec = next(r for r in first["buys"] if r["code"] == code)
            self._fill(directory, rec, vwaps[code], minute, fees[code])
        on_disk = _read_json(directory, "fills_%s.json" % DATE)
        pending = backfill.pending_from_fills(on_disk)
        self.assertEqual([p["code"] for p in pending], ["605388"])
        quotes = {
            "600881": {"price": 1.61, "vwap": 1.60, "ask_v": 1000},
            "000848": {"price": 7.74, "vwap": 7.70, "ask_v": 1000},
            "600515": {"price": 2.64, "vwap": 2.60, "ask_v": 1000},
        }
        result = backfill.run_backfill_exec(
            gateway, DATE, on_disk, pending, on_disk["spares"], self.PREV, quotes,
            positions={}, params=params, book_dir=directory,
            sleep_fn=lambda _: None, log=_log)
        self.assertEqual(result["cancel"]["released"], ["605388"])
        self.assertEqual([r["code"] for r in result["placed"]], self.SPARES)
        expected_backfill = {"600881": (59500, 1.61),
                             "000848": (600, 7.74), "600515": (100, 2.64)}
        for rec in result["placed"]:
            qty, limit = expected_backfill[rec["code"]]
            self.assertEqual(rec["qty"], qty)
            self.assertEqual(rec["execution_limit_price"], limit)
        pre_async = sum(result["committed"].values()) + sum(
            float(r["reserved_notional"] or 0) for r in result["placed"])
        self.assertLessEqual(pre_async, 200000.0)

        cancelled = next(r for r in on_disk["buys"] if r["code"] == "605388")
        fillbackfill.apply_cancelled(directory, DATE, "605388",
                                     cancelled.get("order_id") or "", log=_log)
        merged = executor.merge_backfill_fills(
            directory, DATE, 1, result, placed=result["placed"],
            shift_from=result["released"], log=_log)
        self.assertIsNotNone(merged)
        executor.register_buys_to_book(
            directory, DATE, merged,
            [{"code": code, "board": "半导体"} for code in self.SPARES],
            self.PREV, ma20, log=_log)
        for minute, rec in enumerate(merged, start=3):
            self._fill(directory, rec, vwaps[rec["code"]], minute, fees[rec["code"]])
        return _read_json(directory, "fills_%s.json" % DATE)

    def test_synthetic_within_limit_control_group_proves_hard_cap(self):
        """合成控制组只验证硬顶，不是生产成交回放。"""
        with tempfile.TemporaryDirectory() as directory:
            final = self._replay(
                directory, self.SYNTHETIC_WITHIN_LIMIT_VWAPS, self.SYNTHETIC_FEES)
            actual = backfill.actual_buy_notional(final)
            quantities = {"300085": 3300, "002558": 300, "600881": 59500,
                          "000848": 600, "600515": 100}
            expected = sum(quantities[code] * self.SYNTHETIC_WITHIN_LIMIT_VWAPS[code]
                           for code in quantities)
            self.assertLessEqual(actual, 200000.0)
            self.assertAlmostEqual(actual, expected, places=9)
            self.assertLessEqual(sum(backfill.committed_notional(final).values()), 200000.0)
            self.assertEqual(final["summary"]["budget_bound_breaches"], [])
            self.assertEqual(final["summary"]["cancelled_count"], 1)
            self.assertEqual(final["summary"]["fills_event_confirmed"], 5)

    def test_production_high_vwaps_are_breaches_not_a_legal_replay_or_higher_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            final = self._replay(directory, self.PRODUCTION_VWAPS, self.PRODUCTION_FEES)
            by_code = {r["code"]: r for r in final["buys"]}
            intents = {r["code"]: r for r in order_intents.load(directory, DATE, strict=True)}
            self.assertEqual(final["summary"]["cash_before"], 199263.07)
            for code, fee in self.PRODUCTION_FEES.items():
                self.assertEqual(by_code[code]["filled_price"], self.PRODUCTION_VWAPS[code])
                self.assertEqual(intents[code]["filled_price"], self.PRODUCTION_VWAPS[code])
                self.assertEqual(by_code[code]["fee_actual"], fee)
                self.assertEqual(intents[code]["fee_actual"], fee,
                                 "真实费用必须跨 order_intents 完整保存")
            for code in self.SPARES:
                rec = by_code[code]
                self.assertGreater(self.PRODUCTION_VWAPS[code], rec["execution_limit_price"])
                self.assertEqual(rec["budget_bound_breach"]["kind"],
                                 "fill_above_execution_limit")
                self.assertGreater(rec["budget_bound_breach"]["filled_price"],
                                   rec["execution_limit_price"])
                self.assertEqual(rec["budget_bound_breach"]["source"], "async_fill")
                self.assertEqual(rec["filled_notional"],
                                 rec["filled_qty"] * self.PRODUCTION_VWAPS[code])
            self.assertGreater(backfill.actual_buy_notional(final), 200000.0)
            self.assertEqual({b["code"] for b in final["summary"]["budget_bound_breaches"]},
                             set(self.SPARES))


class TestThreeDayTerminalDriver(unittest.TestCase):
    def test_raw_order_terminal_events_replay_from_preterminal_state(self):
        with tempfile.TemporaryDirectory() as output:
            env = dict(os.environ, SMOKE_ROOT=str(ROOT), PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "tests" / "phase3" /
                                   "w45_three_day_terminal_driver.py"),
                 "--output-dir", output],
                cwd=str(ROOT), env=env, encoding="utf-8", errors="replace",
                capture_output=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             "驱动失败\nSTDOUT:\n%s\nSTDERR:\n%s" % (proc.stdout, proc.stderr))
            lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
            self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
            result = json.loads(lines[0][len("RESULT "):])
            self.assertTrue(result["ok"])
            self.assertEqual([r["date"] for r in result["days"]],
                             ["2026-11-02", "2026-11-03", "2026-11-04"])
            self.assertTrue(all(r["checks"]["idempotent"] for r in result["days"]))


class TestReal688185AuditDriver(unittest.TestCase):
    def test_reference_broker_price_and_slippage_are_separate(self):
        env = dict(os.environ, SMOKE_ROOT=str(ROOT), PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "phase3" / "gm_main_w45_audit_driver.py")],
            cwd=str(ROOT), env=env, encoding="utf-8", errors="replace",
            capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         "驱动失败\nSTDOUT:\n%s\nSTDERR:\n%s" % (proc.stdout, proc.stderr))
        lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
        self.assertEqual(len(lines), 1, "缺少 RESULT\n%s\n%s" % (proc.stdout, proc.stderr))
        result = json.loads(lines[0][len("RESULT "):])
        event = next(r for r in result["events"] if r["code"] == "688185")
        source = result["source_fill"]
        self.assertEqual(event["reference_price"], 74.50)
        self.assertEqual(event["broker_order_price"], 62.78)
        self.assertEqual(event["order_price"], 62.78)
        self.assertEqual(event["order_price_kind"], "market_protection")
        self.assertEqual(event["slippage"], -0.0946)
        self.assertEqual(event["fill_vwap"], 74.40537540089001)
        for key in ("price", "fill_vwap", "qty", "fee"):
            self.assertEqual(event[key], source[key])
        no_reference = next(r for r in result["events"] if r["code"] == "600001")
        self.assertIsNone(no_reference["reference_price"])
        self.assertIsNone(no_reference["slippage"])
        self.assertEqual(result["book"]["closed"], {"sentinel": {"qty": 7, "price": 1.23}})
        self.assertEqual(result["book"]["open"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
