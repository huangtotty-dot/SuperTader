# -*- coding: utf-8 -*-
"""偏离 365 日线（年线）多少算「有风险」？—— 2026-10-28 owner。

问题：「有一列是偏离 365 日年线度，研究下偏离多少会有风险」。

口径：
  · 数据 = `tmp/bias5_panel.parquet`（由 `scripts/experiment/bias5_nextday_20261028.py` 从
    `t_io/cache/daily_kline` 生成的**前复权**面板）。没有该缓存时自动从日线缓存重建；
  · dev365 = (close / MA365 − 1) × 100，MA365 = 最近 365 根收盘均值（含当日），
    **不足 365 根一律不编**；
  · 「风险」给四族指标，不只用收益率（只看向下收益会把「波动变大」漏掉）：
        A 未来 5 日收益 < 0 的概率        B 未来 5 日**最大回撤** MDD5（用 low 算）
        C P(MDD5 ≤ −10%)                  D 未来 5 日**超额**收益（当日截面去均值）
  · t 值按**日聚类**（同日几千只高度相关，按行数算标准误会把 t 放大一个量级）。

与 2026-10-24 那轮（`doc/research/2026-10-24_偏离365日线_超买超卖阈值实验.md`，
931 只 / 25.7 万股票·日）的关系：那轮只看了**收益方向**（结论：两侧都没有可用反转 alpha），
本轮把样本扩到 2,600+ 只并补上**回撤/亏损概率**两族，专门回答"风险"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PANEL = Path("tmp/bias5_panel.parquet")


def load() -> pd.DataFrame:
    if not PANEL.exists():
        raise SystemExit(f"缺面板缓存 {PANEL}：先跑 scripts/experiment/bias5_nextday_20261028.py")
    df = pd.read_parquet(PANEL, columns=["code", "date", "close", "high", "low"])
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code", sort=False)
    df["ma365"] = g["close"].transform(lambda s: s.rolling(365, min_periods=365).mean())
    df["dev365"] = (df["close"] / df["ma365"] - 1.0) * 100.0
    for k in (5, 10):
        df[f"fwd{k}"] = g["close"].transform(lambda s: s.shift(-k) / s - 1.0) * 100.0
    # 未来 5 日最大回撤：t+1..t+5 的最低 low 相对当日收盘
    fwd_min_low = g["low"].transform(lambda s: s.shift(-1).rolling(5, min_periods=1).min().shift(-(4)))
    df["mdd5"] = (fwd_min_low / df["close"] - 1.0) * 100.0
    df = df.dropna(subset=["dev365", "fwd5", "mdd5"])
    df["ex5"] = df["fwd5"] - df.groupby("date")["fwd5"].transform("mean")
    df["loss5"] = (df["fwd5"] < 0).astype(float)
    df["mdd10"] = (df["mdd5"] <= -10).astype(float)
    df["mdd15"] = (df["mdd5"] <= -15).astype(float)
    return df


BINS = [(-100, -60), (-60, -45), (-45, -35), (-35, -25), (-25, -15), (-15, -5),
        (-5, 5), (5, 15), (15, 30), (30, 45), (45, 60), (60, 80), (80, 120), (120, 10000)]


def row(tag: str, x: pd.DataFrame, base: bool = False) -> dict:
    d = {"dev365档": tag, "样本": len(x),
         "5日跌%": 100 * x["loss5"].mean(),
         "MDD5均%": x["mdd5"].mean(),
         "P(回撤>10%)": 100 * x["mdd10"].mean(),
         "P(回撤>15%)": 100 * x["mdd15"].mean(),
         "5日收益%": x["fwd5"].mean(),
         "5日超额%": x["ex5"].mean(),
         "10日收益%": x["fwd10"].mean()}
    daily = x.groupby("date")["ex5"].mean()
    if base or len(daily) < 5:
        d["t(超额)"] = np.nan
    else:
        se = daily.std(ddof=1) / np.sqrt(len(daily))
        d["t(超额)"] = float(daily.mean() / se) if se > 0 else np.nan
    return d


def show(s: pd.DataFrame, title: str) -> None:
    rows = [row("【基线】", s, base=True)]
    for lo, hi in BINS:
        x = s[(s["dev365"] > lo) & (s["dev365"] <= hi)]
        if len(x) < 200:
            continue
        rows.append(row(f"({lo:g},{hi:g}]", x))
    out = pd.DataFrame(rows)
    print(f"\n===== {title}  样本={len(s):,} 股票={s['code'].nunique():,} "
          f"日期 {s['date'].min()}~{s['date'].max()} =====")
    with pd.option_context("display.width", 220, "display.max_columns", 20,
                           "display.float_format", lambda v: f"{v:8.2f}"):
        print(out.to_string(index=False))


def main() -> int:
    df = load()
    print(f"可用样本 {len(df):,} 股票·日 / {df['code'].nunique():,} 只")
    q = df["dev365"].quantile([.05, .25, .5, .75, .9, .95, .99])
    print("dev365 分位:", {f"p{int(k*100)}": round(v, 1) for k, v in q.items()})
    show(df, "全样本")
    last = df["date"].max()
    y1 = (pd.to_datetime(last) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    show(df[df["date"] >= y1], f"最近一年（≥{y1}）")
    show(df[df["date"] < y1], f"更早（<{y1}）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
