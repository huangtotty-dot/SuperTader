# -*- coding: utf-8 -*-
"""
S0 诊断 · 极端尾部 vs 机制损耗
==============================
诊断1（本脚本）：按 t 日收盘 rank 分桶的前瞻 buy_open 收益
  桶：rank 1-4 / 5-10 / 11-50 / 51-500 / 全宇宙均值
  回答：Top-4 极端尾部是否延续了十分位 D10 的正 alpha，
       还是极端尾部本身失效（排名噪声/坠刀）？
输出：results/diag_rank_buckets.csv
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"
SCORES = ["score_eq", "score_icir", "rev10_z"]
BUCKETS = [(1, 4), (5, 10), (11, 50), (51, 500)]
HORIZONS = (1, 5)


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns

    opn = piv["open"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    open_t1 = np.roll(opn, -1, axis=0)
    open_t1[-1] = np.nan

    rows = []
    for h in HORIZONS:
        fwd = np.roll(opn, -(1 + h), axis=0) / open_t1 - 1.0
        fwd[-(1 + h):] = np.nan
        for sname in SCORES:
            sc = piv[sname].reindex(index=sdates, columns=cols).to_numpy(np.float64)
            # 全宇宙均值（有分数且有前瞻收益）
            ok0 = np.isfinite(sc) & np.isfinite(fwd)
            uni = np.nanmean(np.where(ok0, fwd, np.nan), axis=1)
            rec = {"factor": sname, "h": h,
                   "universe": np.nanmean(uni)}
            # 分桶：按日取 rank 区间内的前瞻收益均值
            for lo, hi in BUCKETS:
                daily = np.full(sc.shape[0], np.nan)
                for d in range(sc.shape[0]):
                    s = sc[d]
                    ok = np.isfinite(s) & np.isfinite(fwd[d])
                    n = ok.sum()
                    if n < hi:
                        continue
                    idx = np.flatnonzero(ok)
                    order = np.argsort(-s[idx], kind="mergesort")
                    pick = idx[order[lo - 1:hi]]
                    daily[d] = fwd[d][pick].mean()
                rec[f"rank{lo}_{hi}"] = np.nanmean(daily)
            rows.append(rec)
            print(f"[diag] {sname} h{h}: "
                  + " ".join(f"r{lo}-{hi}={rec[f'rank{lo}_{hi}']:+.4%}" for lo, hi in BUCKETS)
                  + f" uni={rec['universe']:+.4%} ({time.time()-t0:.0f}s)", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "diag_rank_buckets.csv", index=False, encoding="utf-8-sig")
    print(f"[save] diag_rank_buckets.csv 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
