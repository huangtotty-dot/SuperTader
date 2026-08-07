# -*- coding: utf-8 -*-
"""
test_daily_pnl_push.py — 收益汇总推送 NameError 修复回归（W32-B1）
背景：2026-08-05~07 连续 3 天 14:59 推送被容错吞掉：
  "收益汇总推送异常（已吞掉，不影响主循环）: name 'total_day_pnl' is not defined"
修复：main.py _maybe_push_daily_pnl_summary 内 2 处 total_day_pnl → total_day_float。
本测试：从 main.py 用 AST 抽取该函数源码，以桩全局环境 exec 后完整走一遍推送路径
（卡片构建 + send_feishu_payload 捕获 + daily_pnl.jsonl 写入 + pre_close 收盘更新），
断言不再 NameError 且产物字段正确。
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

BASE = Path(r"E:\06_T")
MAIN_PY = BASE / "main.py"


def extract_function_source(path: Path, func_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node)
    raise AssertionError(f"function {func_name} not found in {path}")


def main() -> int:
    src = extract_function_source(MAIN_PY, "_maybe_push_daily_pnl_summary")

    captured = {}

    def fake_send_feishu_payload(payload, success_log="", error_prefix=""):
        captured["payload"] = payload
        captured["success_log"] = success_log
        return True

    tmp = Path(tempfile.mkdtemp(prefix="w32_pnl_test_"))
    holdings_file = tmp / "holdings.json"
    holdings_file.write_text(json.dumps({
        "600176": {"name": "中国巨石", "qty": 300, "cost": 55.744, "pre_close": 11.30},
        "588170": {"name": "科创芯片ETF", "qty": 6000, "cost": 0.950, "pre_close": 0.918},
    }, ensure_ascii=False), encoding="utf-8")

    ns = {
        "datetime": datetime,
        "dtime": dtime,
        "log": logging.getLogger("w32_pnl_test"),
        "_os": os,
        "_json": json,
        "BASE_DIR": str(tmp),
        "HOLDINGS_FILE": str(holdings_file),
        "HOLDINGS": {
            "600176": {"name": "中国巨石", "qty": 300, "cost": 55.744, "pre_close": 11.30},
            "588170": {"name": "科创芯片ETF", "qty": 6000, "cost": 0.950, "pre_close": 0.918},
        },
        "DAILY_DECISION_STATS": {
            "600176": {"last_price": 12.05},
            "588170": {"last_price": 0.941},
        },
        "VIRTUAL_TRADES": {
            "600176": {"SELL_HIGH": [{"qty": 100, "price": 12.10}],
                       "BUY_LOW": [{"qty": 100, "price": 11.90}]},
        },
        "PARAMS": {"commission_rate": 0.00015},
        "send_feishu_payload": fake_send_feishu_payload,
        "_daily_pnl_push_date": "",
    }
    exec(compile(src, str(MAIN_PY), "exec"), ns)

    # 1) 14:59 窗口内调用 — 修复前此处 NameError 被外层 try 吞掉、推送缺失
    ns["_maybe_push_daily_pnl_summary"](datetime(2026, 8, 7, 14, 59, 30))

    assert "payload" in captured, "推送未触发（send_feishu_payload 未被调用）"
    card = captured["payload"]
    header = card["card"]["header"]
    assert header["template"] in ("red", "green"), f"模板色异常: {header['template']}"
    # 600176: (12.05-11.30)*300=+225; 588170: (0.941-0.918)*6000=+138 → 浮动 +363 > 0 → green
    assert header["template"] == "green", f"浮动 +363 应为 green，实际 {header['template']}"
    md = card["card"]["elements"][0]["content"]
    assert "当日收益汇总" in md and "今日总收益" in md, "卡片内容不完整"

    # 2) JSONL 日志已写且字段正确（修复前该路径同样被 NameError 阻断）
    pnl_log = tmp / "t_io" / "logs" / "daily_pnl.jsonl"
    assert pnl_log.exists(), "daily_pnl.jsonl 未写入"
    rec = json.loads(pnl_log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert abs(rec["day_pnl_float"] - 363.0) < 0.01, f"day_pnl_float={rec['day_pnl_float']} != 363.0"
    assert rec["t0_realized"] != 0 or rec["stocks"], "记录字段缺失"
    # T0: 配对100股 (12.10-11.90)*100 - fee
    assert rec["t0_realized"] > 0, f"T0实盈应为正: {rec['t0_realized']}"

    # 3) 每日一次防重复
    ns["_maybe_push_daily_pnl_summary"](datetime(2026, 8, 7, 14, 59, 55))
    assert len(pnl_log.read_text(encoding="utf-8").strip().splitlines()) == 1, "防重复失效"

    # 4) 窗口外不推送
    ns["_daily_pnl_push_date"] = ""
    captured.clear()
    ns["_maybe_push_daily_pnl_summary"](datetime(2026, 8, 7, 15, 30, 0))
    assert "payload" not in captured, "窗口外误推送"

    # 5) pre_close 收盘更新块执行（holdings.json 被更新为最新价）
    h_after = json.loads(holdings_file.read_text(encoding="utf-8"))
    assert abs(h_after["600176"]["pre_close"] - 12.05) < 1e-9, "pre_close 未更新"

    print("PASS: 收益汇总推送路径全绿（卡片/JSONL/防重复/窗口外/pre_close 更新）")
    print(f"  header={header['template']} day_pnl_float={rec['day_pnl_float']} t0_realized={rec['t0_realized']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
