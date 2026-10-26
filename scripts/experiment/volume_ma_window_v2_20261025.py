# -*- coding: utf-8 -*-
"""成交量均线窗口 v2（2026-10-25）：改用**前复权**日线，并把窗口拉长到 250 日。

v1 (`volume_ma_window_20261025.py`) 用的是 tdx 全市场 parquet，那份是**不复权**的，
5 日/20 日前向收益会被送转除权打出假跌停 ⇒ 结论方向可信、量级可疑。
本轮改读 `t_io/cache/daily_kline/*.json`（腾讯前复权，全市场 ~5500 只），
并回答三个 v1 没回答的问题：

  Q1 窗口继续拉长（120/250）会比 60 更强吗？——判断「放量负 alpha」到底是
     **当日异常放量**造成的，还是**长期高换手股本来就跑输**（慢变量效应）。
  Q2 放量的**方向**：放量+上涨 vs 放量+下跌，前向收益差多少？这直接对应
     owner 要的「主力吸筹 / 出货」读法。
  Q3 放量是不是一次性事件：ratio≥2 之后 1~5 日还站在基准上的比例（持续性），
     以及基准自己会不会被这根放量拖高（自我污染）。

用法：`python scripts/experiment/volume_ma_window_v2_20261025.py [--stocks 0=全部]`
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
WINDOWS = (5, 10, 20, 30, 60, 120, 250)
HORIZONS = (5, 20)


def load_panel(limit: int, min_bars: int):
    files = sorted(glob.glob(str(SRC / "*.json")))
    if limit:
        rng = np.random.default_rng(20261025)
        files = [files[i] for i in rng.permutation(len(files))[:limit]]
    frames, used = [], 0
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        rows = d.get("rows") or []
        if len(rows) < min_bars:
            continue
        code = Path(f).stem
        df = pd.DataFrame(rows)
        if not {"date", "close", "volume"}.issubset(df.columns):
            continue
        # forming bar（盘中半成品根）不是收盘，剔掉
        _form = {str(x.get("date") if isinstance(x, dict) else x)
                 for x in (d.get("forming") or [])}
        if _form:
            df = df[~df["date"].astype(str).isin(_form)]
        if len(df) < min_bars:
            continue
        used += 1
        frames.append(df[["date", "close", "volume"]].assign(code=code))
    print(f"载入 {used} 只（>={min_bars} 根）", flush=True)
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = panel["date"].astype(str).str.slice(0, 10)
    panel["close"] = panel["close"].astype(float)
    panel["volume"] = panel["volume"].astype(float)
    panel = panel.sort_values(["code", "date"]).reset_index(drop=True)
    return panel


def build(panel):
    g = panel.groupby("code", sort=False)
    panel["ret1"] = g["close"].pct_change()
    for h in HORIZONS:
        panel[f"fwd{h}"] = g["close"].shift(-h) / panel["close"] - 1.0
    for n in WINDOWS:
        base = g["volume"].transform(lambda s: s.rolling(n).mean().shift(1))
        panel[f"r{n}"] = panel["volume"] / base
    return panel


def demean_by_date(d, col):
    return d[col] - d.groupby("date")[col].transform("mean")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=0, help="0 = 全市场")
    ap.add_argument("--min-bars", type=int, default=400)
    a = ap.parse_args()

    panel = build(load_panel(a.stocks, a.min_bars))
    print(f"样本行 {len(panel):,} · 日期 {panel['date'].nunique()} · "
          f"{panel['date'].min()} ~ {panel['date'].max()}", flush=True)

    # 去当日全市场 beta（只做相对比较）
    for h in HORIZONS:
        panel[f"ex{h}"] = demean_by_date(panel, f"fwd{h}")

    rows_ic, rows_q, rows_dir, rows_persist = [], {}, {}, {}
    for n in WINDOWS:
        r = f"r{n}"
        d = panel[np.isfinite(panel[r]) & (panel[r] > 0)].copy()
        # ---- Q: 横截面 IC（Spearman，按日）----
        line = {"N": n, "days": 0}
        for h in HORIZONS:
            dd = d[np.isfinite(d[f"ex{h}"])]
            ics = []
            for dt, g in dd.groupby("date", sort=False):
                if len(g) < 80:
                    continue
                a1, b1 = g[r].rank(), g[f"ex{h}"].rank()
                if a1.std() == 0 or b1.std() == 0:
                    continue
                ics.append(np.corrcoef(a1, b1)[0, 1])
            ics = np.array(ics)
            t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 2 else np.nan
            line[f"ic{h}"], line[f"t{h}"], line["days"] = ics.mean(), t, len(ics)
        rows_ic.append(line)

        # ---- 分档超额（fwd5）----
        d5 = d[np.isfinite(d["ex5"])]
        d5 = d5.assign(q=pd.qcut(d5[r], 5, labels=False, duplicates="drop"))
        rows_q[n] = d5.groupby("q")["ex5"].mean() * 100

        # ---- 放量的方向：ratio 高时，按当日涨跌分（fwd5）----
        hi = d5[d5[r] >= 1.5]
        up, dn = hi[hi["ret1"] > 0]["ex5"].mean(), hi[hi["ret1"] <= 0]["ex5"].mean()
        rows_dir[n] = (
            len(hi), up * 100 if np.isfinite(up) else np.nan,
            dn * 100 if np.isfinite(dn) else np.nan,
        )

        # ---- 持续性：ratio>=2 之后 1~5 日仍 >=1.5 的比例 ----
        for step in (1, 2, 3, 5):
            nxt = f"r{n}"
            col = panel.groupby("code")[nxt].shift(-step)
            m = (panel[nxt] >= 2.0) & np.isfinite(col)
            rows_persist.setdefault(n, {})[step] = float((col[m] >= 1.5).mean())

        print(f"N={n:3d}  IC5 {line['ic5']:+.4f} (t {line['t5']:+.1f})  "
              f"IC20 {line['ic20']:+.4f} (t {line['t20']:+.1f})  "
              f"放量↑ {rows_dir[n][1]:+.2f}%  放量↓ {rows_dir[n][2]:+.2f}%", flush=True)

    # ---- 窗口一致性：ratio>=2 的日期集合互相重合多少 ----
    print("\n窗口一致性：ratio>=2 的日子，两个窗口都算放量的比例")
    sets = {}
    for n in WINDOWS:
        m = panel[f"r{n}"] >= 2.0
        sets[n] = set(map(tuple, panel.loc[m, ["code", "date"]].values))
    for i, a1 in enumerate(WINDOWS):
        if a1 not in (20, 60):
            continue
        parts = []
        for b1 in WINDOWS:
            if b1 == a1:
                continue
            inter = len(sets[a1] & sets[b1])
            parts.append(f"N{b1}:{inter / max(len(sets[a1]), 1) * 100:.0f}%")
        print(f"  N={a1}: " + "  ".join(parts))

    # ---- 输出 markdown 表 ----
    print("\n| 窗口 N | IC5 | t5 | IC20 | t20 | Q1 | Q3 | Q5 | 放量↑超额 | 放量↓超额 |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ln in rows_ic:
        n = ln["N"]
        q = rows_q[n]
        print(f"| {n} | {ln['ic5']:+.4f} | {ln['t5']:+.1f} | {ln['ic20']:+.4f} | "
              f"{ln['t20']:+.1f} | {q.get(0, np.nan):+.2f} | {q.get(2, np.nan):+.2f} | "
              f"{q.get(4, np.nan):+.2f} | {rows_dir[n][1]:+.2f}% | {rows_dir[n][2]:+.2f}% |")

    print("\n放量持续性：ratio>=2 之后第 k 日 ratio 仍 >=1.5 的比例")
    print("| N | +1日 | +2日 | +3日 | +5日 |")
    print("|---:|---:|---:|---:|---:|")
    for n in WINDOWS:
        p = rows_persist[n]
        print(f"| {n} | {p[1]*100:.1f}% | {p[2]*100:.1f}% | {p[3]*100:.1f}% | {p[5]*100:.1f}% |")


if __name__ == "__main__":
    main()
