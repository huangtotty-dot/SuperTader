# -*- coding: utf-8 -*-
"""test_daily_pnl_record.py（原 test_daily_pnl_push.py）— 2026-09-11 起：
14:59「收益汇总」飞书推送已按 owner 裁决删除（无效信息），本测试改为验证保留行为：
  ① 到点写 daily_pnl.jsonl（字段正确、每日一次去重、窗口外不写）
  ② 收盘更新 holdings.json 的 pre_close
  ③ **不调用 send_feishu_payload**（推送确已移除）
"""
import ast
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]      # 仓库根
MAIN_PY = BASE / "main.py"


def extract_function_source(path: Path, func_name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"function {func_name} not found in {path}")


def main() -> int:
    src = extract_function_source(MAIN_PY, "_maybe_record_daily_pnl")
    captured = {}

    def fake_send_feishu_payload(payload, success_log="", error_prefix=""):
        captured["payload"] = payload
        return True

    tmp = Path(tempfile.mkdtemp(prefix="pnl_record_test_"))
    holdings_file = tmp / "holdings.json"
    holdings_file.write_text(json.dumps({
        "600176": {"name": "中国巨石", "qty": 300, "cost": 55.744, "pre_close": 11.30},
        "588170": {"name": "科创芯片ETF", "qty": 6000, "cost": 0.950, "pre_close": 0.918},
    }, ensure_ascii=False), encoding="utf-8")

    ns = {
        "datetime": datetime, "dtime": dtime,
        "log": logging.getLogger("pnl_record_test"),
        "_os": os, "_json": json,
        "BASE_DIR": str(tmp), "HOLDINGS_FILE": str(holdings_file),
        "HOLDINGS": {
            "600176": {"name": "中国巨石", "qty": 300, "cost": 55.744, "pre_close": 11.30},
            "588170": {"name": "科创芯片ETF", "qty": 6000, "cost": 0.950, "pre_close": 0.918},
        },
        "DAILY_DECISION_STATS": {"600176": {"last_price": 12.05}, "588170": {"last_price": 0.941}},
        "VIRTUAL_TRADES": {"600176": {"SELL_HIGH": [{"qty": 100, "price": 12.10}],
                                      "BUY_LOW": [{"qty": 100, "price": 11.90}]}},
        "PARAMS": {"commission_rate": 0.00015},
        "compute_t0_pnl": lambda trades, rate: {"t0_pnl": 19.9},
        "send_feishu_payload": fake_send_feishu_payload,
        "_daily_pnl_push_date": "",
    }
    # 打桩 src.holdings_repo（防测试写真实 holdings.json）
    import types
    _m = types.ModuleType("src.holdings_repo")

    def _fake_save(held, actor="", reason=""):
        holdings_file.write_text(json.dumps(held, ensure_ascii=False), encoding="utf-8")
    _m.save_held_merged = _fake_save
    sys.modules.setdefault("src", types.ModuleType("src"))
    sys.modules["src.holdings_repo"] = _m

    exec(compile(src, str(MAIN_PY), "exec"), ns)

    # 1) 14:59 触发：写 JSONL，且**不得推送**
    ns["_maybe_record_daily_pnl"](datetime(2026, 9, 11, 14, 59, 30))
    pnl_log = tmp / "t_io" / "logs" / "daily_pnl.jsonl"
    assert pnl_log.exists(), "daily_pnl.jsonl 未写入"
    rec = json.loads(pnl_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert abs(rec["day_pnl_float"] - 363.0) < 0.01, f"day_pnl_float={rec['day_pnl_float']} != 363.0"
    assert not captured, "推送已删除，不应再调用 send_feishu_payload"

    # 2) 每日一次去重
    ns["_maybe_record_daily_pnl"](datetime(2026, 9, 11, 15, 0, 10))
    assert len(pnl_log.read_text(encoding="utf-8").strip().splitlines()) == 1, "防重复失效"

    # 3) 窗口外（<14:59）不写
    ns["_daily_pnl_push_date"] = ""
    ns["_maybe_record_daily_pnl"](datetime(2026, 9, 11, 14, 0, 0))
    assert len(pnl_log.read_text(encoding="utf-8").strip().splitlines()) == 1, "窗口外误写"

    # 4) pre_close 收盘更新块执行
    h_after = json.loads(holdings_file.read_text(encoding="utf-8"))
    assert abs(h_after["600176"]["pre_close"] - 12.05) < 1e-9, "pre_close 未更新"

    print("PASS: 仅落盘+pre_close，无飞书推送 ✅")
    print(f"  day_pnl_float={rec['day_pnl_float']} t0_realized={rec['t0_realized']} pre_close={h_after['600176']['pre_close']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
