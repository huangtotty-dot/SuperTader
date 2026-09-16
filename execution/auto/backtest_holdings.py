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
from datetime import datetime

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
_ap.add_argument("--cash", type=float, default=50000.0,
                 help="回测初始资金。播种底仓需 ≥ Σ(qty×价)；持仓变大后默认 5 万会全部拒单"
                      "（资金不足）→ 0 成交（2026-09-14 实证：6 票底仓需 ≈13.9 万）")
_ap.add_argument("--codes", default="", help="逗号分隔，只回测这些 6 位码（缺省=holdings 全部持仓）")
_ap.add_argument("--tp", type=float, default=0.0,
                 help="覆盖 swing_take_profit_pct（如 0.008）；0=用生产默认 0.005。做止盈档位扫描用")
_ap.add_argument("--b7", action="store_true",
                 help="启用 B7 尾盘反T通道（仅本回测生效；通过 SUPERTRADER_B7_BACKTEST=1 传给 gm_main）")
_ap.add_argument("--full-cost", action="store_true",
                 help="按生产口径计成本：把印花税折算进佣金率（往返 0.136%）。")
_ap.add_argument("--b7-no-breaker", action="store_true",
                 help="B7 连亏熔断不生效（仅评估用：熔断不自动复活会把长跑截断在第一个 4 连亏）")
# ── 成本口径（2026-09-15 实测修正）────────────────────────────────────────────
# gm 回测**不支持印花税参数**（端子可选参数仅 backtest_commission_ratio / slippage_ratio /
# transaction_ratio / commission_unit / marginfloat_ratio，无 tax）。默认 0.00015 双边 =
# 往返 0.030%，**比生产口径 0.136% 少算约 0.106pp/笔** → 绝对收益虚高、任何"费后 +x%/笔"
# 的策略都显得比真实好。
# --full-cost 把印花税折进佣金：c 双边 → 往返 2c = 0.136% ⇒ c = 0.00068（按 1:1 买卖量近似，
# 买入略高估、卖出略低估，总额正确，偏保守）。
_ARGS = _ap.parse_args()
_COMMISSION_RATIO = 0.00068 if _ARGS.full_cost else 0.00015
# 清空自定义参数：gm.api 在 import 时(getopt)与 run() 内(optparse)都会二次解析 sys.argv，
# 不认识 --start/--end/--label 会抛 "no such option"；此处先消费掉，仅保留脚本名。
sys.argv = [sys.argv[0]]

# B7 尾盘反T：必须在 import gm_main 之前置位（gm_main 在模块级读取该变量）
if _ARGS.b7:
    os.environ["SUPERTRADER_B7_BACKTEST"] = "1"
    print("[backtest_holdings] B7 尾盘反T通道已启用（仅本回测）")
if _ARGS.b7_no_breaker:
    os.environ["SUPERTRADER_B7_BACKTEST"] = "1"
    os.environ["SUPERTRADER_B7_NO_BREAKER"] = "1"
    print("[backtest_holdings] B7 连亏熔断已禁用（仅评估用）")

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
    _only = {s.strip() for s in (_ARGS.codes or "").split(",") if s.strip()}
    return {
        c: {"name": h.get("name", c), "gm_symbol": h.get("gm_symbol", ""),
            "qty": int(h.get("qty") or 0), "cost": float(h.get("cost") or 0)}
        for c, h in _data.items()
        if isinstance(h, dict) and not str(c).startswith("_") and int(h.get("qty") or 0) > 0
        and (not _only or c in _only)
    }


HOLDINGS = _load_holdings_for_backtest()

gm_main.STOCKS = {c: v["gm_symbol"] for c, v in HOLDINGS.items()}
gm_main.STOCK_NAMES = {c: v["name"] for c, v in HOLDINGS.items()}
gm_main.MIRROR_HOLDINGS = {c: {"qty": v["qty"], "cost": v["cost"]} for c, v in HOLDINGS.items()}
gm_main.INITIAL_CASH = float(_ARGS.cash)
if _ARGS.tp > 0:
    # PARAMS 是 config.params 的同一个 dict 引用（t_engine_auto 亦 from config.params import PARAMS），
    # 就地改即对内核 _get_params 生效。
    gm_main.PARAMS["swing_take_profit_pct"] = float(_ARGS.tp)
    print(f"[backtest_holdings] swing_take_profit_pct 覆盖为 {_ARGS.tp}")

# B7 生产通道（shadow / live）在本回测里必须显式关闭：
# config 里 b7_shadow_enabled / b7_live_enabled 已翻启（owner 2026-09-15 17:23 拍板跳过影子期直接实单），
# 不加 --b7 时 _B7_BACKTEST_ENABLE=False → 生产 shadow/live 逻辑会照常执行，
# 使「基线」轮自带 B7，A/B 失去意义。此处强制关闭，保证基线纯净。
if not _ARGS.b7:
    for _k in ("b7_shadow_enabled", "b7_live_enabled"):
        gm_main.PARAMS[_k] = False
    print("[backtest_holdings] 已强制关闭 B7 生产通道（shadow/live）→ 本轮为纯净基线")

# 掘金账号个股历史数据上限 180 自然日（最早 2026-03-02）。
# subscribe(60s,count=240) 预热实际拉 miss_count+1=241 根 bar：START 若为 03-03（盘前），
# 241 根会越过 03-02（周一）全 session(240根) 再往前 1 根到 02-27 → ERR_NO_DATA_PERMISSION。
# 故起点后移至 03-05：预热 241 根落在 03-04 session + 03-03 收盘，查询起点 03-03，安全落在 180 日内。
# 窗口 03-05 ~ 08-28 ≈ 5.8 个月（近半年，实际受掘金 180 自然日上限约束）。
START = _ARGS.start
END = _ARGS.end

gm_main._AUDIT_LOG_PATH = os.path.join(OUT_DIR, "backtrace.jsonl")
# 2026-09-15 阶段0-4（诊断D3）：镜像路径同步重定向——只改主链会漏镜像通道，回测审计灌入生产 auto_backtrace.jsonl（91% 污染根因）
gm_main._AUDIT_MIRROR_PATH = os.path.join(OUT_DIR, "backtrace_mirror.jsonl")
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
            # ⚠️ 必须先 write_order 落 order 事件：on_order_status 的孤儿闸
            # （_strategy_ordered_today）按"当日事件桥有无本策略 order"判非本策略成交，
            # 直接调 order_volume 会 5/6 单被判 orphan_fill「不入台账」→ 引擎以为没持仓 →
            # 全程 no_signal（2026-09-14 实证；588170 侥幸通过只因当时事件文件尚不存在走 fail-open）。
            try:
                from gm_bridge.writer import write_order as _wo
                _wo(str(datetime.now()), code, "BUY", qty, float(h.get("cost") or 0))
            except Exception as _we:
                print(f"[INIT·回测播种] {code} order 事件写入失败（将触发孤儿闸）: {_we}")
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
    backtest_commission_ratio=_COMMISSION_RATIO,
    backtest_slippage_ratio=0.0001,
    backtest_adjust=ADJUST_PREV,
    backtest_match_mode=1)
