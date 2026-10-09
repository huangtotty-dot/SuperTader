# -*- coding: utf-8 -*-
"""
S1 诊断 · 修复臂偏差点分解
============================
S0 修复预演 = <2元剔除 + lag1 → +22.0%
S1 修复臂   = 卫生宇宙(<2元+dd60) + lag0 → +15.6%（首轮）
2×2 分解定位偏差来源；并校验新引擎在「<2元+lag1」下复现 +22.0%（引擎等价性）。
输出：results/s1_diag_decompose.json
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"
sys.path.insert(0, str(HERE))
from s1_sim import s1_run_sim, metrics  # noqa: E402


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    with open(OUT_DIR / "s1_hygiene_mask.pkl", "rb") as f:
        mask_df = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns
    col_of = {i: c for i, c in enumerate(cols)}
    opn = piv["open"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    cls = piv["close"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    sc0 = piv["score_eq"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    mask_hyg = mask_df.reindex(index=sdates, columns=cols).fillna(False).to_numpy(bool)
    mask_px2 = cls >= 2.0

    out = {}
    for mtag, mk in (("px2only", mask_px2), ("hygiene", mask_hyg)):
        sc = np.where(mk, sc0, np.nan)
        for lag in (1, 0):
            tag = f"{mtag}_lag{lag}"
            r = s1_run_sim(tag, "C", sc, opn, cls, col_of, sdates,
                           n_slots=4, buffer_m=6, min_hold=3, proceeds_lag=lag)
            m = metrics(r["nav"], fee_total=r["fee_total"],
                        rot_count=r["rot_count"], n_trades=r["n_trades"],
                        avg_exposure=r["avg_exposure"], avg_slots=r["avg_slots"])
            contrib = pd.Series(r["flows"]).sort_values()
            m["worst3"] = {str(k): round(float(v), 0)
                           for k, v in contrib.head(3).items()}
            out[tag] = m
            print(f"[diag] {tag}: ann={m['ann_ret']:+.2%} mdd={m['max_dd']:+.2%} "
                  f"sharpe={m['sharpe']:.2f} rot={m['rot_count']} "
                  f"fee={m['fee_total']:,.0f} worst3={m['worst3']} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    (OUT_DIR / "s1_diag_decompose.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[save] s1_diag_decompose.json ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
