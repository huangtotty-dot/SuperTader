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
import argparse

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTO = os.path.join(_ST, "execution", "auto")
for _p in (_ST, _AUTO, os.path.join(_AUTO, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 命令行可复写窗口/输出目录（并行跑不同窗口时，产物需分离避免互相覆盖）
_ap = argparse.ArgumentParser(description="当前持仓回测")
_ap.add_argument("--start", default="2026-03-05 08:00:00")
_ap.add_argument("--end", default="2026-08-28 16:00:00")
_ap.add_argument("--label", default="", help="输出目录后缀；空=默认 backtest_holdings")
_ARGS = _ap.parse_args()
# 清空自定义参数：gm.api 在 import 时(getopt)与 run() 内(optparse)都会二次解析 sys.argv，
# 不认识 --start/--end/--label 会抛 "no such option"；此处先消费掉，仅保留脚本名。
sys.argv = [sys.argv[0]]

_OUT_SUB = ("backtest_holdings_" + _ARGS.label) if _ARGS.label else "backtest_holdings"
OUT_DIR = os.path.join(_ST, "t_io", "validation", "auto", _OUT_SUB)
os.makedirs(OUT_DIR, exist_ok=True)

# 事件桥重定向到校验目录（不污染 t_io/bridge 生产桥）
import gm_bridge.writer as writer  # noqa: E402
writer.BRIDGE_DIR = OUT_DIR

import gm_main  # noqa: E402
from gm.api import run, MODE_BACKTEST, ADJUST_PREV  # noqa: E402
from utils.gm_token import load_token  # noqa: E402

# ── 当前持仓（单一真源 t_io/state/holdings.json 派生仅持有 qty>0；gm_symbol/name/cost 对齐） ──
# 注：588170（科创芯片ETF）在旧终端账号无 ETF 品种数据权限（ERR_NO_DATA_PERMISSION）。
#     新终端（国盛掘金3 专业版）账号是否放开 ETF 待实测——此处保留 588170 一并试跑，
#     若 subscribe 仍报 ERR_NO_DATA_PERMISSION 则剔除重跑两只股票。
def _load_holdings_for_backtest():
    _hp = os.path.join(_ST, "t_io", "state", "holdings.json")
    with open(_hp, "r", encoding="utf-8") as f:
        _data = json.load(f)
    return {
        c: {"name": h.get("name", c), "gm_symbol": h.get("gm_symbol", ""),
            "qty": int(h.get("qty") or 0), "cost": float(h.get("cost") or 0)}
        for c, h in _data.items()
        if isinstance(h, dict) and not str(c).startswith("_") and int(h.get("qty") or 0) > 0
    }


HOLDINGS = _load_holdings_for_backtest()

gm_main.STOCKS = {c: v["gm_symbol"] for c, v in HOLDINGS.items()}
gm_main.STOCK_NAMES = {c: v["name"] for c, v in HOLDINGS.items()}
gm_main.MIRROR_HOLDINGS = {c: {"qty": v["qty"], "cost": v["cost"]} for c, v in HOLDINGS.items()}
gm_main.INITIAL_CASH = 50000

# 掘金账号个股历史数据上限 180 自然日（最早 2026-03-02）。
# subscribe(60s,count=240) 预热实际拉 miss_count+1=241 根 bar：START 若为 03-03（盘前），
# 241 根会越过 03-02（周一）全 session(240根) 再往前 1 根到 02-27 → ERR_NO_DATA_PERMISSION。
# 故起点后移至 03-05：预热 241 根落在 03-04 session + 03-03 收盘，查询起点 03-03，安全落在 180 日内。
# 窗口 03-05 ~ 08-28 ≈ 5.8 个月（近半年，实际受掘金 180 自然日上限约束）。
START = _ARGS.start
END = _ARGS.end

gm_main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")
# 卖出体系状态独立目录，不触碰生产 auto_sell_state.json
import sell_state  # noqa: E402
sell_state.SELL_STATE_PATH = os.path.join(OUT_DIR, "sell_state.json")

# ── 回测"已持有底仓"播种 ──
# gm_main._reconcile_positions_at_init 在回测模式直接 return（L691-692），回测从空仓开始，
# 底仓建仓又被 TREND_BREAKDOWN / pool_gate 递延（base_deferred）→ 全程无成交。用户是"已持有"
# 这三只票，正确回测应让策略围绕现有底仓跑做T。GM 回测无"初始持仓"接口，唯一正规做法是
# 开盘即买入建底仓。故覆盖该函数：init 里按 HOLDINGS 逐票下市价买单 → 复用现有 base 建仓链路，
# 成交回调 on_order_status 自然把持仓灌入 executed_orders/manual_position 并标记 _base_settled。
#
# 注意成本语义：本回测度量"持有三票跑做T"的做T绩效，底仓成本=回测起点(03-05)成交价，
# 而非用户实际成本(588170 0.889 / 600481 28.216)。600481/588170 实际成本远高于起点价，
# 若按真实成本，-8% 硬止损会在首日即刻触发（浮亏 -73%/-35%），与本回测口径不同。
def _bt_seed_holdings(context):
    for code, sym in gm_main.STOCKS.items():
        h = HOLDINGS.get(code)
        if not h:
            continue
        qty = int(h.get("qty", 0))
        if qty <= 0:
            continue
        try:
            gm_main.order_volume(symbol=sym, volume=qty,
                                 side=gm_main.OrderSide_Buy,
                                 order_type=gm_main.OrderType_Market,
                                 position_effect=gm_main.PositionEffect_Open)
            context._base_ordered.add(code)
            setattr(context, f'_base_ref_{code}', qty)
            print(f"[INIT·回测播种] {code} {h.get('name', code)} 买入建底仓 {qty}股（市价，成本=起点成交价）")
        except Exception as e:
            print(f"[INIT·回测播种] {code} 下单失败: {e}")


gm_main._reconcile_positions_at_init = _bt_seed_holdings

print(f"[backtest_holdings] 标的={list(gm_main.STOCKS)}")
print(f"[backtest_holdings] 镜像底仓={ {c: v['qty'] for c, v in gm_main.MIRROR_HOLDINGS.items()} }")
print(f"[backtest_holdings] 窗口={START} ~ {END} 资金={gm_main.INITIAL_CASH}")
print(f"[backtest_holdings] 产物目录={OUT_DIR}")

run(strategy_id="95e85ee3-a287-11f1-9a76-98fa9b8df5e7",
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
