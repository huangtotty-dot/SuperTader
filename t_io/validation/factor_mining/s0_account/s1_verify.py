# -*- coding: utf-8 -*-
"""
S1 验证 · 修复臂复跑对照 + 卫生宇宙 EW 基线
============================================
对照对象（S0 已落盘结果）：
  - S0 修复预演 score_eq_C + <2元剔除 + 回款次日可用：ann +22.0% / mdd -48.8% / Sharpe 0.68
  - S0 EW 基线（未卫生宇宙）：ann +21.4%
S1 修复臂 = score_eq_C + 卫生宇宙(<2元 + 坠刀60/60) + 回款当日可用，
预注册通过界：ann_ret ∈ [+19%, +25%]（±3pp）。
本脚本：算卫生宇宙 EW 基线 + 汇总修复臂 run JSON → s1_verify_fixarm.json。
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
from s1_sim import metrics  # noqa: E402

FIX_RUN_JSON = OUT_DIR / "s1_runs" / "s1_eq_N4_M6_H3_C.json"
S0_FIX = dict(ann_ret=0.220, max_dd=-0.488, sharpe=0.68)   # S0 修复预演
PASS_LO, PASS_HI = 0.19, 0.25                              # ±3pp 通过界


def ew_baseline() -> dict:
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    with open(OUT_DIR / "s1_hygiene_mask.pkl", "rb") as f:
        mask_df = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns
    cls = piv["close"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    sc_ok = np.isfinite(piv["score_eq"].reindex(index=sdates, columns=cols)
                        .to_numpy(np.float64))
    mask = mask_df.reindex(index=sdates, columns=cols).fillna(False).to_numpy(bool)
    prev_cls = np.roll(cls, 1, axis=0)
    prev_cls[0] = np.nan
    dret = cls / prev_cls - 1.0
    ok = sc_ok & mask & np.isfinite(dret)
    ew = np.where(ok, dret, np.nan)
    with np.errstate(invalid="ignore"):
        ew_ret = np.nanmean(ew, axis=1)
    ew_ret[0] = 0.0
    ew_nav = pd.Series(np.cumprod(1.0 + np.nan_to_num(ew_ret)),
                       index=sdates, name="s1_ew_hygiened")
    nav_path = OUT_DIR / "s1_nav" / "nav_s1_ew_hygiened.csv"
    nav_path.parent.mkdir(parents=True, exist_ok=True)
    ew_nav.to_csv(nav_path, encoding="utf-8-sig")
    m = metrics(ew_nav)
    m["nav_path"] = str(nav_path)
    return m


def main() -> None:
    t0 = time.time()
    ew = ew_baseline()
    print(f"[ew] 卫生宇宙 EW: ann={ew['ann_ret']:+.2%} mdd={ew['max_dd']:+.2%} "
          f"sharpe={ew['sharpe']:.2f} ({time.time()-t0:.0f}s)", flush=True)

    fix = json.loads(FIX_RUN_JSON.read_text(encoding="utf-8"))
    delta_pp = (fix["ann_ret"] - S0_FIX["ann_ret"]) * 100
    passed = PASS_LO <= fix["ann_ret"] <= PASS_HI
    summary = dict(
        fix_arm=dict(tag=fix["tag"], ann_ret=fix["ann_ret"],
                     max_dd=fix["max_dd"], sharpe=fix["sharpe"],
                     final_nav=fix["final_nav"], win_rate=fix["win_rate"],
                     avg_exposure=fix["avg_exposure"],
                     n_trades=fix["n_trades"], fee_total=fix["fee_total"]),
        s0_fixpreview=S0_FIX,
        s1_ew_hygiened=ew,
        s0_ew_ann=0.214,
        delta_vs_s0fix_pp=delta_pp,
        pass_band=[PASS_LO, PASS_HI],
        passed=bool(passed),
    )
    out = OUT_DIR / "s1_verify_fixarm.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[verify] 修复臂 ann={fix['ann_ret']:+.2%} vs S0预演 +22.0% "
          f"(Δ{delta_pp:+.1f}pp) -> {'PASS' if passed else 'FAIL(需自查)'} "
          f"-> {out}", flush=True)


if __name__ == "__main__":
    main()
