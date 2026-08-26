#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
样本外敏感性验证: Renko砖高 × 目标止盈 网格
============================================
目的: 确认生产参数(砖高0.3% / 止盈0.5%)在样本外不是孤立尖峰, 增强实战信心。
方法: 39支×1年1min, 训练期(2025-08-26~2026-06-26) + 验证期(2026-06-26~2026-08-26)
      网格: 砖高[0.2%,0.3%,0.5%] × 止盈[0.3%,0.5%,0.8%]
      度量: 每天一买一卖做T闭环 胜率/平均收益
判断: 0.3%/0.5% 在训练+验证期都近最优(或非最差), 且验证期胜率>55% → 参数稳健
用法: python scripts/param_sensitivity_validation.py
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

from analysis.indicators import resample_to_15min, add_15min_indicators
from analysis.renko_builder import RenkoBuilder

COST = 0.0005
TRAIN_END = pd.Timestamp("2026-06-26")
BRICKS = [0.002, 0.003, 0.005]
TPS = [0.003, 0.005, 0.008]
OUT_FILE = BASE / "t_io" / "backtest_1year" / "param_sensitivity_result.txt"


def load_all():
    stocks = {}
    for f in sorted(glob.glob(str(BASE / "t_io" / "backtest_1year_data" / "*_1year_1min.csv"))):
        code = os.path.basename(f).split("_1year")[0].split(".")[0]
        df = pd.read_csv(f, parse_dates=["time"]).dropna(subset=["time", "close"])
        df = df.sort_values("time").reset_index(drop=True)
        # 15min MACD 映射到1min
        d15 = add_15min_indicators(resample_to_15min(df))
        m15 = d15[["time", "macd_hist_15m"]].rename(columns={"time": "t15"})
        df["t15"] = df["time"].dt.floor("15min")
        df = df.merge(m15, on="t15", how="left")
        df["macd15"] = df["macd_hist_15m"].fillna(0.0)
        stocks[code] = df
    return stocks


def build_buy_times(df, brick_pct):
    """Renko 向下砖(砖高brick) + 15分MACD金叉 → 买入信号时刻"""
    builder = RenkoBuilder(brick_size_pct=brick_pct)
    buy_times = set()
    for row in df.itertuples():
        if builder.update(row.time, row.close, row.high, row.low, row.volume) \
                and builder.brick_direction == "down" and row.macd15 > 0:
            buy_times.add(row.time)
    return buy_times


def t_backtest(df, buy_times, tp):
    """每天一买一卖做T闭环: 每日首个买入 → target止盈 或 尾盘强平"""
    px_map = dict(zip(df["time"], df["close"]))
    trades = []
    for day, d in df.groupby(df["time"].dt.date):
        buys = sorted([t for t in d["time"] if t in buy_times])
        if not buys:
            continue
        entry_t = buys[0]
        entry = px_map[entry_t] * (1 + COST)
        fut = d[d["time"] > entry_t]
        hit = fut[fut["close"] >= entry * (1 + tp)]
        force = fut[fut["time"].dt.hour * 60 + fut["time"].dt.minute >= 1455]
        fallback = fut.iloc[-1]["close"] if not fut.empty else d.iloc[-1]["close"]
        exit_px = hit.iloc[0]["close"] if not hit.empty else (force.iloc[0]["close"] if not force.empty else fallback)
        trades.append((exit_px * (1 - COST) - entry) / entry * 100)
    return np.array(trades)


def main():
    print("加载 39 支数据...")
    stocks = load_all()
    print(f"共 {len(stocks)} 支")

    # 网格结果: {(brick, tp): {"train": [...pnl], "valid": [...pnl]}}
    grid = {}
    for brick in BRICKS:
        for tp in TPS:
            grid[(brick, tp)] = {"train": [], "valid": []}

    for code, df in sorted(stocks.items()):
        for brick in BRICKS:
            buy_times = build_buy_times(df, brick)
            train_df = df[df["time"] < TRAIN_END]
            valid_df = df[df["time"] >= TRAIN_END]
            for tp in TPS:
                tr = t_backtest(train_df, buy_times, tp)
                va = t_backtest(valid_df, buy_times, tp)
                if len(tr):
                    grid[(brick, tp)]["train"].extend(tr.tolist())
                if len(va):
                    grid[(brick, tp)]["valid"].extend(va.tolist())
        print(f"  {code} done", flush=True)

    lines = []
    def log(msg=""):
        print(msg)
        lines.append(msg)

    log("=" * 100)
    log("样本外敏感性验证: Renko砖高 × 目标止盈 (39支 × 1年1min)")
    log(f"训练期: < 2026-06-26  |  验证期: >= 2026-06-26  |  每天一买一卖做T闭环, 手续费双边0.0005")
    log("=" * 100)

    for phase in ("train", "valid"):
        log(f"\n【{phase}期】胜率 %  (平均收益 % / 笔)")
        label = "砖高/止盈"
        log(f"{label:<12}" + "".join(f"{tp*100:.1f}%".rjust(16) for tp in TPS))
        log("-" * 100)
        for brick in BRICKS:
            row = f"砖{brick*100:.1f}%".ljust(12)
            for tp in TPS:
                pnls = np.array(grid[(brick, tp)][phase])
                if len(pnls):
                    wr = (pnls > 0).mean() * 100
                    avg = pnls.mean()
                    marker = " ★" if (brick == 0.003 and tp == 0.005) else ""
                    row += f"{wr:5.1f}%/{avg:+.2f}{marker}".rjust(16)
                else:
                    row += "".rjust(16)
            log(row)

    # 验证期结论
    log("\n" + "=" * 100)
    log("🎯 生产参数(砖0.3% / 止盈0.5%)稳健性判断")
    log("=" * 100)
    prod = grid[(0.003, 0.005)]
    pv = np.array(prod["valid"])
    pt = np.array(prod["train"])
    vwr = (pv > 0).mean() * 100 if len(pv) else 0
    twr = (pt > 0).mean() * 100 if len(pt) else 0
    vavg = pv.mean() if len(pv) else 0
    tavg = pt.mean() if len(pt) else 0
    log(f"  生产参数: 训练期 {twr:.1f}% / {tavg:+.3f}% (n={len(pt)}), 验证期 {vwr:.1f}% / {vavg:+.3f}% (n={len(pv)})")
    # 在验证期网格中的排名
    valid_wrs = {(b, t): ((np.array(grid[(b, t)]["valid"]) > 0).mean() * 100) for b in BRICKS for t in TPS if len(grid[(b, t)]["valid"])}
    rank = sorted(valid_wrs, key=valid_wrs.get, reverse=True)
    prod_rank = rank.index((0.003, 0.005)) + 1 if (0.003, 0.005) in rank else len(rank) + 1
    log(f"  验证期胜率排名: 0.3%/0.5% 排第 {prod_rank}/{len(rank)} (1=最优)")
    log(f"  判断: 验证期胜率 {vwr:.1f}% {'✅>55% 稳健' if vwr > 55 else '⚠️≤55% 需警惕'}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n✅ 已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()
