# coding=utf-8
"""
execution/auto/backtest_holdings.py — 当前持仓半年回测（2026-08-29）

用 holdings.json 当前持仓（588170 科创芯片ETF / 600481 双良节能 / 002451 摩恩电气），
在掘金量化回测环境（MODE_BACKTEST）跑近半年（2026-02-28 ~ 2026-08-28）做T策略。

说明：
  · 588170 为 T+0 ETF，掘金做T策略按 T+1 股票机制处理（owner 已确认按股票机制回测，
    结果偏保守）；manual_position 的 type 硬编码 stock，策略自然按股票 T+1 跑。
  · MIRROR_HOLDINGS 的 cost 用持仓真实成本价，供 -8% 硬止损等判定使用。
  · 事件桥/审计日志/sell_state 重定向到 t_io/validation/auto/backtest_holdings/，
    不污染生产 t_io/bridge。

用法（需掘金终端运行）:
  python execution/auto/backtest_holdings.py

产物:
  <superTrader>/t_io/validation/auto/backtest_holdings/
    events_*.jsonl     订单/成交事件
    backtrace.jsonl    决策审计轨迹
    sell_state.json    卖出体系状态
  stdout 末尾 on_backtest_finished 打印掘金回测绩效指标（indicator）+ 手动统计。
"""
import os
import sys
import json

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTO = os.path.join(_ST, "execution", "auto")
for _p in (_ST, _AUTO, os.path.join(_AUTO, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUT_DIR = os.path.join(_ST, "t_io", "validation", "auto", "backtest_holdings")
os.makedirs(OUT_DIR, exist_ok=True)

# 事件桥重定向到校验目录（不污染 t_io/bridge 生产桥）
import gm_bridge.writer as writer  # noqa: E402
writer.BRIDGE_DIR = OUT_DIR

import gm_main  # noqa: E402
from gm.api import run, MODE_BACKTEST, ADJUST_PREV  # noqa: E402
from utils.gm_token import load_token  # noqa: E402

# ── 当前持仓（对齐 t_io/state/holdings.json；gm_symbol 对齐 config/auto_pool.py） ──
# 注：588170（科创芯片ETF）因掘金账号无 ETF 品种数据权限（ERR_NO_DATA_PERMISSION，
#     只订阅[股票]行情）无法回测，此处剔除。仅回测两只股票。
HOLDINGS = {
    "600481": {"name": "双良节能",    "gm_symbol": "SHSE.600481", "qty": 100,   "cost": 28.216},
    "002451": {"name": "摩恩电气",    "gm_symbol": "SZSE.002451", "qty": 1300,  "cost": 7.754},
}

gm_main.STOCKS = {c: v["gm_symbol"] for c, v in HOLDINGS.items()}
gm_main.STOCK_NAMES = {c: v["name"] for c, v in HOLDINGS.items()}
gm_main.MIRROR_HOLDINGS = {c: {"qty": v["qty"], "cost": v["cost"]} for c, v in HOLDINGS.items()}
gm_main.INITIAL_CASH = 50000

# 掘金账号个股历史数据上限 180 自然日（最早 2026-03-02），subscribe 预热还需往前 1 个交易日，
# 故窗口起点后移至 03-03，约 178 自然日 ≈ 近半年。
START = "2026-03-03 08:00:00"
END = "2026-08-28 16:00:00"

gm_main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")
# 卖出体系状态独立目录，不触碰生产 auto_sell_state.json
import sell_state  # noqa: E402
sell_state.SELL_STATE_PATH = os.path.join(OUT_DIR, "sell_state.json")

print(f"[backtest_holdings] 标的={list(gm_main.STOCKS)}")
print(f"[backtest_holdings] 镜像底仓={ {c: v['qty'] for c, v in gm_main.MIRROR_HOLDINGS.items()} }")
print(f"[backtest_holdings] 窗口={START} ~ {END} 资金={gm_main.INITIAL_CASH}")
print(f"[backtest_holdings] 产物目录={OUT_DIR}")

run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
    filename="gm_main.py",
    mode=MODE_BACKTEST,
    token=load_token(),
    backtest_start_time=START,
    backtest_end_time=END,
    backtest_initial_cash=gm_main.INITIAL_CASH,
    backtest_commission_ratio=0.00015,
    backtest_slippage_ratio=0.0001,
    backtest_adjust=ADJUST_PREV,
    backtest_match_mode=1)
