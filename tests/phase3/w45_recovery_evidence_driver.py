# -*- coding: utf-8 -*-
"""W45 SDK 前原子意图与崩溃恢复持久证据驱动（全离线、输出目录可指定）。"""
import argparse
import copy
import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(os.environ.get("SMOKE_ROOT") or Path(__file__).resolve().parents[2])
AUTO = ROOT / "execution" / "auto"
for path in (str(ROOT), str(AUTO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from oversold_gap_confirm import backfill, executor, order_intents  # noqa: E402

DATE = "2026-11-03"


def _dump(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _plan(code):
    return {"code": code, "gap": 0.05, "price": 10.0, "qty_target": 1000,
            "qty_place": 1000, "truncated": False, "defer_reason": None}


def _params(**overrides):
    out = dict(executor.DEFAULTS, daily_budget=200000.0, single_budget=100000.0,
               cash_headroom=0.95, min_lot=100, order_poll_rounds=0,
               order_poll_sleep_sec=0.0)
    out.update(overrides)
    return out


def _record(code="600001", order_id=None):
    return {"code": code, "symbol": executor.gm_symbol_of(code), "side": "BUY",
            "gap": 0.05, "price": 10.0, "execution_limit_price": 10.0,
            "execution_ceiling_price": 10.0, "budget_price_kind": "reference_limit",
            "qty_target": 1000, "qty_placed": 1000, "status": "submitted",
            "filled_qty": 0, "filled_price": None, "filled_notional": 0.0,
            "reserved_notional": 10000.0, "fee_est": 0.0,
            "order_id": order_id, "qty_remaining": 1000, "booked_qty": 1000,
            "message": "", "defer_reason": None}


class _Gateway:
    def __init__(self, poll_reply=None):
        self.poll_reply = poll_reply
        self.calls = []

    def get_cash(self):
        return 1000000.0

    def get_positions(self):
        return {}

    def place_order(self, code, side, qty, price, execution_limit_price=None):
        self.calls.append({"kind": "place", "code": code, "qty": qty,
                           "execution_limit_price": execution_limit_price})
        return {"order_id": "new-sdk-id", "status": "submitted",
                "filled_qty": 0, "filled_price": None}

    def poll_order(self, order_id, code):
        self.calls.append({"kind": "poll", "code": code, "order_id": order_id})
        return copy.deepcopy(self.poll_reply or {
            "order_id": order_id, "status": "submitted", "raw_status": 1,
            "filled_qty": 0, "filled_price": None})

    def cancel_order(self, order_id, code):
        self.calls.append({"kind": "cancel", "code": code, "order_id": order_id})
        return {"ok": True, "order_id": order_id}


def _reserve_failure(root):
    directory = root / "reserve_write_failure"
    gateway = _Gateway()
    view = {"date": DATE, "buys": []}
    with mock.patch.object(order_intents, "upsert_fill_record",
                           side_effect=OSError("offline_injected_reserve_write_failure")):
        records, issues = executor._execute_buy_plan_with_budget(
            gateway, [_plan("600001"), _plan("600002")], {}, view,
            100000.0, 200000.0, 100000.0, _params(), book_dir=str(directory),
            date=DATE, sleep_fn=lambda _: None, log=lambda *a: None)
    checks = {"zero_sdk": not gateway.calls,
              "first_is_persist_error": records[0]["defer_reason"] == "order_intent_persist_failed",
              "following_blocked": records[1]["defer_reason"] == "budget_or_recovery_blocked",
              "zero_quantity_sent": all(r["qty_placed"] == 0 for r in records)}
    return {"checks": checks, "ok": all(checks.values()), "records": records,
            "issues": issues, "gateway_calls": gateway.calls}


def _missing_id(root, mode):
    directory = root / (mode + "_missing_order_id")
    # 每轮只注入一个崩溃现场，复跑时不得累积上一轮 intents。
    if directory.exists():
        shutil.rmtree(directory)
    rec = _record()
    rec.pop("order_id", None)
    order_intents.upsert_fill_record(str(directory), DATE, rec, creating=True)
    gateway = _Gateway()
    if mode == "first_round":
        view = {"date": DATE, "buys": []}
        records, issues = executor._execute_buy_plan_with_budget(
            gateway, [_plan("600002")], {}, view, 100000.0,
            200000.0, 100000.0, _params(), book_dir=str(directory), date=DATE,
            sleep_fn=lambda _: None, log=lambda *a: None)
        blocked = records[0]["defer_reason"] == "budget_or_recovery_blocked"
        output = {"records": records, "issues": issues}
    else:
        result = backfill.run_backfill_exec(
            gateway, DATE, {"date": DATE, "buys": []}, [],
            [{"code": "600002", "gap": 0.04, "price": 10.0}],
            {"600002": 9.0}, {"600002": {"price": 10.0, "vwap": 9.5, "ask_v": 100}},
            positions={}, params=_params(), book_dir=str(directory),
            sleep_fn=lambda _: None, log=lambda *a: None)
        issues = result["budget_bound_breaches"]
        blocked = not result["placed"] and result["note"] == "budget_bound_breach"
        output = {"result": result}
    checks = {"zero_sdk": not [c for c in gateway.calls if c["kind"] == "place"],
              "no_guessed_poll": not [c for c in gateway.calls if c["kind"] == "poll"],
              "missing_id_reported": any(i.get("kind") == "recovery_order_id_missing"
                                         for i in issues),
              "new_buy_blocked": blocked}
    return {"checks": checks, "ok": all(checks.values()),
            "gateway_calls": gateway.calls, **output}


def _known_id(root):
    directory = root / "known_order_id_recovery"
    if directory.exists():
        shutil.rmtree(directory)
    order_intents.upsert_fill_record(str(directory), DATE, _record(order_id="known-id"),
                                     creating=True)
    gateway = _Gateway({"order_id": "known-id", "status": "cancelled", "raw_status": 5,
                        "filled_qty": 0, "filled_price": None})
    view = {"date": DATE, "buys": []}
    records, issues = executor._execute_buy_plan_with_budget(
        gateway, [_plan("600002")], {}, view, 100000.0,
        200000.0, 100000.0, _params(), book_dir=str(directory), date=DATE,
        sleep_fn=lambda _: None, log=lambda *a: None)
    checks = {"poll_precedes_new_sdk": [c["kind"] for c in gateway.calls] == ["poll", "place"],
              "fills_file_absent": not (directory / ("fills_%s.json" % DATE)).exists(),
              "old_intent_merged": (view["buys"][0]["order_id"] == "known-id"
                                    and view["buys"][0]["status"] == "cancelled"),
              "new_order_after_closed_recovery": records[0]["qty_placed"] == 1000,
              "no_recovery_issue": issues == []}
    return {"checks": checks, "ok": all(checks.values()), "gateway_calls": gateway.calls,
            "recovered_view": view, "records": records, "issues": issues}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(
        ROOT / "test_evidence" / "w45_order_budget" / "recovery"))
    args = parser.parse_args()
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    result = {"reserve_write_failure": _reserve_failure(root),
              "first_round_missing_order_id": _missing_id(root, "first_round"),
              "backfill_missing_order_id": _missing_id(root, "backfill"),
              "known_order_id_recovery": _known_id(root)}
    result["ok"] = all(value["ok"] for value in result.values())
    result["production_access"] = "none; temporary offline ledgers only"
    _dump(root / "recovery_evidence.json", result)
    print("RESULT " + json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
