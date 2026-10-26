# -*- coding: utf-8 -*-
"""「平均成交量用多少天」——用通达信 5 年全市场日线做的窗口选择实验（2026-10-25）。

owner 需求：K 线成交量区画一条**平均成交量**，目的是"测量主力行为"，要研究用多少天算。

做法（横截面 IC，去掉大盘 beta）：
  对 N ∈ {5, 10, 20, 30, 60}：
      量比_N(t) = 当日成交量 / 前 N 日均量（不含当日）
      前向收益 fwd5(t) = close(t+5)/close(t) − 1（次日不可得，用当日收盘起算，只做**相对**比较）
  每天在横截面上算 Spearman IC(量比, fwd5)，再对日序列求均值/t 值；
  另按量比分 5 档，看「相对当日全市场均值」的超额（扣掉大盘，避免把 beta 当 alpha）。

输出：控制台表格 + `doc/research/2026-10-25_成交量均线窗口实验.md`

用法：``python scripts/experiment/volume_ma_window_20261025.py [--stocks 600]``
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parents[2]
FP = BASE / "t_io" / "tdx" / "tdx_daily.parquet"
OUT = BASE / "doc" / "research" / "2026-10-25_成交量均线窗口实验.md"
WINDOWS = (5, 10, 20, 30, 60)
H = 5                     # 前向收益天数


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=600)
    ap.add_argument("--min-bars", type=int, default=300)
    ap.add_argument("--field", default="volume", choices=("volume", "amount"))
    a = ap.parse_args()

    pf = pq.ParquetFile(str(FP))
    md = pf.metadata
    codes = []
    for rg in range(md.num_row_groups):
        st = md.row_group(rg).column(0).statistics
        if st and st.min is not None and str(st.min) == str(st.max):
            codes.append((str(st.min), rg))
    rng = np.random.default_rng(20261025)
    rng.shuffle(codes)
    codes = codes[: a.stocks]
    print(f"抽样 {len(codes)} 只（全市场 {md.num_row_groups} 只）", flush=True)

    # 每只票产出：date -> {ratio_N, fwd}
    recs = {n: [] for n in WINDOWS}
    used = 0
    for i, (code, rg) in enumerate(codes, 1):
        try:
            t = pf.read_row_group(rg, columns=["date", "close", a.field])
        except Exception:
            continue
        df = t.to_pandas()
        if len(df) < a.min_bars:
            continue
        used += 1
        dates = df["date"].astype(str).values
        close = df["close"].astype(float).values
        vol = df[a.field].astype(float).values
        n = len(close)
        fwd = np.full(n, np.nan)
        fwd[:-H] = close[H:] / close[:-H] - 1.0
        for N in WINDOWS:
            base = pd.Series(vol).rolling(N).mean().shift(1).values   # 前 N 日均量（不含当日）
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(base > 0, vol / base, np.nan)
            m = np.isfinite(ratio) & np.isfinite(fwd) & (ratio > 0)
            if m.sum():
                recs[N].append(pd.DataFrame({"date": dates[m], "code": code,
                                             "ratio": ratio[m], "fwd": fwd[m]}))
        if i % 100 == 0:
            print(f"  {i}/{len(codes)}", flush=True)

    print(f"可用 {used} 只", flush=True)
    rows_ic, rows_q = [], {}
    for N in WINDOWS:
        if not recs[N]:
            continue
        d = pd.concat(recs[N], ignore_index=True)
        d["date"] = d["date"].str.slice(0, 10)
        # 去掉同日大盘 beta：fwd 减当日全市场均值
        day_mean = d.groupby("date")["fwd"].transform("mean")
        d["ex"] = d["fwd"] - day_mean
        # 横截面 IC（Spearman）
        ic = []
        for dt, g in d.groupby("date"):
            if len(g) < 80:
                continue
            r = g["ratio"].rank()
            f = g["ex"].rank()
            if r.std() == 0 or f.std() == 0:
                continue
            ic.append(np.corrcoef(r, f)[0, 1])
        ic = np.array(ic)
        t = ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if len(ic) > 2 else np.nan
        rows_ic.append((N, len(ic), ic.mean(), t))
        # 分档超额
        d["q"] = pd.qcut(d["ratio"], 5, labels=False, duplicates="drop")
        qm = d.groupby("q")["ex"].mean() * 100
        hi = d[d["ratio"] >= 2.0]["ex"].mean() * 100
        lo = d[d["ratio"] <= 0.6]["ex"].mean() * 100
        rows_q[N] = (qm, hi, lo, len(d))
        print(f"N={N:2d}  天数{len(ic):4d}  IC均{ic.mean():+.4f}  t={t:+.2f}  "
              f"档位超额 {', '.join(f'{v:+.2f}' for v in qm.values)}  "
              f"量比≥2 {hi:+.2f}% / ≤0.6 {lo:+.2f}%", flush=True)

    # 报告
    lines = ["# 成交量均线窗口实验（2026-10-25）", "",
             f"> 数据：通达信本地日线（不复权）抽样 **{used} 只** × 约 5 年；"
             f"前向 {H} 日收益，减当日全市场均值去 beta；IC = 横截面 Spearman。", "",
             "| 窗口 N | 截面天数 | IC 均值 | t | 量比≥2 超额 | 量比≤0.6 超额 |",
             "|---:|---:|---:|---:|---:|---:|"]
    for N, nd, m, t in rows_ic:
        qm, hi, lo, _ = rows_q[N]
        lines.append(f"| {N} | {nd} | {m:+.4f} | {t:+.2f} | {hi:+.2f}% | {lo:+.2f}% |")
    lines.append("")
    lines.append("分档超额（相对当日全市场，%）——")
    lines.append("")
    lines.append("| 窗口 N | Q1(最低) | Q2 | Q3 | Q4 | Q5(最高) |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for N in WINDOWS:
        qm = rows_q[N][0]
        lines.append(f"| {N} | " + " | ".join(f"{v:+.2f}" for v in qm.values) + " |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("报告 →", OUT)


if __name__ == "__main__":
    main()
