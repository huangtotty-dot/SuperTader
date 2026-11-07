# -*- coding: utf-8 -*-
"""W45 三日原始 order_terminal 事件离线回放。

只读生产 events 指定行；在输出目录重新构造终态前 fills/positions/day_state，
再喂入原始事件。绝不复制生产已 cancelled fills 冒充重放，也不连接交易终端。
"""
import argparse
import copy
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(os.environ.get("SMOKE_ROOT") or Path(__file__).resolve().parents[2])
AUTO = ROOT / "execution" / "auto"
for path in (str(ROOT), str(AUTO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from oversold_gap_confirm import backfill, executor, fillbackfill, order_intents  # noqa: E402

CASES = (
    ("2026-11-02", "events_20261102.jsonl", 13, "600779"),
    ("2026-11-03", "events_20261103.jsonl", 11, "605388"),
    ("2026-11-04", "events_20261104.jsonl", 6, "603598"),
)


def _dump(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _source_event(filename, line_no, code):
    path = ROOT / "t_io" / "bridge" / filename
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    event = json.loads(lines[line_no - 1])
    if event.get("event") not in ("order_terminal", "reject"):
        raise AssertionError("指定行不是终态事件: %s:%s" % (path, line_no))
    if event.get("code") != code or int(event.get("raw_status")) != 5:
        raise AssertionError("原始事件身份/状态变化: %s:%s %r" % (path, line_no, event))
    return path, event


def _replay_day(output_root, date, filename, line_no, code):
    source_path, raw = _source_event(filename, line_no, code)
    day_dir = output_root / date
    # 持久证据目录可重复运行：旧的幂等日志不得污染下一轮“终态前”现场。
    if day_dir.exists():
        shutil.rmtree(day_dir)
    day_dir.mkdir(parents=True, exist_ok=True)
    qty = int(raw["qty"])
    order_id = str(raw["order_id"])
    reference = 10.0
    rec = {
        "code": code, "symbol": executor.gm_symbol_of(code), "side": "BUY",
        "intent_id": "offline-%s-%s" % (date, code), "gap": 0.05,
        "price": reference, "execution_limit_price": reference,
        "execution_ceiling_price": reference, "budget_price_kind": "reference_limit",
        "qty_target": qty, "qty_placed": qty, "status": "submitted",
        "filled_qty": 0, "filled_price": None, "filled_notional": 0.0,
        "reserved_notional": qty * reference, "fee_est": 0.0,
        "order_id": order_id, "qty_remaining": qty, "booked_qty": qty,
        "message": "", "defer_reason": None,
    }
    fills = {"date": date, "mode": "offline_replay", "picks": [{"code": code}],
             "spares": [], "buys": [rec], "summary": executor._buys_summary([rec])}
    sentinel_closed = {"sentinel": {"qty": 7, "price": 1.23, "keep": "unchanged"}}
    book = {"updated_at": "", "open": {code: {
        "entry_date": date, "entry_price": reference, "qty": qty,
        "board": "离线回放", "days_held": 0}}, "closed": copy.deepcopy(sentinel_closed)}
    state = {"date": date, "buy_done": True,
             "pending": backfill.pending_from_fills(fills),
             "sentinel": {"keep": "unchanged"}}
    _dump(day_dir / ("fills_%s.json" % date), fills)
    _dump(day_dir / ("exec_report_%s.json" % date),
          {"date": date, "summary": copy.deepcopy(fills["summary"])})
    _dump(day_dir / "positions.json", book)
    _dump(day_dir / ("day_state_%s.json" % date), state)
    order_intents.upsert_fill_record(str(day_dir), date, rec, creating=True)
    before = {"fills": copy.deepcopy(fills), "positions": copy.deepcopy(book),
              "day_state": copy.deepcopy(state)}
    _dump(day_dir / "preterminal_snapshot.json", before)

    event = {"time": raw["time"], "code": raw["code"], "side": raw["side"],
             "qty": raw["qty"], "order_id": raw["order_id"],
             "status": raw["raw_status"], "rej_detail": raw.get("detail") or ""}
    first = fillbackfill.apply_reject(str(day_dir), event, log=lambda *a: None)
    final_fills = _load(day_dir / ("fills_%s.json" % date))
    final_book = _load(day_dir / "positions.json")
    final_state = _load(day_dir / ("day_state_%s.json" % date))
    final_report = _load(day_dir / ("exec_report_%s.json" % date))
    final_intents = order_intents.load(str(day_dir), date, strict=True)
    out = final_fills["buys"][0]
    checks = {
        "action_is_cancelled_removal": first.get("action") == "buy_cancelled_removed",
        "raw_event_preserved": raw["code"] == code and raw["raw_status"] == 5,
        "fills_cancelled": out.get("status") == "cancelled",
        "remaining_released": out.get("qty_remaining") == 0 and out.get("reserved_notional") == 0.0,
        "terminal_columns": final_fills["summary"]["terminal_counts"] == {
            "rejected": 0, "cancelled": 1, "expired": 0, "error": 0},
        "pending_cleared": final_state["pending"] == [] and backfill.pending_from_fills(final_fills) == [],
        "position_removed": code not in final_book["open"],
        "closed_sentinel_unchanged": final_book["closed"] == sentinel_closed,
        "day_state_sentinel_unchanged": final_state["sentinel"] == {"keep": "unchanged"},
        "exec_report_synchronized": final_report["summary"] == final_fills["summary"],
        "intent_terminal_synchronized": (len(final_intents) == 1
                                         and final_intents[0]["side"] == "BUY"
                                         and final_intents[0]["status"] == "cancelled"
                                         and final_intents[0]["order_id"] == order_id),
    }
    state_names = ["positions.json", "fills_%s.json" % date,
                   "exec_report_%s.json" % date, "day_state_%s.json" % date,
                   "order_intents_%s.json" % date]
    snapshot = {name: (day_dir / name).read_bytes() for name in state_names}
    replay = fillbackfill.apply_reject(str(day_dir), event, log=lambda *a: None)
    checks["idempotent"] = (replay.get("action") == "skipped"
                            and replay.get("reason") == "duplicate_key"
                            and snapshot == {name: (day_dir / name).read_bytes()
                                             for name in state_names})
    result = {"date": date, "source": "%s:%s" % (source_path, line_no),
              "raw_event": raw, "first_action": first, "replay_action": replay,
              "checks": checks, "ok": all(checks.values()),
              "final_record": out, "final_summary": final_fills["summary"]}
    _dump(day_dir / "terminal_replay_result.json", result)
    if not result["ok"]:
        raise AssertionError("三日终态回放检查失败: %s %r" % (date, checks))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(
        ROOT / "test_evidence" / "w45_order_budget" / "three_day_terminal"))
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    days = [_replay_day(output, *case) for case in CASES]
    result = {"ok": all(day["ok"] for day in days), "days": days,
              "production_events_access": "read_only",
              "preterminal_state": "constructed_offline_not_copied_from_cancelled_fills"}
    _dump(output / "three_day_terminal_evidence.json", result)
    print("RESULT " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
