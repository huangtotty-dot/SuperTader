# -*- coding: utf-8 -*-
"""
S0 诊断2 · 前瞻收益的右删失（停牌/退市）偏差
=============================================
F3/diag 的 buy_open_h = open(t+1+h)/open(t+1)-1 对「信号后停牌/退市」
样本为 NaN 被剔除 → 因子级 alpha 被高估；账户仿真卖不掉必须硬持，
期末无价的按 0 减值。本脚本量化删失规模与对 Top-4 alpha 的冲击：
  - rank1-4 样本中 fwd_h NaN（剔除面板尾部自然删失）占比
  - 两种填充界：censored=−100%（全损）/ censored=持有期内最后可得 open
输出：results/diag2_censor.csv
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
BUCKETS = [(1, 4), (5, 10), (11, 50)]
H = 5


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns

    opn = piv["open"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    n_days = opn.shape[0]
    open_t1 = np.roll(opn, -1, axis=0)
    open_t1[-1] = np.nan
    fwd = np.roll(opn, -(1 + H), axis=0) / open_t1 - 1.0
    fwd[-(1 + H):] = np.nan
    # 持有期最后可得 open（t+1 .. t+1+H 内最后一个非 NaN）
    last_open = np.full_like(opn, np.nan)
    run_last = np.full(opn.shape[1], np.nan)
    fwd_last = np.full(n_days, np.nan)  # placeholder
    # 逐日前向填充太烧钱，改为：对每列从 t+1 起 H 窗口内找最后非 NaN。
    # 简化向量化：ffill 后的 open 矩阵，取 t+1+H 位置的 ffill 值
    opn_df = pd.DataFrame(opn).ffill()
    opn_ff = opn_df.to_numpy()
    open_last = np.roll(opn_ff, -(1 + H), axis=0)
    open_last[-(1 + H):] = np.nan
    fwd_fill = open_last / open_t1 - 1.0        # 删失时用持有期最后可得价
    # 面板尾部自然删失（信号日太晚，所有人都没有 t+1+h）：用 open_t1 是否 NaN 界定样本
    natural_tail = np.isnan(open_t1)

    rows = []
    for sname in SCORES:
        sc = piv[sname].reindex(index=sdates, columns=cols).to_numpy(np.float64)
        rec = {"factor": sname, "h": H}
        for lo, hi in BUCKETS:
            gross, fill0, filllast, cens = [], [], [], 0
            tot = 0
            for d in range(n_days):
                s = sc[d]
                ok = np.isfinite(s) & ~natural_tail[d]
                n = ok.sum()
                if n < hi:
                    continue
                idx = np.flatnonzero(ok)
                order = np.argsort(-s[idx], kind="mergesort")
                pick = idx[order[lo - 1:hi]]
                f = fwd[d][pick]
                m = np.isfinite(f)
                gross.append(np.nanmean(f[m]) if m.any() else np.nan)
                tot += len(pick)
                cens += int((~m).sum())
                f0 = np.where(m, f, -1.0)                  # 全损界
                fl = np.where(m, f, fwd_fill[d][pick])     # 最后可得价界
                fill0.append(f0.mean())
                filllast.append(np.nanmean(fl))
            rec[f"r{lo}_{hi}_gross_naive"] = np.nanmean(gross)
            rec[f"r{lo}_{hi}_cens0"] = np.nanmean(fill0)
            rec[f"r{lo}_{hi}_censlast"] = np.nanmean(filllast)
            rec[f"r{lo}_{hi}_cens_pct"] = cens / max(tot, 1)
        rows.append(rec)
        print(f"[diag2] {sname}: r1-4 naive={rec['r1_4_gross_naive']:+.4%} "
              f"cens%={rec['r1_4_cens_pct']:.2%} "
              f"cens0={rec['r1_4_cens0']:+.4%} censlast={rec['r1_4_censlast']:+.4%} "
              f"({time.time()-t0:.0f}s)", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "diag2_censor.csv", index=False, encoding="utf-8-sig")
    print(f"[save] diag2_censor.csv 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
