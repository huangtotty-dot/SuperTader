#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线回放验证: Renko买入+目标止盈 集成到 signal_engine 后的完整做T闭环
=====================================================================
对比三种口径的"每天一买一卖做T闭环":
  A) evaluate 驱动 + swing_use_renko=True  (集成后: Renko买入+target止盈)
  B) evaluate 驱动 + swing_use_renko=False (集成前: 布林触轨+MACD)
  C) 独立逻辑 sig_renko + target 止盈        (全量基准, 39支已验证胜率78.5%)

数据: t_io/backtest_1year_data/*.csv (1min)
步进: 每 step_min 分钟 evaluate 一次(做T信号为5min级, 51ms/次, 全量每分钟不可行)
结算: 每日首个 BUY_LOW 为entry → 首个 SELL_HIGH/尾盘为 exit, 手续费双边0.0005

用法:
  python scripts/replay_renko_t.py                       # 默认5支代表×近3月×每5min
  python scripts/replay_renko_t.py --codes 000988,002261 --days 120 --step 5
"""
import sys
import os
import glob
from pathlib import Path

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pandas as pd
import numpy as np
import argparse

COST = 0.0005  # 双边手续费(与回测口径一致)

# 默认代表样本: 牛/熊/震荡/不同板块
DEFAULT_CODES = ["000988", "002202", "002261", "002176", "002451"]


def load_df(code):
    matches = glob.glob(str(BASE / "t_io" / "backtest_1year_data" / f"{code}*_1year_1min.csv"))
    if not matches:
        return None
    df = pd.read_csv(matches[0], parse_dates=["time"])
    df = df.dropna(subset=["time", "close"]).sort_values("time").reset_index(drop=True)
    df["date"] = df["time"].dt.strftime("%Y-%m-%d")
    return df


def run_evaluate(code, df, use_renko, step_min=5):
    """用 SignalEngine.evaluate 驱动, 每 step_min 分钟评估一次, 返回信号列表[(ts, action, price)]"""
    import core.signal_engine as se
    from core.signal_engine import SignalEngine
    from config import PARAMS

    se.HOLDINGS = {}
    se.VIRTUAL_TRADES = {}
    se.MINUTE_FETCH_STATUS = {code: "ok"}
    se.PERSIST_INTRADAY_STATE = False
    se.PARAMS = PARAMS
    PARAMS["swing_use_renko"] = use_renko

    eng = SignalEngine()
    signals = []
    # 每5分钟边界(含9:30/收盘)
    eval_times = df[df["time"].dt.minute % step_min == 0]["time"].unique()
    prev_ts = None
    for bt in eval_times:
        se.SIM_NOW = bt.to_pydatetime()
        sub = df[df["time"] <= bt]
        if len(sub) < 5:
            continue
        price = float(sub.iloc[-1]["close"])
        daily_ctx = {"daily_status": "ok", "daily_buy_t_ok": True,
                     "index_regime": "range", "intraday_alerts": []}
        holding = {"name": code, "cost": price, "qty": 500, "base": 500,
                   "t_qty": 500, "type": "stock"}
        try:
            bs, ss, sig = eng.evaluate(code, code, sub, holding, daily_ctx=daily_ctx)
        except Exception:
            continue
        if sig and sig.action in ("BUY_LOW", "SELL_HIGH"):
            signals.append((pd.Timestamp(sig.ts) if sig.ts is not None else bt, sig.action, price))
    return signals


def settle_daily(signals):
    """每天一买一卖闭环: 当日首个 BUY_LOW → 首个 SELL_HIGH(或当日最后SELL) 为一轮"""
    trades = []
    from collections import defaultdict
    by_day = defaultdict(list)
    for ts, action, price in signals:
        by_day[ts.date()].append((ts, action, price))
    for day in sorted(by_day):
        seq = sorted(by_day[day], key=lambda x: x[0])
        entry = None
        for ts, action, price in seq:
            if action == "BUY_LOW" and entry is None:
                entry = price
            elif action == "SELL_HIGH" and entry is not None:
                pnl = (price * (1 - COST) - entry * (1 + COST)) / (entry * (1 + COST)) * 100
                trades.append(pnl)
                entry = None
                break  # 每天一买一卖, 一轮后当日结束
        # 尾盘强平兜底(当天有entry未卖出——evaluate 已含尾盘14:55强平, 此处兜底)
        if entry is not None:
            last_px = seq[-1][2]
            pnl = (last_px * (1 - COST) - entry * (1 + COST)) / (entry * (1 + COST)) * 100
            trades.append(pnl)
    return np.array(trades) if trades else np.array([])


def run_independent(code, df):
    """独立逻辑基准: sig_renko(向下砖+15分MACD金叉) + target 0.5% 止盈 (每日一买一卖)"""
    sys.path.insert(0, str(BASE))
    from scripts.intraday_t_backtest_1year import StockData, sig_renko
    from datetime import timedelta
    from analysis.renko_builder import RenkoBuilder

    sd = StockData(df)
    buy_times = sig_renko(sd)[0]
    builder = RenkoBuilder(brick_size_pct=0.003)
    renko_up = set()
    for row in df.itertuples():
        if builder.update(row.time, row.close, row.high, row.low, row.volume) and builder.brick_direction == "up":
            renko_up.add(row.time)
    px_map = dict(zip(df["time"], df["close"]))
    trades = []
    for day, d in df.groupby(df["time"].dt.date):
        buys = sorted([t for t in d["time"] if t in buy_times])
        if not buys:
            continue
        entry_t = buys[0]
        entry = px_map[entry_t] * (1 + COST)
        fut = d[d["time"] > entry_t]
        hit = fut[fut["close"] >= entry * (1 + 0.005)]
        force = fut[fut["time"].dt.hour * 60 + fut["time"].dt.minute >= 1455]
        fallback = fut.iloc[-1]["close"] if not fut.empty else d.iloc[-1]["close"]
        exit_px = hit.iloc[0]["close"] if not hit.empty else (force.iloc[0]["close"] if not force.empty else fallback)
        pnl = (exit_px * (1 - COST) - entry) / entry * 100
        trades.append(pnl)
    return np.array(trades)


def summarize(trades):
    if not len(trades):
        return (0, 0.0, 0.0)
    return (len(trades), float((trades > 0).mean()) * 100, float(trades.mean()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=",".join(DEFAULT_CODES))
    ap.add_argument("--days", type=int, default=90, help="回放最近N个交易日")
    ap.add_argument("--step", type=int, default=5, help="evaluate步进分钟(默认5)")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",")]

    lines = []
    def log(msg=""):
        print(msg)
        lines.append(msg)

    log("=" * 100)
    log(f"离线回放验证: Renko买入+目标止盈 集成 signal_engine")
    log(f"样本: {len(codes)}支 × 最近{args.days}交易日 × 每{args.step}min evaluate")
    log("=" * 100)

    agg = {"renko_on": [], "renko_off": [], "independent": []}
    for code in codes:
        df = load_df(code)
        if df is None or df.empty:
            log(f"{code}: 无数据, 跳过")
            continue
        # 取最近N个交易日
        last_days = sorted(df["date"].unique())[-args.days:]
        df = df[df["date"].isin(last_days)].reset_index(drop=True)

        try:
            sig_on = run_evaluate(code, df, True, step_min=args.step)
            tr_on = settle_daily(sig_on)
        except Exception as e:
            log(f"{code}: evaluate开 异常 {str(e)[:40]}")
            tr_on = np.array([])
        try:
            sig_off = run_evaluate(code, df, False, step_min=args.step)
            tr_off = settle_daily(sig_off)
        except Exception as e:
            log(f"{code}: evaluate关 异常 {str(e)[:40]}")
            tr_off = np.array([])
        try:
            tr_ind = run_independent(code, df)
        except Exception as e:
            log(f"{code}: 独立基准 异常 {str(e)[:40]}")
            tr_ind = np.array([])

        n_on, wr_on, avg_on = summarize(tr_on)
        n_off, wr_off, avg_off = summarize(tr_off)
        n_ind, wr_ind, avg_ind = summarize(tr_ind)
        agg["renko_on"].append(tr_on); agg["renko_off"].append(tr_off); agg["independent"].append(tr_ind)
        log(f"{code:<8} 集成开(Renko+止盈): {n_on:>3}笔 {wr_on:5.1f}% {avg_on:+.3f}% | "
            f"集成关(原布林): {n_off:>3}笔 {wr_off:5.1f}% {avg_off:+.3f}% | "
            f"独立基准: {n_ind:>3}笔 {wr_ind:5.1f}% {avg_ind:+.3f}%")

    log("\n" + "-" * 100)
    log("📊 汇总 (合并全部股票)")
    log(f"{'口径':<22}{'总交易':>8}{'胜率':>9}{'平均收益':>10}")
    log("-" * 100)
    for name, key in [("集成开 Renko+止盈", "renko_on"), ("集成关 原布林+MACD", "renko_off"),
                      ("独立基准 Renko+止盈", "independent")]:
        all_t = np.concatenate(agg[key]) if agg[key] else np.array([])
        n, wr, avg = summarize(all_t)
        log(f"{name:<22}{n:>8d}{wr:>8.1f}%{avg:>+9.3f}%")

    out = BASE / "t_io" / "backtest_1year" / "replay_renko_t_result.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n✅ 已保存: {out}")


if __name__ == "__main__":
    main()
