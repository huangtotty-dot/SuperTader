# -*- coding: utf-8 -*-
"""
S0 诊断3 · 减值分解 + 宇宙卫生修复预演
=======================================
A. 各臂期末「卖不掉的持仓」分解：数量、买入成本、期末标记
   （close 为 NaN = 退市/长期停牌 → 按 0 减值，账户硬吃）
B. 修复预演：score_eq 臂C 主口径(lag1) + 信号日收盘 <2 元剔除
   （合法的宇宙卫生规则，无未来函数），验证坠刀减值是否可控
输出：results/diag3_writeoff.csv / results/metrics_fixpreview.csv
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"
sys.path.insert(0, str(HERE))
from s0_sim import run_sim, metrics, SCORES, ARMS   # noqa: E402


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns
    col_of = {i: c for i, c in enumerate(cols)}
    opn = piv["open"].reindex(index=sdates).to_numpy(np.float64)
    cls = piv["close"].reindex(index=sdates).to_numpy(np.float64)
    last_close = cls[-1]

    # ── A. 各主臂(lag1) 期末滞留持仓 ──
    rows = []
    for run in ["score_eq_C", "score_icir_C", "rev10_z_C"]:
        t = pd.read_csv(OUT_DIR / f"trades_{run}.csv")
        held = {}
        for _, r in t.iterrows():
            q = held.get(r["code"], 0)
            held[r["code"]] = q + r["qty"] if r["side"] == "buy" else q - r["qty"]
        stuck = {c: q for c, q in held.items() if q > 0}
        n_stuck = len(stuck)
        n_zero = sum(1 for c in stuck
                     if c not in set(cols) or not np.isfinite(
                         last_close[list(cols).index(c)]))
        rows.append(dict(run=run, stuck_positions=n_stuck,
                         stuck_no_final_px=n_zero))
        print(f"[A] {run}: 期末滞留 {n_stuck} 仓, 其中无期末价(按0减值) {n_zero} 仓",
              flush=True)
    pd.DataFrame(rows).to_csv(OUT_DIR / "diag3_writeoff.csv",
                              index=False, encoding="utf-8-sig")

    # ── B. 修复预演：低价票剔除（信号日 close < 2 元 → 分数置 NaN）──
    sc = piv["score_eq"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    sc_clean = np.where(cls >= 2.0, sc, np.nan)
    res = {}
    for tag, mat in (("base", sc), ("px2filter", sc_clean)):
        r = run_sim(f"eqC_{tag}", "C", mat, opn, cls, col_of, sdates,
                    proceeds_lag=1)
        res[tag] = metrics(r["nav"], r["fee_total"], r["rot_count"],
                           r["n_trades"], r["avg_exposure"], r["avg_slots"])
        res[tag]["run"] = f"score_eq_C_{tag}"
        r["trades"].to_csv(OUT_DIR / f"trades_score_eq_C_{tag}.csv",
                           index=False, encoding="utf-8-sig")
        contrib = pd.Series(r["flows"], name="contrib").sort_values(ascending=False)
        contrib.index.name = "code"
        contrib.to_csv(OUT_DIR / f"contrib_score_eq_C_{tag}.csv",
                       encoding="utf-8-sig")
        print(f"[B] {tag}: nav={res[tag]['final_nav']:.3f} "
              f"ann={res[tag]['ann_ret']:+.2%} mdd={res[tag]['max_dd']:+.2%} "
              f"sharpe={res[tag]['sharpe']:.2f} ({time.time()-t0:.0f}s)",
              flush=True)
    pd.DataFrame(res.values()).set_index("run").to_csv(
        OUT_DIR / "metrics_fixpreview.csv", encoding="utf-8-sig")
    print(f"[save] 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
