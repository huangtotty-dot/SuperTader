# -*- coding: utf-8 -*-
"""「偏离 365 日线」的超买 / 超卖阈值实验（2026-10-24 owner：
「做下实验，当偏离365日均线多大属于超买或者超卖，如果出现用特殊标记进行提醒」）。

## 问题

`dev365 = (收盘 − MA365) / MA365 × 100`。多大算过热/超跌？**用前向收益定阈值**，不拍脑袋。

## 口径（沿用本仓研究铁律）

* 进场 = **次日开盘**；出场 = T+h 收盘（h=1/3/5/10）
* 超额 = 减**同日全样本**同口径均值（横截面去市场）；t 按**日期聚类**
* 分年看稳定；同时看未来 5 日振幅（区分"方向"和"波动"）
* 数据 = 本地日线缓存（800 根 ⇒ 每只可测约 420 个含 MA365 的交易日）

用法：python scripts/experiment/ma365_dev_extremes_20261024.py [--stocks 2000] [--procs 3]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
DAILY = BASE / "t_io" / "cache" / "daily_kline"
OUT_MD = BASE / "doc" / "research" / "2026-10-24_偏离365日线_超买超卖阈值实验.md"
OUT_PQ = BASE / "t_io" / "cache" / "dev365_study.parquet"

HOLDS = (1, 3, 5, 10)
BUCKETS = [(-999, -40), (-40, -30), (-30, -20), (-20, -10), (-10, 0),
           (0, 10), (10, 20), (20, 30), (30, 40), (40, 60), (60, 999)]


def _scan_one(code):
    import pandas as pd
    try:
        blob = json.loads((DAILY / f"{code}.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = blob.get("rows") or []
    forming = set(str(x)[:10] for x in (blob.get("forming") or []))
    if forming:
        rows = [r for r in rows if str(r["date"])[:10] not in forming]
    n = len(rows)
    if n < 400:
        return []
    df = pd.DataFrame(rows)
    df["date"] = df["date"].astype(str).str[:10]
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    dates = df["date"].tolist()

    out = []
    for t in range(365, n - 11):
        if not (c[t] > 0):
            continue
        ma = float(np.mean(c[t - 364:t + 1]))
        if not (ma > 0):
            continue
        dev = (c[t] - ma) / ma * 100.0
        entry = o[t + 1]
        if not (entry > 0):
            continue
        r = {k: (c[t + k] / entry - 1.0) if t + k < n else None for k in HOLDS}
        hi = float(np.max(h[t + 1:t + 6])) if t + 5 < n else np.nan
        lo = float(np.min(l[t + 1:t + 6])) if t + 5 < n else np.nan
        out.append({
            "code": code, "date": dates[t], "dev": round(dev, 2),
            "r1": r[1], "r3": r[3], "r5": r[5], "r10": r[10],
            "amp5": (hi - lo) / c[t] * 100.0 if np.isfinite(hi) and np.isfinite(lo) else None,
        })
    return out


def _t_by_date(df, col):
    g = df.groupby("date")[col].mean().dropna()
    if len(g) < 5:
        return float("nan"), len(g)
    sd = g.std(ddof=1)
    return (float(g.mean() / (sd / np.sqrt(len(g)))), len(g)) if sd else (float("nan"), len(g))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=2000)
    ap.add_argument("--procs", type=int, default=3)
    a = ap.parse_args()

    codes = sorted(p.stem for p in DAILY.glob("*.json")
                   if p.stem.isdigit() and len(p.stem) == 6)
    if a.stocks and len(codes) > a.stocks:
        step = len(codes) / a.stocks
        codes = [codes[int(i * step)] for i in range(a.stocks)]
    print(f"样本 {len(codes)} 只 · procs={a.procs}", flush=True)

    from concurrent.futures import ProcessPoolExecutor
    rows = []
    with ProcessPoolExecutor(max_workers=a.procs) as ex:
        for i, part in enumerate(ex.map(_scan_one, codes, chunksize=16)):
            rows.extend(part)
            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(codes)} · {len(rows)} 行", flush=True)

    import pandas as pd
    df = pd.DataFrame(rows)
    cnt = df.groupby("date")["code"].nunique()
    _min_n = max(50, int(0.15 * df["code"].nunique()))
    df = df[df["date"].isin(set(cnt[cnt >= _min_n].index))].copy()
    print(f"\n样本 {len(df)} 股票·日 · {df['date'].nunique()} 天 · "
          f"{df['date'].min()} ~ {df['date'].max()} · {df['code'].nunique()} 只", flush=True)

    for k in HOLDS:
        df[f"x{k}"] = df[f"r{k}"] - df.groupby("date")[f"r{k}"].transform("mean")
    df.to_parquet(OUT_PQ, index=False)

    def summ(name, d):
        if len(d) < 30:
            return None
        o = {"组": name, "n": len(d)}
        for k in HOLDS:
            o[f"x{k}"] = round(d[f"x{k}"].mean() * 100, 3)
        t5, _ = _t_by_date(d, "x5")
        o["t5"] = None if t5 != t5 else round(t5, 2)
        o["胜率5"] = round((d["r5"] > 0).mean() * 100, 1)
        o["振幅5"] = round(d["amp5"].mean(), 2) if d["amp5"].notna().any() else None
        return o

    def line(o):
        return (f"| {o['组']} | {o['n']} | {o.get('x1', '—')}% | {o.get('x3', '—')}% | "
                f"{o.get('x5', '—')}% | {o.get('x10', '—')}% | {o.get('t5', '—')} | "
                f"{o.get('胜率5', '—')}% | {o.get('振幅5', '—')}% |")

    L = []
    def W(s=""):
        print(s, flush=True)
        L.append(s)

    hdr = ("| 组 | n | T+1 | T+3 | T+5 | T+10 | t(T+5) | 胜率5 | 未来5日振幅 |\n"
           "|---|---|---|---|---|---|---|---|---|")
    W("# 偏离 365 日线：超买 / 超卖阈值实验（2026-10-24）\n")
    W(f"* 样本 **{len(df)}** 股票·日 · {df['date'].nunique()} 天 · {df['code'].nunique()} 只 · "
      f"{df['date'].min()} ~ {df['date'].max()}")
    W("* 进场=次日开盘；超额=减同日全样本均值；t 按日期聚类\n")

    W("## 一、按偏离度分档（超额 = 减同日全市场）\n")
    W(hdr)
    W(line(summ("全样本", df)))
    for lo, hi in BUCKETS:
        d = df[(df["dev"] >= lo) & (df["dev"] < hi)]
        o = summ(f"[{lo if lo > -999 else '−∞'}, {hi if hi < 999 else '+∞'})%", d)
        if o:
            W(line(o))
    W("")

    W("## 二、尾部细分（找阈值）\n")
    W(hdr)
    for thr in (20, 30, 40, 50, 60, 80):
        W(line(summ(f"偏离 ≥ +{thr}%（超买候选）", df[df["dev"] >= thr])))
    for thr in (-10, -20, -30, -40, -50):
        W(line(summ(f"偏离 ≤ {thr}%（超卖候选）", df[df["dev"] <= thr])))
    W("")

    W("## 三、分年稳定性（挑出的阈值）\n")
    W("| 年 | ≥+40% n / T+5 | ≤−20% n / T+5 | 全样本 T+5 |")
    W("|---|---|---|---|")
    yr = pd.to_datetime(df["date"]).dt.year
    for y in sorted(set(yr)):
        m = yr == y
        a = df.loc[m & (df["dev"] >= 40), "x5"].dropna()
        b = df.loc[m & (df["dev"] <= -20), "x5"].dropna()
        c5 = df.loc[m, "x5"].dropna()
        if len(a) < 20 and len(b) < 20:
            continue
        W(f"| {y} | {len(a)} / {a.mean() * 100:+.2f}% | {len(b)} / {b.mean() * 100:+.2f}% | "
          f"{c5.mean() * 100:+.2f}% |")
    W("")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\n写入 {OUT_MD}\n落盘 {OUT_PQ.name}")


if __name__ == "__main__":
    main()
