# -*- coding: utf-8 -*-
"""把「量比」拆成 短期异常(E) × 长期量能状态(S)，看 IC 到底来自哪一半（2026-10-25）。

背景：v2 实验里 IC 随窗口 N 单调变强直到 N=250（IC5 -0.018 → -0.086），
这有两种可能，必须分清，否则「用多少日均量」会选错：
  (a) 今日异常放量本身有负 alpha  ⇒ 该用**短窗**当基准（基准不能被异动本身抬高）
  (b) 长期换手水平高的股票本来就跑输 ⇒ 长窗只是"状态"变量，与今日异动无关

分解（对数口径，可加）：
    log(vol_t / MA120_t) = log(vol_t / MA20_t) + log(MA20_t / MA120_t)
            └─ 总量 T ─┘   └──── E（短期异常）────┘   └─── S（量能状态）───┘

再按流动性五档（60 日均成交额代理）分层，排除「小票高换手」这个混杂。

用法：`python scripts/experiment/volume_ma_decompose_20261025.py --stocks 1500`
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


def ic_by_day(d, xcol, ycol, min_n=80):
    ics = []
    for _, g in d.groupby("date", sort=False):
        if len(g) < min_n:
            continue
        a, b = g[xcol].rank(), g[ycol].rank()
        if a.std() == 0 or b.std() == 0:
            continue
        ics.append(np.corrcoef(a, b)[0, 1])
    ics = np.array(ics)
    if len(ics) < 3:
        return np.nan, np.nan, 0
    t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))
    return ics.mean(), t, len(ics)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=1500)
    ap.add_argument("--min-bars", type=int, default=400)
    a = ap.parse_args()
    p = load(a.stocks, a.min_bars)
    g = p.groupby("code", sort=False)
    p["fwd"] = g["close"].shift(-H) / p["close"] - 1.0
    p["ex"] = p["fwd"] - p.groupby("date")["fwd"].transform("mean")
    ma20 = g["volume"].transform(lambda s: s.rolling(20).mean().shift(1))
    ma120 = g["volume"].transform(lambda s: s.rolling(120).mean().shift(1))
    p["E"] = np.log(p["volume"] / ma20)
    p["S"] = np.log(ma20 / ma120)
    p["T"] = np.log(p["volume"] / ma120)
    # 流动性/规模代理：60 日均成交额（额≈量×价，只用相对分档）
    p["liq"] = g.apply(lambda x: (x["volume"] * x["close"]).rolling(60).mean().shift(1)
                       ).reset_index(level=0, drop=True)

    d = p[np.isfinite(p["T"]) & np.isfinite(p["ex"]) & np.isfinite(p["liq"])].copy()
    d["lq"] = d.groupby("date")["liq"].transform(lambda s: pd.qcut(s, 5, labels=False,
                                                                   duplicates="drop"))
    print(f"样本 {len(d):,} 行 · {d['code'].nunique()} 只 · {d['date'].nunique()} 日\n")

    print("== 全样本 IC（vs 5 日前向超额）==")
    for col, name in (("E", "E 短期异常 vol/MA20"), ("S", "S 量能状态 MA20/MA120"),
                      ("T", "T 合计 vol/MA120")):
        m, t, nd = ic_by_day(d, col, "ex")
        print(f"  {name:28s} IC {m:+.4f}  t {t:+.1f}  天数 {nd}")

    print("\n== E 的 IC 在 S 五档内（控制长期量能状态）==")
    d["sq"] = d.groupby("date")["S"].transform(lambda s: pd.qcut(s, 5, labels=False,
                                                                 duplicates="drop"))
    for q in sorted(d["sq"].dropna().unique()):
        sub = d[d["sq"] == q]
        m, t, nd = ic_by_day(sub, "E", "ex")
        print(f"  S 第{int(q)+1}档   IC(E) {m:+.4f}  t {t:+.1f}  天数 {nd}")

    print("\n== S 的 IC 在 E 五档内（控制今日异常）==")
    d["eq"] = d.groupby("date")["E"].transform(lambda s: pd.qcut(s, 5, labels=False,
                                                                 duplicates="drop"))
    for q in sorted(d["eq"].dropna().unique()):
        sub = d[d["eq"] == q]
        m, t, nd = ic_by_day(sub, "S", "ex")
        print(f"  E 第{int(q)+1}档   IC(S) {m:+.4f}  t {t:+.1f}  天数 {nd}")

    print("\n== 流动性五档内，E / S / T 的 IC（排除小票高换手混杂）==")
    print("  档 |      IC(E)        |      IC(S)        |      IC(T)")
    for q in sorted(d["lq"].dropna().unique()):
        sub = d[d["lq"] == q]
        ce = ic_by_day(sub, "E", "ex")
        cs = ic_by_day(sub, "S", "ex")
        ct = ic_by_day(sub, "T", "ex")
        print(f"  {int(q)+1}  | {ce[0]:+.4f} (t{ce[1]:+.1f}) | "
              f"{cs[0]:+.4f} (t{cs[1]:+.1f}) | {ct[0]:+.4f} (t{ct[1]:+.1f})")

    print("\n== 按 T 五档的日均超额（%)（便于直接读「今天量比多少 → 未来 5 日）==")
    d["tq"] = d.groupby("date")["T"].transform(lambda s: pd.qcut(s, 5, labels=False,
                                                                 duplicates="drop"))
    for q, gg in d.groupby("tq"):
        print(f"  T 第{int(q)+1}档  n={len(gg):>7,}  超额 {gg['ex'].mean()*100:+.3f}%")


if __name__ == "__main__":
    main()
