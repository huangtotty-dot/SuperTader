# -*- coding: utf-8 -*-
"""W45 未知柜台结果的 fail-closed 持久证据（全离线、临时/指定证据账本）。"""
import argparse
import json
import shutil
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AUTO = ROOT / "execution" / "auto"
for path in (str(ROOT), str(AUTO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from oversold_gap_confirm import backfill, executor, order_intents  # noqa: E402

DATE = "2026-11-03"
LOG = lambda *args, **kwargs: None


def _plan(code):
    return {"code": code, "gap": 0.05, "price": 10.0, "qty_target": 1000,
            "qty_place": 1000, "truncated": False, "defer_reason": None}


def _cfg(**overrides):
    out = dict(executor.DEFAULTS, daily_budget=200000.0, single_budget=100000.0,
               cash_headroom=0.95, min_lot=100, order_poll_rounds=0,
               order_poll_sleep_sec=0.0)
    out.update(overrides)
    return out


def _record(order_id="recover-id"):
    return {"code": "600001", "symbol": "SHSE.600001", "side": "BUY",
            "gap": 0.05, "price": 10.0, "execution_limit_price": 10.0,
            "execution_ceiling_price": 10.0, "budget_price_kind": "reference_limit",
            "qty_target": 1000, "qty_placed": 1000, "status": "submitted",
            "filled_qty": 0, "filled_price": None, "filled_notional": 0.0,
            "reserved_notional": 10000.0, "fee_est": 0.0, "order_id": order_id,
            "qty_remaining": 1000, "booked_qty": 1000, "message": "",
            "defer_reason": None}


class _Gateway:
    def __init__(self, replies=None, poll_replies=None):
        self.replies = list(replies or [])
        self.poll_replies = dict(poll_replies or {})
        self.calls = []

    def get_cash(self):
        return 1000000.0

    def get_positions(self):
        return {}

    def place_order(self, code, side, qty, price, execution_limit_price=None):
        self.calls.append({"kind": "place", "code": code, "qty": qty})
        if self.replies:
            value = self.replies.pop(0)
            if isinstance(value, BaseException):
                raise value
            return dict(value)
        return {"order_id": "sdk-id", "status": "submitted", "filled_qty": 0}

    def poll_order(self, order_id, code):
        self.calls.append({"kind": "poll", "code": code, "order_id": order_id})
        value = self.poll_replies.get(str(order_id))
        if isinstance(value, BaseException):
            raise value
        if value is not None:
            return dict(value)
        return {"order_id": order_id, "status": "submitted", "raw_status": 1,
                "filled_qty": 0, "filled_price": None}

    def cancel_order(self, order_id, code):
        self.calls.append({"kind": "cancel", "code": code, "order_id": order_id})
        return {"ok": True, "order_id": order_id}

    @property
    def places(self):
        return [row for row in self.calls if row["kind"] == "place"]


def _execute(gateway, directory, plans, cfg=None):
    return executor._execute_buy_plan_with_budget(
        gateway, plans, {}, {"date": DATE, "buys": []}, 100000.0,
        200000.0, 100000.0, cfg or _cfg(), book_dir=str(directory), date=DATE,
        sleep_fn=lambda _: None, log=LOG)


def _place_exceptions(root):
    cases = []
    for exc in (TimeoutError("offline_timeout"), RuntimeError("offline_unknown")):
        directory = root / ("place_" + type(exc).__name__)
        gateway = _Gateway(replies=[exc])
        records, issues = _execute(gateway, directory, [_plan("600001"), _plan("600002")])
        saved = order_intents.load(str(directory), DATE, strict=True)[0]
        restarted = _Gateway()
        records2, issues2 = _execute(restarted, directory, [_plan("600003")])
        saved2 = order_intents.load(str(directory), DATE, strict=True)[0]
        checks = {
            "one_initial_sdk_only": len(gateway.places) == 1,
            "unresolved": records[0]["status"] == "unresolved",
            "quantity_preserved": (records[0]["qty_placed"], records[0]["qty_remaining"])
                                  == (1000, 1000),
            "reservation_preserved": records[0]["reserved_notional"] == 10000.0,
            "following_buy_blocked": records[1]["defer_reason"] == "budget_or_recovery_blocked",
            "issue_recorded": any(i.get("kind") == "order_result_unresolved" for i in issues),
            "intent_persisted": saved["status"] == "unresolved"
                                and saved["reserved_notional"] == 10000.0,
            "restart_zero_sdk": not restarted.places,
            "restart_missing_id_blocked": any(i.get("kind") == "recovery_order_id_missing"
                                              for i in issues2),
            "restart_reservation_preserved": saved2["reserved_notional"] == 10000.0,
        }
        cases.append({"exception": type(exc).__name__, "ok": all(checks.values()),
                      "checks": checks, "records": records, "issues": issues,
                      "restart_records": records2, "restart_issues": issues2})
    return cases


def _poll_exception(root):
    directory = root / "poll_exception"
    gateway = _Gateway(
        replies=[{"order_id": "poll-id", "status": "submitted", "filled_qty": 0}],
        poll_replies={"poll-id": TimeoutError("offline_poll_timeout")})
    records, issues = _execute(gateway, directory, [_plan("600001"), _plan("600002")],
                               _cfg(order_poll_rounds=1))
    checks = {"unresolved": records[0]["status"] == "unresolved",
              "reservation_preserved": records[0]["reserved_notional"] == 10000.0,
              "following_buy_blocked": records[1]["defer_reason"] == "budget_or_recovery_blocked",
              "one_sdk_only": len(gateway.places) == 1,
              "issue_recorded": any(i.get("kind") == "order_result_unresolved" for i in issues)}
    return {"ok": all(checks.values()), "checks": checks,
            "records": records, "issues": issues}


def _backfill_exception(root):
    directory = root / "backfill_exception"
    gateway = _Gateway(replies=[TimeoutError("offline_backfill_timeout")])
    spares = [{"code": "600001", "gap": 0.05, "price": 10.0},
              {"code": "600002", "gap": 0.04, "price": 10.0}]
    quotes = {code: {"price": 10.0, "vwap": 9.5, "ask_v": 100}
              for code in ("600001", "600002")}
    result = backfill.run_backfill_exec(
        gateway, DATE, {"date": DATE, "buys": []}, [], spares,
        {"600001": 9.0, "600002": 9.0}, quotes, positions={},
        params=_cfg(single_budget=10000.0),
        book_dir=str(directory), sleep_fn=lambda _: None, log=LOG)
    checks = {"one_sdk_only": len(gateway.places) == 1,
              "unresolved": result["placed"][0]["status"] == "unresolved",
              "reservation_preserved": result["placed"][0]["reserved_notional"] == 10000.0,
              "following_spare_blocked": result["skipped"][0]["reason"]
                                         == "budget_or_recovery_blocked",
              "issue_recorded": any(i.get("kind") == "order_result_unresolved"
                                    for i in result["budget_bound_breaches"])}
    return {"ok": all(checks.values()), "checks": checks, "result": result}


def _ack_failure(root):
    directory = root / "ack_second_write_failure"

    def _flaky_ack(book_dir, date, rec):
        order_intents.upsert_fill_record(book_dir, date, rec)
        raise OSError("offline_ack_second_write_failure")

    gateway = _Gateway(replies=[{"order_id": "ack-id", "status": "submitted",
                                 "filled_qty": 0}])
    with mock.patch.object(order_intents, "acknowledge", side_effect=_flaky_ack):
        records, issues = _execute(gateway, directory, [_plan("600001"), _plan("600002")])
    saved = order_intents.load(str(directory), DATE, strict=True)[0]
    restarted = _Gateway(poll_replies={"ack-id": TimeoutError("offline_recovery_poll")})
    records2, issues2 = _execute(restarted, directory, [_plan("600003")])
    saved2 = order_intents.load(str(directory), DATE, strict=True)[0]
    checks = {"unresolved": records[0]["status"] == "unresolved",
              "following_buy_blocked": records[1]["defer_reason"] == "budget_or_recovery_blocked",
              "ack_issue": any(i.get("kind") == "order_intent_ack_persist_failed" for i in issues),
              "order_id_first_write_survived": saved["order_id"] == "ack-id",
              "reservation_preserved": saved["reserved_notional"] == 10000.0,
              "restart_zero_sdk": not restarted.places,
              "restart_poll_issue": any(i.get("kind") == "recovery_order_unresolved"
                                        for i in issues2),
              "restart_reservation_preserved": saved2["reserved_notional"] == 10000.0}
    return {"ok": all(checks.values()), "checks": checks, "records": records,
            "issues": issues, "restart_records": records2, "restart_issues": issues2}


def _recovery_write_failure(root):
    directory = root / "recovery_write_failure"
    order_intents.upsert_fill_record(str(directory), DATE, _record(), creating=True)
    gateway = _Gateway(poll_replies={"recover-id": {
        "order_id": "recover-id", "status": "cancelled", "raw_status": 5,
        "filled_qty": 0, "filled_price": None}})
    with mock.patch.object(order_intents, "write_all",
                           side_effect=OSError("offline_recovery_write_failure")):
        records, issues = _execute(gateway, directory, [_plan("600002")])
    saved = order_intents.load(str(directory), DATE, strict=True)[0]
    checks = {"zero_new_sdk": not gateway.places,
              "following_buy_blocked": records[0]["defer_reason"] == "budget_or_recovery_blocked",
              "write_issue": any(i.get("kind") == "order_intent_recovery_persist_failed"
                                 for i in issues),
              "disk_not_released": saved["status"] == "submitted"
                                   and saved["qty_remaining"] == 1000
                                   and saved["reserved_notional"] == 10000.0}
    return {"ok": all(checks.values()), "checks": checks,
            "records": records, "issues": issues, "disk_intent": saved}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(
        ROOT / "test_evidence" / "w45_order_budget" / "unresolved"))
    args = parser.parse_args()
    root = Path(args.output_dir)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    place_cases = _place_exceptions(root)
    scenarios = {
        "place_exceptions": place_cases,
        "poll_exception": _poll_exception(root),
        "backfill_exception": _backfill_exception(root),
        "ack_second_write_failure": _ack_failure(root),
        "recovery_writeback_failure": _recovery_write_failure(root),
    }
    ok = all(row["ok"] for row in place_cases) and all(
        value["ok"] for key, value in scenarios.items() if key != "place_exceptions")
    result = {"ok": ok, "scenarios": scenarios,
              "production_access": "none; offline gateway stubs and evidence ledgers only"}
    with open(root / "unresolved_evidence.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("RESULT " + json.dumps({"ok": ok, "scenario_count": len(scenarios),
                                  "evidence": str(root / "unresolved_evidence.json")},
                                 ensure_ascii=False))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
