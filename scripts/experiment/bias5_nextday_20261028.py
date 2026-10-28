# -*- coding: utf-8 -*-
"""5 日乖离度 → 次日「调整」概率（owner 2026-10-28）。

问题：「股价偏离 5 日线多少，第二天调整的概率较高？」

口径：
  · 数据 = `t_io/cache/daily_kline/*.json`（腾讯**前复权**，避免除权跳空污染）；
  · bias5 = (close / MA5 − 1) × 100，MA5 = 最近 5 根收盘均值（含当日）；
  · 「次日」= 该股自己的下一根日线；今天的盘中半成品根剔除；
  · 「调整」给四种互不替代的定义，并且**同时给方向与波动两族指标** ——
    只看「下跌概率」会把「波动变大」误读成「要跌」：
        A 次日下跌（ret1 < 0）        B 次日跌 > 1%
        C 次日上涨 > 1%               D 次日振幅 (high−low)/close
  · 主口径用**超额**（当日全市场截面去均值）把大盘剥掉；
  · t 值按**日聚类**：先把每档的指标按交易日取均值，再对「日均值序列」做 t ——
    2.9 万行里同日几千只高度相关，按行数算标准误会把 t 放大十倍以上。

产物：`tmp/bias5_panel.parquet`（185MB 缓存，重跑秒开；首次约 12 分钟解析 5,540 个 JSON）+ 终端表。
运行：`python scripts/experiment/bias5_nextday_20261028.py`
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TODAY = "2026-10-28"
PANEL = Path("tmp/bias5_panel.parquet")


def load_panel() -> pd.DataFrame:
    rows = []
    for fp in sorted(glob.glob("t_io/cache/daily_kline/*.json")):
        code = fp.replace("\\", "/").rsplit("/", 1)[-1][:-5]
        if code.startswith("index_"):
            continue
        try:
            d = json.loads(open(fp, encoding="utf-8").read())
        except Exception:
            continue
        rr = d.get("rows") if isinstance(d, dict) else d
        if not rr:
            continue
        for r in rr:
            rows.append((code, r.get("date"), r.get("open"), r.get("high"),
                         r.get("low"), r.get("close"), r.get("volume")))
    return pd.DataFrame(rows, columns=["code", "date", "open", "high", "low", "close", "volume"])


def build(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw[raw["date"] < TODAY].copy()          # 今天的盘中半成品根不进统计
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code", sort=False)
    df["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    df["bias5"] = (df["close"] / df["ma5"] - 1.0) * 100.0
    for c in ("close", "high", "low"):
        df[c + "1"] = g[c].transform(lambda s: s.shift(-1))
    df["ret1"] = (df["close1"] / df["close"] - 1.0) * 100.0
    df = df.dropna(subset=["bias5", "ret1", "close1"])
    df = df[df["volume"] > 0]                     # 停牌根不要（前复权下 close 会复制）
    df["amp1"] = (df["high1"] - df["low1"]) / df["close"] * 100.0
    df["ret1_ex"] = df["ret1"] - df.groupby("date")["ret1"].transform("mean")
    df["down"] = (df["ret1"] < 0).astype(float)
    df["down1"] = (df["ret1"] <= -1.0).astype(float)
    df["up1"] = (df["ret1"] >= 1.0).astype(float)
    df["ex_down"] = (df["ret1_ex"] < 0).astype(float)
    df["break_ma5"] = (df["close1"] < df["ma5"]).astype(float)
    return df


BINS = [(-100, -15), (-15, -10), (-10, -8), (-8, -6), (-6, -4), (-4, -2), (-2, -1),
        (-1, 0), (0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 8),
        (8, 10), (10, 15), (15, 20), (20, 100)]

COLS = [("n", "样本"), ("次日跌%", None), ("次日跌>1%", None), ("次日涨>1%", None),
        ("振幅%", "amp1"), ("次日收益%", None), ("次日超额%", None),
        ("超额<0%", "ex_down"), ("t(超额)", None)]


def _row(tag: str, x: pd.DataFrame, is_base: bool = False) -> dict:
    d = {"bias5档": tag, "样本": len(x),
         "次日跌%": 100 * x["down"].mean(),
         "次日跌>1%": 100 * x["down1"].mean(),
         "次日涨>1%": 100 * x["up1"].mean(),
         "振幅%": x["amp1"].mean(),
         "次日收益%": x["ret1"].mean(),
         "次日超额%": x["ret1_ex"].mean(),
         "超额<0%": 100 * x["ex_down"].mean()}
    # 日聚类 t：每档按交易日求均值再检验
    daily = x.groupby("date")["ret1_ex"].mean()
    if is_base or len(daily) < 5:
        d["t(超额)"] = np.nan
    else:
        se = daily.std(ddof=1) / np.sqrt(len(daily))
        d["t(超额)"] = float(daily.mean() / se) if se > 0 else np.nan
    return d


def table(s: pd.DataFrame, title: str) -> pd.DataFrame:
    rows = [_row("【基线】", s, is_base=True)]
    for lo, hi in BINS:
        x = s[(s["bias5"] > lo) & (s["bias5"] <= hi)]
        if len(x) < 200:
            continue
        rows.append(_row(f"({lo:g},{hi:g}]", x))
    out = pd.DataFrame(rows)
    print(f"\n===== {title}  样本={len(s):,}  股票={s['code'].nunique():,}  "
          f"日期 {s['date'].min()}~{s['date'].max()} =====")
    with pd.option_context("display.width", 220, "display.max_columns", 20,
                           "display.float_format", lambda v: f"{v:8.2f}"):
        print(out.to_string(index=False))
    return out


def main():
    if PANEL.exists():
        df = pd.read_parquet(PANEL)
        print(f"命中面板缓存 {PANEL}")
    else:
        print("载入面板…", flush=True)
        df = build(load_panel())
        tmp = PANEL.with_suffix(".tmp.parquet")
        df.to_parquet(tmp, index=False)
        tmp.replace(PANEL)
        print(f"面板已缓存 → {PANEL}")
    print(f"样本行 {len(df):,}  股票 {df['code'].nunique():,}  "
          f"日期 {df['date'].min()}~{df['date'].max()}")
    print("最后 6 个日期覆盖：")
    print(df.groupby("date").size().tail(6).to_string())
    table(df, "全样本")
    last = df["date"].max()
    y1 = (pd.to_datetime(last) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    table(df[df["date"] >= y1], f"最近一年（≥{y1}）")
    table(df[df["date"] < y1], f"更早（<{y1}）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
