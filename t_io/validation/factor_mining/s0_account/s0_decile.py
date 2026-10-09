# -*- coding: utf-8 -*-
"""
S0 步骤2/3 · 复合分十分位组回测（buy_open 口径，h=1/3/5）
=========================================================
口径（与 F3 ic_layer.decile_analysis 对齐）：
  - t 日收盘分 → buy_open_h = open(t+1+h)/open(t+1)-1（T+1 开盘买，h 日后开盘卖）
  - 按日截面分 10 组（组号 1=分最低 .. 10=分最高），组均收益序列
  - 扣费：每边 0.0345%（往返 0.069%）；D10-D1 多空每腿各扣一个往返
输出：
  results/decile_summary.csv         各因子 × h 组均收益 / 多空 / 单调性
  results/decile_nav_{name}_h{h}.csv 组均收益与多空净值日序列
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"

FEE_SIDE = 0.000345          # 每边 0.0345%
FEE_RT = 2 * FEE_SIDE        # 往返 0.069%
HORIZONS = (1, 3, 5)
SCORES = ["score_eq", "score_icir", "rev10_z"]
N_GROUPS = 10


def decile_for(score: np.ndarray, fwd: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """score/fwd: dates × symbols（NaN 已对齐）。返回 (group_ret[d,10], ls[d])。"""
    n_days = score.shape[0]
    grp_ret = np.full((n_days, N_GROUPS), np.nan)
    for d in range(n_days):
        s = score[d]
        r = fwd[d]
        ok = np.isfinite(s) & np.isfinite(r)
        n = ok.sum()
        if n < 100:
            continue
        sv, rv = s[ok], r[ok]
        order = np.argsort(sv, kind="mergesort")
        pct = (np.arange(n) + 0.5) / n          # 秩百分位（ties 顺序任意，影响极小）
        g = np.minimum((pct * N_GROUPS).astype(np.int64), N_GROUPS - 1)
        gg = np.empty(n, dtype=np.int64)
        gg[order] = g
        cs = np.bincount(gg, minlength=N_GROUPS).astype(float)
        ss = np.bincount(gg, weights=rv, minlength=N_GROUPS)
        grp_ret[d] = np.where(cs > 0, ss / np.where(cs == 0, 1.0, cs), np.nan)
    ls = grp_ret[:, N_GROUPS - 1] - grp_ret[:, 0]
    return grp_ret, ls


def _spearman_mono(means: np.ndarray) -> float:
    ok = np.isfinite(means)
    if ok.sum() < 3:
        return float("nan")
    m = means[ok]
    r = pd.Series(m).rank().to_numpy()
    x = np.arange(1, N_GROUPS + 1, dtype=float)[ok]
    rc, xc = r - r.mean(), x - x.mean()
    den = float(np.sqrt((rc ** 2).sum() * (xc ** 2).sum()))
    return float((rc * xc).sum() / den) if den > 0 else float("nan")


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))

    open_p = piv["open"].reindex(sdates)
    opn = open_p.to_numpy(np.float64)
    open_t1 = np.roll(opn, -1, axis=0)
    open_t1[-1] = np.nan

    rows = []
    for h in HORIZONS:
        fwd = np.roll(opn, -(1 + h), axis=0) / open_t1 - 1.0
        fwd[-(1 + h):] = np.nan
        for name in SCORES:
            sc = piv[name].reindex(index=sdates, columns=open_p.columns).to_numpy(np.float64)
            grp, ls = decile_for(sc, fwd)
            means = np.nanmean(grp, axis=0)                 # 每期( h 日)毛收益
            means_net = means - FEE_RT                      # 扣往返
            ls_g = np.nanmean(ls)
            ls_n = ls_g - 2 * FEE_RT                        # 多空两腿各一往返
            nav_g = np.nancumprod(1.0 + np.nan_to_num(ls))
            nav_n = np.nancumprod(1.0 + np.nan_to_num(ls - 2 * FEE_RT))
            row = {"factor": name, "h": h, "n_days": int(np.isfinite(ls).sum())}
            for i in range(N_GROUPS):
                row[f"D{i+1}_gross"] = means[i]
                row[f"D{i+1}_net"] = means_net[i]
            row.update({
                "D10_D1_gross": ls_g, "D10_D1_net": ls_n,
                "ls_nav_gross": nav_g[-1], "ls_nav_net": nav_n[-1],
                "mono": _spearman_mono(means),
            })
            rows.append(row)
            # 组均收益与多空净值日序列落盘
            nav = pd.DataFrame(grp, index=sdates,
                               columns=[f"D{i+1}" for i in range(N_GROUPS)])
            nav["LS_gross"] = ls
            nav["LS_nav_gross"] = nav_g
            nav["LS_nav_net"] = nav_n
            nav.to_csv(OUT_DIR / f"decile_nav_{name}_h{h}.csv", encoding="utf-8-sig")
            print(f"[decile] {name} h{h}: D10-D1={ls_g:+.4%}/期 net={ls_n:+.4%} "
                  f"nav_net={nav_n[-1]:.2f} mono={row['mono']:+.2f} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    summ = pd.DataFrame(rows)
    summ.to_csv(OUT_DIR / "decile_summary.csv", index=False, encoding="utf-8-sig")
    print(f"[save] decile_summary.csv 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
