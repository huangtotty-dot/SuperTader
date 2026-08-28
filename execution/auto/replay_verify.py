# coding=utf-8
"""
execution/auto/replay_verify.py — 迁移后 auto 执行层回放校验（P4-1 验收入口）

用途：用同一历史日/窗口回放 execution/auto/gm_main.py（迁入后的 goldminer 执行层），
输出事件桥（events.jsonl/backtrace.jsonl），供与 goldminer 原版 main.py 回放对比
「P0-P6 各卖出通道触发时点一致」。

用法（需掘金终端运行，gm SDK 回测模式）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe execution/auto/replay_verify.py [scen]

scen: fix2 / fix3 / fix4（对齐 goldminer replay_wp9 三场景）；缺省 fix3。
产物: <superTrader>/t_io/validation/auto/replay/<scen>/events.jsonl + backtrace.jsonl
"""
import os
import sys

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTO = os.path.join(_ST, "execution", "auto")
for _p in (_ST, _AUTO, os.path.join(_AUTO, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCEN = sys.argv[1] if len(sys.argv) > 1 else "fix3"
assert SCEN in ("fix2", "fix3", "fix4"), f"未知场景 {SCEN}"

OUT_DIR = os.path.join(_ST, "t_io", "validation", "auto", "replay", SCEN)
os.makedirs(OUT_DIR, exist_ok=True)

# 事件桥重定向到校验目录（不污染 t_io/bridge 生产桥）
import gm_bridge.writer as writer
writer.BRIDGE_DIR = OUT_DIR

import gm_main  # noqa: E402
from gm.api import run, MODE_BACKTEST, ADJUST_PREV  # noqa: E402
from utils.gm_token import load_token  # noqa: E402

if SCEN in ("fix2", "fix3"):
    gm_main.STOCKS = {"000988": "SZSE.000988"}
    gm_main.STOCK_NAMES = {"000988": "华工科技"}
    gm_main.MIRROR_HOLDINGS = {"000988": {"qty": 800, "cost": 0}}
    gm_main.INITIAL_CASH = 200000
    CASH = 200000
    START = "2026-04-26 08:00:00" if SCEN == "fix2" else "2026-04-24 08:00:00"
else:
    CASH = 150000
    gm_main.INITIAL_CASH = 150000
    START = "2026-04-27 08:00:00"
END = "2026-07-24 16:00:00"

gm_main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")
# 迁移日 sell_state 全新初始化语义：校验目录下独立，不触碰生产 auto_sell_state.json
import sell_state  # noqa: E402
sell_state.SELL_STATE_PATH = os.path.join(OUT_DIR, "sell_state.json")

print(f"[replay_verify] 场景={SCEN} 窗口={START}~{END} 资金={CASH} 标的={list(gm_main.STOCKS)}")
print(f"[replay_verify] 产物目录={OUT_DIR}")
print(f"[replay_verify] 提示：同窗口用 goldminer 原版 replay_wp9.py 跑一次，对比 events.jsonl 中 "
      f"order/fill 的通道触发时点（P0 MA5/P1 PANIC/P2 TRAIL/P3 TREND_EXIT/P4 TARGET/P5 SELL_HIGH/P6 TAIL）。")

run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
    filename="gm_main.py",
    mode=MODE_BACKTEST,
    token=load_token(),
    backtest_start_time=START,
    backtest_end_time=END,
    backtest_initial_cash=CASH,
    backtest_commission_ratio=0.00015,
    backtest_slippage_ratio=0.0001,
    backtest_adjust=ADJUST_PREV,
    backtest_match_mode=1)
