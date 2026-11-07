# -*- coding: utf-8 -*-
"""离线验证 SELL raw5/raw12 终态累计成交对账、精确留痕与完整幂等。

不 init、不连接终端、不下单；事件桥、OSC 账本与审计全部使用临时目录。
已有成交场景先走真实 apply_fill，不用预填 filled_qty 绕开累计差额路径。
"""
import argparse
import atexit
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("SMOKE_ROOT") or Path(__file__).resolve().parents[2])
TMP = Path(tempfile.mkdtemp(prefix="gm_sell_terminal_rollback_"))
os.environ["GM_BRIDGE_DIR"] = str(TMP / "bridge")
os.environ["SUPERTRADER_ROOT"] = str(ROOT)

gm_api = types.ModuleType("gm.api")
for key, value in {
    "MODE_LIVE": 1, "MODE_BACKTEST": 2,
    "ADJUST_PREV": 1, "ADJUST_NONE": 0,
    "OrderSide_Buy": 1, "OrderSide_Sell": 2,
    "OrderType_Market": 1, "OrderType_Limit": 2,
    "PositionEffect_Open": 1, "PositionEffect_Close": 2,
    "PositionSide_Long": 1,
}.items():
    setattr(gm_api, key, value)


def _no_sdk(*args, **kwargs):
    raise AssertionError("本回放不得触发任何真实 SDK 调用")


for name in ("order_volume", "get_instruments", "run"):
    setattr(gm_api, name, _no_sdk)
gm_api.subscribe = lambda **kwargs: None
gm_api.history_n = lambda **kwargs: []
gm_api.history = lambda **kwargs: []
gm_api.current = lambda *args, **kwargs: []
gm_api.get_orders = lambda *args, **kwargs: []
sys.modules["gm.api"] = gm_api
sys.path.insert(0, str(ROOT / "execution" / "auto"))

from oversold_gap_confirm import order_intents  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "gm_main_sell_terminal_rollback", str(ROOT / "execution" / "auto" / "gm_main.py"))
GM = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = GM
spec.loader.exec_module(GM)
try:
    atexit.unregister(GM._gm_atexit_banner)
except Exception:
    pass
GM._AUDIT_LOG_PATH = str(TMP / "backtrace.jsonl")
GM._AUDIT_MIRROR_PATH = str(TMP / "auto_backtrace.jsonl")
FB = GM._osc_fillbackfill

DATE = "2026-11-03"
NOW = datetime(2026, 11, 3, 14, 50, 0)
VWAP = 10.123456789012345
FEE = 7.891234567890123
PRIOR_VWAP = 9.987654321098765
PRIOR_FEE = 1.234567890123456
ctx = types.SimpleNamespace(
    mode=GM.MODE_LIVE, now=NOW,
    executed_orders={}, manual_position={}, _fills_done=set(),
    rejected_order_count=0, _pending_recon={},
)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _snapshot(symbol):
    files = {}
    for path in sorted(TMP.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(TMP))] = path.read_bytes()
    return {"manual_position": copy.deepcopy(ctx.manual_position[symbol]), "files": files}


rows = []
for raw, code, scenario in (
        (5, "600001", "submitted_zero"),
        (5, "600003", "prior_fill_200"),
        (12, "600002", "submitted_zero"),
        (12, "600004", "prior_fill_200")):
    symbol = "SHSE." + code
    order_id = "sell-partial-%s-%s" % (raw, scenario)
    book_dir = TMP / ("book_%s" % code)
    book_dir.mkdir(parents=True, exist_ok=True)
    GM._OSC_BOOK_DIR = str(book_dir)
    rec = {
        "code": code, "symbol": symbol, "side": "SELL", "order_id": order_id,
        "reason": "pop_sell", "price": 10.0, "qty_placed": 1000,
        "status": "submitted", "filled_qty": 0, "filled_price": None,
        "qty_remaining": 1000, "fee_est": 0.0, "message": "",
        "intent_id": "intent-%s" % order_id,
    }
    sells_path = book_dir / ("sells_close_%s.json" % DATE)
    _write_json(sells_path, {"date": DATE, "sells": [rec], "summary": {}})
    _write_json(book_dir / "positions.json", {
        "updated_at": "", "closed": {},
        "open": {code: {"entry_date": "2026-11-02", "entry_price": 8.0, "qty": 1000}},
    })
    order_intents.upsert_fill_record(str(book_dir), DATE, rec, creating=True)
    ctx.manual_position[symbol] = {"qty": 0, "available": 0, "t_qty": 0}

    prior_action = None
    if scenario == "prior_fill_200":
        # 先走真实累计成交链：0→200，OSC 1000→800；终态再只补 200→400 的增量。
        prior = FB.apply_fill(str(book_dir), {
            "time": DATE + " 14:49:50", "code": code, "side": "SELL",
            "qty": 200, "fill_vwap": PRIOR_VWAP, "fee": PRIOR_FEE,
            "order_id": order_id}, log=lambda *_: None)
        prior_action = prior.get("action")
    osc_before = int(_read_json(book_dir / "positions.json")["open"][code]["qty"])

    order = {
        "symbol": symbol, "status": raw, "side": 2,
        "volume": 1000, "filled_volume": 400, "filled_vwap": VWAP,
        "filled_commission": FEE, "id": order_id, "price": 10.0,
        "ord_rej_reason_detail": "",
    }
    GM.on_order_status(ctx, order)
    terminal_event = {
        "time": str(NOW), "code": code, "side": "SELL", "qty": 1000,
        "order_id": order_id, "filled_qty": 400, "filled_vwap": VWAP,
        "fee": FEE, "status": raw, "rej_detail": "",
    }
    before_host_replay = _snapshot(symbol)
    GM.on_order_status(ctx, dict(order))
    host_replay_idempotent = before_host_replay == _snapshot(symbol)
    before_reject_replay = _snapshot(symbol)
    reject_replay = FB.apply_reject(str(book_dir), terminal_event, log=lambda *_: None)
    reject_replay_idempotent = before_reject_replay == _snapshot(symbol)

    position = dict(ctx.manual_position[symbol])
    osc_after = int(_read_json(book_dir / "positions.json")["open"][code]["qty"])
    sell = _read_json(sells_path)["sells"][0]
    intent = next(r for r in order_intents.load(str(book_dir), DATE, strict=True)
                  if r.get("intent_id") == rec["intent_id"])
    rows.append({
        "raw_status": raw, "scenario": scenario, "code": code,
        "volume": 1000, "filled_volume": 400, "position": position,
        "prior_fill_action": prior_action, "osc_qty_before": osc_before,
        "osc_qty_after": osc_after, "osc_delta": osc_before - osc_after,
        "sell": sell, "intent": intent,
        "host_replay_idempotent": host_replay_idempotent,
        "reject_replay_action": reject_replay.get("action"),
        "reject_replay_reason": reject_replay.get("reason"),
        "reject_replay_idempotent": reject_replay_idempotent,
    })

GM._audit_close()
rollbacks = []
for path in (TMP / "backtrace.jsonl", TMP / "auto_backtrace.jsonl"):
    if not path.exists():
        continue
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                audit = json.loads(line)
            except Exception:
                continue
            if audit.get("event") == "sell_rollback":
                rollbacks.append(audit)

expected_status = {5: "cancelled", 12: "expired"}
checks = {
    "raw5_raw12_both_paths": {(r["raw_status"], r["scenario"]) for r in rows} == {
        (5, "submitted_zero"), (5, "prior_fill_200"),
        (12, "submitted_zero"), (12, "prior_fill_200")},
    "host_only_unfilled_600": all(
        r["position"] == {"qty": 600, "available": 600, "t_qty": 600} for r in rows),
    "osc_only_real_delta": all(
        r["osc_qty_after"] == 600 and r["osc_delta"] ==
        (400 if r["scenario"] == "submitted_zero" else 200) for r in rows),
    "prior_path_used_real_apply_fill": all(
        r["prior_fill_action"] == "sell_reduced" and r["osc_qty_before"] == 800
        for r in rows if r["scenario"] == "prior_fill_200"),
    "terminal_qty_and_status": all(
        r["sell"]["status"] == expected_status[r["raw_status"]] and
        r["sell"]["filled_qty"] == 400 and r["sell"]["qty_remaining"] == 0
        for r in rows),
    "fills_exact_vwap_fee": all(
        r["sell"]["filled_price"] == VWAP and r["sell"]["fee_actual"] == FEE for r in rows),
    "intents_exactly_match_fills": all(
        r["intent"]["status"] == r["sell"]["status"] and
        r["intent"]["filled_qty"] == 400 and r["intent"]["qty_remaining"] == 0 and
        r["intent"]["filled_price"] == VWAP and r["intent"]["fee_actual"] == FEE
        for r in rows),
    "host_replay_fully_idempotent": all(r["host_replay_idempotent"] for r in rows),
    "reject_replay_fully_idempotent": all(
        r["reject_replay_idempotent"] and r["reject_replay_action"] == "skipped" and
        r["reject_replay_reason"] == "duplicate_key" for r in rows),
    "rollback_audit_uses_600": len(rollbacks) == len(rows) * 2 and all(
        int(r.get("qty") or 0) == 600 for r in rollbacks),
}
result = {"ok": all(checks.values()), "checks": checks, "cases": rows,
          "expected": {"filled_vwap": VWAP, "filled_commission": FEE},
          "rollback_audits": rollbacks, "production_access": "none; offline stubs only"}

parser = argparse.ArgumentParser()
parser.add_argument("--output-dir", default=str(
    ROOT / "test_evidence" / "w45_order_budget" / "sell_terminal_rollback"))
args = parser.parse_args()
out_dir = Path(args.output_dir)
if out_dir.exists():
    shutil.rmtree(out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
with open(out_dir / "sell_terminal_rollback_evidence.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("RESULT " + json.dumps(result, ensure_ascii=False))
shutil.rmtree(TMP, ignore_errors=True)
if not result["ok"]:
    raise SystemExit(1)
