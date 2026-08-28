# -*- coding: utf-8 -*-
"""P0 打回项3: subscribe 实际收到 bar 证据（2026-08-28 审核返工）
在 run() 回调上下文（回测窗口驱动，安全无实盘副作用）订阅 watchlist 全部标的，
统计 on_bar 实际收到的唯一标的数，回答"41 只订阅是否都能收到 60s bar"。
用法: python t_io/experiment/gm_subscribe_probe.py
"""
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCHLIST = os.path.join(BASE, "t_io", "state", "watchlist_buy.json")
_GM_REPO = r"c:/Users/Lenovo/.goldminer3/projects/e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7"
if _GM_REPO not in sys.path:
    sys.path.insert(0, _GM_REPO)
from utils.gm_token import load_token  # noqa: E402

from gm.api import *  # noqa: F401,F403

RECEIVED = set()


def _watch_symbols():
    w = json.load(open(WATCHLIST, encoding="utf-8"))
    codes = [k for k in w.get("stocks", {}) if k.isdigit()]
    return [("SHSE." if k[0] in "569" else "SZSE.") + k for k in codes]


def init(context):
    symbols = _watch_symbols()
    context.probe_symbols = symbols
    context.probe_last_report = 0
    print(f"[probe] 订阅 {len(symbols)} 只 @60s ...", flush=True)
    # 对齐 goldminer 生产签名（count=240 + fields）
    subscribe(symbols=symbols, frequency="60s", count=240,
              fields="symbol,eob,open,high,low,close,volume,amount")


def on_bar(context, bars):
    for b in bars:
        RECEIVED.add(b["symbol"])
    total = len(getattr(context, "probe_symbols", []))
    # 每当计数增长就打印进度（回测模式无 on_finish）
    if len(RECEIVED) > getattr(context, "probe_last_report", 0):
        context.probe_last_report = len(RECEIVED)
        print(f"[probe] 已收到唯一标的: {len(RECEIVED)}/{total}", flush=True)
    if len(RECEIVED) >= total:
        print(f"[probe] ✅ 全部 {total} 只均收到 60s bar", flush=True)


if __name__ == "__main__":
    # gm.run() 会按模块名导入 filename（剥离 sys.path 前缀）——把本文件所在目录加入 sys.path 后传 basename
    _exp_dir = os.path.dirname(os.path.abspath(__file__))
    if _exp_dir not in sys.path:
        sys.path.insert(0, _exp_dir)
    run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
        filename="gm_subscribe_probe.py",
        mode=MODE_BACKTEST,
        token=load_token(),
        backtest_start_time="2026-08-27 09:30:00",
        backtest_end_time="2026-08-27 09:45:00",
        backtest_initial_cash=300000,
        backtest_adjust=ADJUST_PREV)
