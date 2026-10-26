# -*- coding: utf-8 -*-
"""收尾两问（2026-10-25）：① 量比阈值该定在哪（给 panel/tooltip 用）
② 负 alpha 是不是只在某一段时期成立（分年稳健性）。

用法：`python scripts/experiment/volume_ma_threshold_20261025.py --stocks 1200`
"""
import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parents[2]
SRC = BASE / "t_io" / "cache" / "daily_kline"
N = 60
H = 5


def load(limit, min_bars):
    files = sorted(glob.glob(str(SRC / "*.json")))
    if limit:
        rng = np.random.default_rng(20261025)
        files = [files[i] for i in rng.permutation(len(files))[:limit]]
    out = []
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        rows = d.get("rows") or []
        if len(rows) < min_bars:
            continue
        df = pd.DataFrame(rows)
        if not {"date", "close", "volume"}.issubset(df.columns):
            continue
        form = {str(x.get("date") if isinstance(x, dict) else x)
                for x in (d.get("forming") or [])}
        if form:
            df = df[~df["date"].astype(str).isin(form)]
        if len(df) < min_bars:
            continue
        out.append(df[["date", "close", "volume"]].assign(code=Path(f).stem))
    p = pd.concat(out, ignore_index=True)
    p["date"] = p["date"].astype(str).str.slice(0, 10)
    for c in ("close", "volume"):
        p[c] = p[c].astype(float)
    return p.sort_values(["code", "date"]).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=1200)
    ap.add_argument("--min-bars", type=int, default=400)
    a = ap.parse_args()
    p = load(a.stocks, a.min_bars)
    g = p.groupby("code", sort=False)
    p["fwd"] = g["close"].shift(-H) / p["close"] - 1.0
    p["ex"] = p["fwd"] - p.groupby("date")["fwd"].transform("mean")
    p["r"] = p["volume"] / g["volume"].transform(lambda s: s.rolling(N).mean().shift(1))
    d = p[np.isfinite(p["r"]) & np.isfinite(p["ex"]) & (p["r"] > 0)].copy()
    print(f"样本 {len(d):,} 行 · {d['code'].nunique()} 只 · {d['date'].nunique()} 日 · N={N}\n")

    print("== 量比(60日) 分布分位 ==")
    qs = [1, 5, 10, 25, 50, 75, 90, 95, 97.5, 99, 99.5]
    print("  " + "  ".join(f"P{q:g}={d['r'].quantile(q/100):.2f}" for q in qs))

    print("\n== 按量比分档的前向 5 日超额（%）==")
    edges = [0, 0.5, 0.7, 1.0, 1.3, 1.5, 2.0, 3.0, 99]
    d["b"] = pd.cut(d["r"], edges, right=False)
    for b, gg in d.groupby("b"):
        se = gg["ex"].std(ddof=1) / np.sqrt(len(gg))
        print(f"  {str(b):<12s} n={len(gg):>7,} 占比{len(gg)/len(d)*100:5.1f}%  "
              f"超额 {gg['ex'].mean()*100:+.3f}%  t {gg['ex'].mean()/se:+.1f}")

    print("\n== 分年 IC（Spearman，量比 vs 前向 5 日超额）==")
    d["yr"] = d["date"].str.slice(0, 4)
    for yr, gg in d.groupby("yr"):
        ics = []
        for _, g2 in gg.groupby("date", sort=False):
            if len(g2) < 80:
                continue
            a1, b1 = g2["r"].rank(), g2["ex"].rank()
            if a1.std() == 0 or b1.std() == 0:
                continue
            ics.append(np.corrcoef(a1, b1)[0, 1])
        if len(ics) < 20:
            continue
        ics = np.array(ics)
        t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))
        print(f"  {yr}  天数 {len(ics):4d}  IC {ics.mean():+.4f}  t {t:+.1f}")

    print("\n== 只看 2020 年以后 ==")
    sub = d[d["yr"] >= "2020"]
    ics = []
    for _, g2 in sub.groupby("date", sort=False):
        if len(g2) < 80:
            continue
        a1, b1 = g2["r"].rank(), g2["ex"].rank()
        if a1.std() == 0 or b1.std() == 0:
            continue
        ics.append(np.corrcoef(a1, b1)[0, 1])
    ics = np.array(ics)
    t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))
    print(f"  天数 {len(ics)}  IC {ics.mean():+.4f}  t {t:+.1f}")


if __name__ == "__main__":
    main()
