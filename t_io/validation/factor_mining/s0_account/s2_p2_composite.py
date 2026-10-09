# -*- coding: utf-8 -*-
"""
S2-P2 复合增强臂 · 账户仿真对照（任务P3 step③）
================================================
基准臂：S1 终选 score_eq（原四因子等权 z：REV10/GAP/AMOUNT_CHG_inv/PRICE_POS60_inv）。
增强臂：score_p2 = 原四因子 z + P2 存活因子 z 等权（完备截面）。
  P2 存活因子（MC 显著 + 方向统一 + 去冗余 |rho|>0.7 后）：
    TURN20_inv（-TURN20，|IC_h5|=0.047）
    STR_inv（-STR，|IC_h5|=0.043）
    IND_MOM20_inv（-IND_MOM20，行业反转，|IC_h5|=0.035）
  剔除：GAP_cond_turn（与 GAP ρ=0.879）、PCT_TURN20（与 AMOUNT_CHG_inv ρ=0.921）、
        IND_BREADTH_inv（与 IND_MOM20_inv ρ=0.841，|IC| 更小）。
完备截面：7 因子全非 NaN 才入样（P2 快照已含 F3 四道过滤 + S1 卫生掩码，
故完备截面自动满足卫生宇宙；暖机段 P2 全 NaN → 增强臂天然无暖机伪影）。

仿真：S1 终选配置 N4/M8/H1/TP=A、费 0.0345%/边、回款当日可用（s1_sim.s1_run_sim）。
窗口：full=2024-03-05~2026-09-17（含暖机）、nowu=2024-06-03 起（剔暖机段）、
      h1/h2=2025-06-11 切半（同 S1 r3 切点）。

CLI：
  python s2_p2_composite.py --step scores          # 合成 score_p2 落盘
  python s2_p2_composite.py --step sim --arm base  # 基准臂 4 窗
  python s2_p2_composite.py --step sim --arm p2    # 增强臂 4 窗
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
FM_DIR = HERE.parent
sys.path.insert(0, str(FM_DIR))
sys.path.insert(0, str(HERE))

import s1_sim  # noqa: E402
from p2_factor_screen import FULLHIST_SNAP, load_core4  # noqa: E402

RES = HERE / "results"
OUT = Path(__file__).resolve().parents[1] / "results" / "s2_p2"
P2_SNAP = FM_DIR / "results" / "p2_screen" / "factors_p2_filtered.parquet"

CUT_DATE = pd.Timestamp("2025-06-11")
NOWU_DATE = pd.Timestamp("2024-06-03")
CFG = dict(n=4, m=8, min_hold=1, tp_arm="A")

# 增强臂因子（z 列名, 源, 符号）
P2_ADD = [("z_TURN20_inv", "TURN20", -1.0),
          ("z_STR_inv", "STR", -1.0),
          ("z_IND_MOM20_inv", "IND_MOM20", -1.0)]
CORE4 = [("z_REV10", "REV10", +1.0), ("z_GAP", "GAP", +1.0),
         ("z_AMT_INV", "AMOUNT_CHG", -1.0), ("z_POS_INV", "PRICE_POS60", -1.0)]
WINSOR_Q = 0.01


def _winsorize_z(x: np.ndarray) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    ok = np.isfinite(x)
    if ok.sum() < 30:
        return out
    v = x[ok]
    lo, hi = np.quantile(v, [WINSOR_Q, 1.0 - WINSOR_Q])
    v = np.clip(v, lo, hi)
    mu, sd = v.mean(), v.std()
    if sd <= 0 or not np.isfinite(sd):
        return out
    out[ok] = (v - mu) / sd
    return out


def step_scores() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    core = load_core4(since="2024-03-05")
    p2 = pd.read_parquet(P2_SNAP, columns=["date", "symbol", "TURN20", "STR", "IND_MOM20"])
    p2["date"] = pd.to_datetime(p2["date"])
    m = core.merge(p2, on=["date", "symbol"], how="inner")
    del core, p2
    src_all = [s for _, s, _ in CORE4] + [s for _, s, _ in P2_ADD]
    m = m.dropna(subset=src_all).reset_index(drop=True)
    print(f"[merge] 完备截面 {len(m)} 行, {m['date'].nunique()} 日 "
          f"({m['date'].min().date()} ~ {m['date'].max().date()})", flush=True)

    zcols = []
    for zc, src, sgn in CORE4 + P2_ADD:
        m[zc] = m[src].to_numpy(np.float64) * sgn
        zcols.append(zc)
    m = m.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
    gid = pd.factorize(m["date"], sort=True)[0]
    bounds = np.flatnonzero(np.r_[True, gid[1:] != gid[:-1]])
    bounds = np.r_[bounds, len(m)]
    for zc in zcols:
        arr = m[zc].to_numpy(np.float64)
        out = np.empty_like(arr)
        for s, e in zip(bounds[:-1], bounds[1:]):
            out[s:e] = _winsorize_z(arr[s:e])
        m[zc] = out
    m["score_p2"] = m[zcols].mean(axis=1)
    m[["symbol", "date", "score_p2"] + zcols].to_parquet(
        OUT / "scores_p2.parquet", index=False)
    print(f"[save] scores_p2.parquet {len(m)} 行 ({time.time()-t0:.0f}s)", flush=True)

    # 对齐 pivots 日期×代码轴 → float32 透视（与 s1_sim.load_data 同轴）
    with open(RES / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    dates = piv["close"].index
    cols = piv["close"].columns
    sc = m.pivot(index="date", columns="symbol", values="score_p2").reindex(
        index=dates, columns=cols).astype(np.float32)
    with open(OUT / "score_p2_pivot.pkl", "wb") as f:
        pickle.dump(sc, f, protocol=4)
    print(f"[save] score_p2_pivot.pkl {sc.shape} 非NaN日均 "
          f"{np.isfinite(sc.to_numpy()).sum(axis=1).mean():.0f}", flush=True)


def load_p2_data(arm: str = "p2") -> dict:
    with open(RES / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    with open(RES / "s1_hygiene_mask.pkl", "rb") as f:
        mask_df = pickle.load(f)
    scores_df = pd.read_parquet(RES / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns
    col_of = {i: c for i, c in enumerate(cols)}
    opn = piv["open"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    cls = piv["close"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    if arm == "p2":
        with open(OUT / "score_p2_pivot.pkl", "rb") as f:
            sc_p = pickle.load(f)
    elif arm == "p2w50":
        # 组间 50/50 灵敏度臂：core4 组与 P2 组各占半权（等权臂为 4:3 因子数权）
        sp = pd.read_parquet(OUT / "scores_p2.parquet")
        core_z = [c for c, _, _ in CORE4]
        p2_z = [c for c, _, _ in P2_ADD]
        sp["score_w50"] = 0.5 * sp[core_z].mean(axis=1) + 0.5 * sp[p2_z].mean(axis=1)
        with open(RES / "pivots.pkl", "rb") as f:
            piv0 = pickle.load(f)
        sc_p = sp.pivot(index="date", columns="symbol", values="score_w50").reindex(
            index=piv0["close"].index, columns=piv0["close"].columns).astype(np.float32)
    else:
        raise ValueError(arm)
    sc = sc_p.reindex(index=sdates, columns=cols).to_numpy(np.float64)
    mask = mask_df.reindex(index=sdates, columns=cols).fillna(False).to_numpy(bool)
    sc_clean = np.where(mask, sc, np.nan)      # 与基准臂同口径再套一道卫生（幂等）
    return dict(opn=opn, cls=cls, score=sc_clean, col_of=col_of, dates=sdates)


def windows(dates: pd.DatetimeIndex) -> dict:
    cut = int(dates.searchsorted(CUT_DATE))
    nw = int(dates.searchsorted(NOWU_DATE))
    return dict(full=slice(0, len(dates)), nowu=slice(nw, len(dates)),
                h1=slice(0, cut), h2=slice(cut, len(dates)))


def step_sim(arm: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if arm == "base":
        data = s1_sim.load_data("score_eq")
    else:
        data = load_p2_data(arm)
    dates = data["dates"]
    print(f"[sim:{arm}] {dates[0].date()} ~ {dates[-1].date()} ({time.time()-t0:.0f}s)",
          flush=True)
    recs = []
    for wname, sl in windows(dates).items():
        t1 = time.time()
        tag = f"s2p2_{arm}_N{CFG['n']}M{CFG['m']}H{CFG['min_hold']}{CFG['tp_arm']}_{wname}"
        res = s1_sim.s1_run_sim(
            tag, CFG["tp_arm"], data["score"][sl], data["opn"][sl], data["cls"][sl],
            data["col_of"], dates[sl], n_slots=CFG["n"], buffer_m=CFG["m"],
            min_hold=CFG["min_hold"], proceeds_lag=0)
        rec = s1_sim.metrics(res["nav"], fee_total=res["fee_total"],
                             rot_count=res["rot_count"], n_trades=res["n_trades"],
                             avg_exposure=res["avg_exposure"],
                             avg_slots=res["avg_slots"])
        rec.update(arm=arm, window=wname,
                   date_start=str(dates[sl][0].date()),
                   date_end=str(dates[sl][-1].date()), **CFG)
        res["nav"].to_csv(OUT / f"nav_{tag}.csv", encoding="utf-8-sig")
        recs.append(rec)
        print(f"  {wname}: ann={rec['ann_ret']:+.2%} sharpe={rec['sharpe']:.2f} "
              f"mdd={rec['max_dd']:+.2%} [{time.time()-t1:.0f}s]", flush=True)
    df = pd.DataFrame(recs)
    df.to_csv(OUT / f"metrics_{arm}.csv", index=False, encoding="utf-8-sig")
    print(f"[save] metrics_{arm}.csv 总耗时 {time.time()-t0:.0f}s", flush=True)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["scores", "sim"], required=True)
    ap.add_argument("--arm", choices=["base", "p2", "p2w50"], default="p2")
    args = ap.parse_args()
    if args.step == "scores":
        step_scores()
    else:
        step_sim(args.arm)


if __name__ == "__main__":
    main()
