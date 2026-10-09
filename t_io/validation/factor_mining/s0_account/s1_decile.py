# -*- coding: utf-8 -*-
"""
S1 步骤2 · 复合分十分位组评估（卫生宇宙 + 删失填充口径固化）
=============================================================
相对 s0_decile.py 的修正案差异（预注册 §S1）：
  ① 宇宙卫生：score 先经 s1_hygiene_mask.pkl 过滤（与 s1_sim 同一掩码）；
  ③ 删失填充：前瞻收益 buy_open_h 的 NaN 右删失（信号后停牌/退市）改为
     「持有期内最后可得 open 价填充」口径（S0 diag2 已验证 h5 填充后 alpha
     不变，本脚本将其固化为默认；面板尾部自然删失 open_t1 缺失的样本仍剔除）。
其余口径与 S0 一致：t 收盘分 → open(t+1) 买 → open(t+1+h) 卖；10 组；
费每边 0.0345%；多空两腿各一往返。

输出：results/s1_decile_summary.csv / results/s1_decile_nav_{name}_h{h}.csv
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"

FEE_SIDE = 0.000345
FEE_RT = 2 * FEE_SIDE
HORIZONS = (1, 3, 5)
SCORES = ["score_eq", "score_icir", "rev10_z"]
N_GROUPS = 10


def decile_for(score: np.ndarray, fwd: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
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
        pct = (np.arange(n) + 0.5) / n
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
    with open(OUT_DIR / "s1_hygiene_mask.pkl", "rb") as f:
        mask_df = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns

    open_p = piv["open"].reindex(index=sdates, columns=cols)
    opn = open_p.to_numpy(np.float64)
    open_t1 = np.roll(opn, -1, axis=0)
    open_t1[-1] = np.nan
    # 修正案③：持有期最后可得 open（ffill 后取 t+1+h 位置）
    opn_ff = open_p.ffill().to_numpy(np.float64)
    mask = mask_df.reindex(index=sdates, columns=cols).fillna(False).to_numpy(bool)

    rows = []
    for h in HORIZONS:
        fwd = np.roll(opn, -(1 + h), axis=0) / open_t1 - 1.0
        fwd[-(1 + h):] = np.nan
        fwd_fill = np.roll(opn_ff, -(1 + h), axis=0) / open_t1 - 1.0
        fwd_fill[-(1 + h):] = np.nan
        # 删失填充：fwd NaN 且 open_t1 可得（非面板尾部自然删失）→ 用最后可得价
        fwd_filled = np.where(np.isfinite(fwd), fwd, fwd_fill)
        n_cens = int((~np.isfinite(fwd) & np.isfinite(fwd_filled)).sum())
        print(f"[fill] h{h}: 右删失填充 {n_cens} 格 ({time.time()-t0:.0f}s)", flush=True)
        for name in SCORES:
            sc = piv[name].reindex(index=sdates, columns=cols).to_numpy(np.float64)
            sc = np.where(mask, sc, np.nan)          # 修正案①：卫生宇宙
            grp, ls = decile_for(sc, fwd_filled)
            means = np.nanmean(grp, axis=0)
            means_net = means - FEE_RT
            ls_g = np.nanmean(ls)
            ls_n = ls_g - 2 * FEE_RT
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
            nav = pd.DataFrame(grp, index=sdates,
                               columns=[f"D{i+1}" for i in range(N_GROUPS)])
            nav["LS_gross"] = ls
            nav["LS_nav_gross"] = nav_g
            nav["LS_nav_net"] = nav_n
            nav.to_csv(OUT_DIR / f"s1_decile_nav_{name}_h{h}.csv",
                       encoding="utf-8-sig")
            print(f"[s1_decile] {name} h{h}: D10-D1={ls_g:+.4%}/期 "
                  f"net={ls_n:+.4%} nav_net={nav_n[-1]:.2f} mono={row['mono']:+.2f} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    pd.DataFrame(rows).to_csv(OUT_DIR / "s1_decile_summary.csv",
                              index=False, encoding="utf-8-sig")
    print(f"[save] s1_decile_summary.csv 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
