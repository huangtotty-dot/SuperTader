#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Renko 买入择时稳定性复验（39支候选股级）
============================================
背景: 修订试验(intraday_t_backtest_1year)发现 Renko向下砖+15分MACD金叉 的
  买入择时 +30min 胜率 60.2% (vs baseline 36.8%)。本脚本在更广样本上复验:
    • 22支候选股 × 6个月 5min 缓存 (t_io/backtest_cache/*.pkl)
    • + 5支已有 1年 1min 数据 (t_io/backtest_1year_data/*.csv)
 度量: 每日首个买入信号后 +15/+30/+60min 收益(不含手续费) → 择时胜率
 对比: Renko买入 vs baseline(布林下轨+15分MACD金叉)买入
 目的: 确认 Renko 买入择时在跨股票/跨数据频率上是否稳定 >50%
"""
import sys
import os
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
import glob

from analysis.indicators import resample_to_15min, add_15min_indicators, resample_to_5min, add_5min_indicators
from analysis.renko_builder import RenkoBuilder

OUT_FILE = BASE / "t_io" / "backtest_1year" / "renko_buy_timing_validation.txt"

HORIZONS = [15, 30, 60]   # 分钟


def load_1min():
    """加载 1年 1min 数据(全部候选股, 统一口径)"""
    frames = []
    for f in sorted(glob.glob(str(BASE / "t_io" / "backtest_1year_data" / "*_1year_1min.csv"))):
        code = os.path.basename(f).split("_1year")[0].split(".")[0]  # 统一去后缀
        df = pd.read_csv(f, parse_dates=["time"])
        df = df.dropna(subset=["time", "close"]).sort_values("time").reset_index(drop=True)
        frames.append((code, df, "1min"))
    return frames


def compute_buy_times(df, scheme):
    """返回每日首个买入信号时间点集合 + 每根bar的信号标记(时间->bool)"""
    if scheme == "renko":
        # Renko 砖 0.3% + 15min MACD 金叉
        df15 = add_15min_indicators(resample_to_15min(df))
        m15 = df15[["time", "macd_hist_15m"]].rename(columns={"time": "t15"})
        d = df.copy()
        d["t15"] = d["time"].dt.floor("15min")
        d = d.merge(m15, on="t15", how="left")
        d["macd_hist_15m"] = d["macd_hist_15m"].fillna(0.0)
        builder = RenkoBuilder(brick_size_pct=0.003)
        buy_times = set()
        for row in d.itertuples():
            created = builder.update(row.time, row.close, row.high, row.low, row.volume)
            if created and builder.brick_direction == "down" and row.macd_hist_15m > 0:
                buy_times.add(row.time)
        return buy_times
    elif scheme == "baseline":
        df5 = add_5min_indicators(resample_to_5min(df))
        m15 = add_15min_indicators(resample_to_15min(df))[["time", "macd_hist_15m"]].rename(columns={"time": "t15"})
        df5["t15"] = df5["time"].dt.floor("15min")
        df5 = df5.merge(m15, on="t15", how="left")
        df5["macd_hist_15m"] = df5["macd_hist_15m"].fillna(0.0)
        df5["bb_pct_5m"] = df5["bb_pct_5m"].clip(0, 1)
        buy = df5.loc[(df5["bb_pct_5m"] <= 0) & (df5["macd_hist_15m"] > 0), "time"]
        return set(buy)
    return set()


def buy_timing_quality(df, buy_times, horizons):
    """度量每日首个买入信号后 N 分钟收益。返回 {horizon: (n, win_rate, avg)}"""
    px_map = dict(zip(df["time"], df["close"]))
    res = {}
    for h in horizons:
        pnls = []
        for day, d in df.groupby(df["time"].dt.date):
            buys = sorted([t for t in d["time"] if t in buy_times])
            if not buys:
                continue
            t0 = buys[0]
            p0 = px_map.get(t0, 0)
            t1 = t0 + pd.Timedelta(minutes=h)
            p1 = px_map.get(t1)
            if p1 is not None and p0 and p0 > 0:
                pnls.append((p1 - p0) / p0 * 100)
        arr = np.array(pnls)
        if len(arr):
            res[h] = (len(arr), float((arr > 0).mean()) * 100, float(arr.mean()))
        else:
            res[h] = (0, 0.0, 0.0)
    return res


def main():
    lines = []
    def log(msg=""):
        print(msg)
        lines.append(msg)

    # 加载数据(统一 1min 口径)
    datasets = load_1min()
    log("=" * 100)
    log(f"Renko 买入择时稳定性复验 — {len(datasets)} 支股票 × 1年1min")
    log(f"砖高0.3% | 度量: 每日首个买入信号后N分钟收益(无手续费) | 对比: Renko vs baseline")
    log("=" * 100)

    # 每支股票: 6个月涨跌幅(用于市场状态分组)
    stock_results = {}  # code -> {scheme: {h: (n,wr,avg)}}, ret_6m
    for code, df, freq in datasets:
        ret_6m = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        rec = {"freq": freq, "ret_6m": ret_6m, "schemes": {}}
        for scheme in ("renko", "baseline"):
            try:
                buy_times = compute_buy_times(df, scheme)
                rec["schemes"][scheme] = buy_timing_quality(df, buy_times, HORIZONS)
            except Exception as e:
                rec["schemes"][scheme] = {}
        stock_results[code] = rec

    # ---- 汇总表: 方案 × 时间框架 ----
    log("\n" + "-" * 100)
    log("【汇总】买入择时胜率 (所有股票合并)")
    log(f"{'方案':<10}{'+15min':>24}{'+30min':>24}{'+60min':>24}")
    log(f"{'':<10}{'n':>6}{'胜率':>9}{'平均':>9}{'n':>6}{'胜率':>9}{'平均':>9}{'n':>6}{'胜率':>9}{'平均':>9}")
    log("-" * 100)
    agg = {}
    for scheme in ("renko", "baseline"):
        agg[scheme] = {}
        for h in HORIZONS:
            # 个股胜率/收益的平均(仅统计 n>=10 的股票, 避免小样本噪声)
            wrs = [rec["schemes"].get(scheme, {}).get(h, (0,0,0))[1] for rec in stock_results.values()
                   if rec["schemes"].get(scheme, {}).get(h, (0,0,0))[0] >= 10]
            avgs = [rec["schemes"].get(scheme, {}).get(h, (0,0,0))[2] for rec in stock_results.values()
                    if rec["schemes"].get(scheme, {}).get(h, (0,0,0))[0] >= 10]
            ns = [rec["schemes"].get(scheme, {}).get(h, (0,0,0))[0] for rec in stock_results.values()
                  if rec["schemes"].get(scheme, {}).get(h, (0,0,0))[0] >= 10]
            n_total = sum(ns)
            wr_avg = np.mean(wrs) if wrs else 0
            avg_avg = np.mean(avgs) if avgs else 0
            agg[scheme][h] = (n_total, wr_avg, avg_avg, len(ns))
        log(f"{scheme:<10}"
            + "".join(f"{agg[scheme][h][0]:>6d}{agg[scheme][h][1]:>8.1f}%{agg[scheme][h][2]:>+9.3f}%" for h in HORIZONS))

    # ---- 个股胜率稳定性 ----
    log("\n【稳定性】个股 +30min 买入择时胜率 (每支一行, 胜率>50%=择时有效)")
    log(f"{'股票':<12}{'频率':>5}{'6月涨跌':>8}{'Renko_n':>8}{'Renko胜率':>10}{'Base_n':>8}{'Base胜率':>10}{'Renko>50%?':>10}")
    log("-" * 100)
    renko_ok = 0; base_ok = 0; cnt = 0
    for code, rec in sorted(stock_results.items()):
        r30 = rec["schemes"].get("renko", {}).get(30, (0,0,0))
        b30 = rec["schemes"].get("baseline", {}).get(30, (0,0,0))
        r_ok = "✅" if r30[1] > 50 and r30[0] >= 10 else ("⚠️样本少" if r30[0] < 10 else "❌")
        b_ok = "✅" if b30[1] > 50 and b30[0] >= 10 else ("⚠️样本少" if b30[0] < 10 else "❌")
        if r30[0] >= 10: renko_ok += (1 if r30[1] > 50 else 0); cnt += 1
        if b30[0] >= 10: base_ok += (1 if b30[1] > 50 else 0)
        log(f"{code:<12}{rec['freq']:>5}{rec['ret_6m']:>+7.1f}%{r30[0]:>8d}{r30[1]:>9.1f}%{b30[0]:>8d}{b30[1]:>9.1f}%{r_ok:>10}")
    log("-" * 100)
    log(f"Renko 个股+30min胜率>50%占比: {renko_ok}/{cnt}   |   baseline: {base_ok}/{cnt}")

    # ---- 分市场状态 ----
    log("\n【按6个月涨跌幅分组】+30min 买入择时 (renko vs baseline)")
    log(f"{'分组':<16}{'Renko_n':>9}{'Renko胜率':>11}{'Renko平均':>11}{'Base_n':>9}{'Base胜率':>11}")
    log("-" * 100)
    groups = {"上涨>10%": lambda r: r > 10, "震荡(-10~10%)": lambda r: -10 <= r <= 10, "下跌<-10%": lambda r: r < -10}
    for gname, cond in groups.items():
        recs = [rec for rec in stock_results.values() if cond(rec["ret_6m"])]
        if not recs:
            log(f"{gname:<16} 无样本")
            continue
        rp = [rec["schemes"].get("renko", {}).get(30, (0,0,0)) for rec in recs]
        bp = [rec["schemes"].get("baseline", {}).get(30, (0,0,0)) for rec in recs]
        rn = sum(x[0] for x in rp); rwr = np.mean([x[1] for x in rp if x[0] >= 10]) if any(x[0] >= 10 for x in rp) else 0
        r_avg = np.mean([x[2] for x in rp if x[0] >= 10]) if any(x[0] >= 10 for x in rp) else 0
        bn = sum(x[0] for x in bp); bwr = np.mean([x[1] for x in bp if x[0] >= 10]) if any(x[0] >= 10 for x in bp) else 0
        log(f"{gname:<16}{rn:>9d}{rwr:>10.1f}%{r_avg:>+10.3f}%{bn:>9d}{bwr:>10.1f}%")

    log("\n⚠️ 注: 全部数据为 1年1min (2025-08-26~2026-08-26), Renko砖高统一0.3%")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n✅ 已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()
