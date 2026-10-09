# -*- coding: utf-8 -*-
"""
S1 稳健性压测驱动（不改 s1_sim.py，调用层复用其函数）
=====================================================
基线：N4/M6/min_hold=3/tp=C, score_eq（ann +15.6% Sharpe 0.53）

  --test halves    ① 子时段分半：净值窗口按时间对半切两段，各独立起户跑一遍
  --test fee2x     ② 费用压力：每边费 0.0345% -> 0.069%（模块常量覆写），全窗重跑
  --test coverage  ③ 宇宙覆盖率日检：results/s1_hygiene_daily.csv 覆盖率<70% 时段

产物统一落 results/s1_stress/。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import s1_sim

HERE = Path(__file__).resolve().parent
STRESS_DIR = HERE / "results" / "s1_stress"

BASE = dict(n_slots=4, buffer_m=6, min_hold=3, arm="C", score="score_eq")


def run_one(tag: str, data: dict, sl: slice, fee: float | None = None) -> dict:
    """在 data 的日期切片 sl 上跑基线配置，返回指标 dict。"""
    old_fee = s1_sim.FEE
    if fee is not None:
        s1_sim.FEE = fee
    try:
        res = s1_sim.s1_run_sim(
            tag, BASE["arm"],
            data["score"][sl], data["opn"][sl], data["cls"][sl],
            data["col_of"], data["dates"][sl],
            n_slots=BASE["n_slots"], buffer_m=BASE["buffer_m"],
            min_hold=BASE["min_hold"], proceeds_lag=0)
    finally:
        s1_sim.FEE = old_fee
    rec = s1_sim.metrics(res["nav"], fee_total=res["fee_total"],
                         rot_count=res["rot_count"], n_trades=res["n_trades"],
                         avg_exposure=res["avg_exposure"],
                         avg_slots=res["avg_slots"])
    rec.update(tag=tag, date_start=str(res["nav"].index[0].date()),
               date_end=str(res["nav"].index[-1].date()))
    nav_path = STRESS_DIR / f"nav_{tag}.csv"
    res["nav"].to_csv(nav_path, encoding="utf-8-sig")
    rec["nav_path"] = str(nav_path)
    return rec


def test_halves() -> None:
    t0 = time.time()
    data = s1_sim.load_data(BASE["score"])
    dates = data["dates"]
    mid = dates[0] + (dates[-1] - dates[0]) / 2          # 按日历时长对半
    cut = int(np.searchsorted(dates.values, np.datetime64(mid)))
    recs = []
    for i, sl in enumerate((slice(0, cut), slice(cut, len(dates))), start=1):
        tag = f"s1_stress_half{i}_N4_M6_H3_C"
        rec = run_one(tag, data, sl)
        recs.append(rec)
        print(f"[halves] half{i} {rec['date_start']}~{rec['date_end']} "
              f"ann={rec['ann_ret']:+.2%} sharpe={rec['sharpe']:.2f} "
              f"mdd={rec['max_dd']:+.2%} expo={rec['avg_exposure']:.0%}", flush=True)
    out = dict(test="halves", split_rule="calendar midpoint",
               split_date=str(dates[cut].date()),
               baseline_full="s1_eq_N4_M6_H3_C (ann=+15.58% sharpe=0.53)",
               halves=recs)
    (STRESS_DIR / "s1_stress_halves.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[halves] done -> results/s1_stress/s1_stress_halves.json "
          f"({time.time()-t0:.0f}s)", flush=True)


def test_fee2x() -> None:
    t0 = time.time()
    data = s1_sim.load_data(BASE["score"])
    fee2 = s1_sim.FEE * 2.0
    rec = run_one("s1_stress_fee2x_N4_M6_H3_C", data,
                  slice(0, len(data["dates"])), fee=fee2)
    out = dict(test="fee2x", fee_per_side=fee2,
               baseline=dict(ann_ret=0.15584441662175652, sharpe=0.5335626676867969,
                             max_dd=-0.5616068826333112, fee_total=159929.11594495096,
                             final_nav=1426439.1175927042),
               fee2x=rec,
               ann_decay=rec["ann_ret"] - 0.15584441662175652)
    (STRESS_DIR / "s1_stress_fee2x.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fee2x] ann={rec['ann_ret']:+.2%} (baseline +15.58%, "
          f"decay {out['ann_decay']:+.2%}) sharpe={rec['sharpe']:.2f} "
          f"fee={rec['fee_total']:,.0f} -> results/s1_stress/s1_stress_fee2x.json "
          f"({time.time()-t0:.0f}s)", flush=True)


def test_coverage() -> None:
    df = pd.read_csv(HERE / "results" / "s1_hygiene_daily.csv",
                     encoding="utf-8-sig", parse_dates=["date"])
    df["coverage"] = df["n_clean"] / df["n_univ"]
    cov = df.set_index("date")["coverage"]
    bad = cov[cov < 0.70]
    # 连续段归并（gap>10 个日历日断开）
    segs = []
    if len(bad):
        start = prev = bad.index[0]
        for d in bad.index[1:]:
            if (d - prev).days > 10:
                segs.append((start, prev))
                start = d
            prev = d
        segs.append((start, prev))
    seg_recs = [dict(start=str(a.date()), end=str(b.date()),
                     n_days=int(((cov.index >= a) & (cov.index <= b)).sum()),
                     min_cov=float(cov.loc[a:b].min()),
                     mean_cov=float(cov.loc[a:b].mean()))
                for a, b in segs]
    out = dict(test="coverage", threshold=0.70,
               n_days=len(cov),
               date_start=str(cov.index[0].date()), date_end=str(cov.index[-1].date()),
               mean_cov=float(cov.mean()), min_cov=float(cov.min()),
               min_cov_date=str(cov.idxmin().date()),
               n_days_below=int(len(bad)),
               pct_days_below=float(len(bad) / len(cov)),
               segments=seg_recs)
    (STRESS_DIR / "s1_stress_coverage.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    cov.rename("coverage").to_csv(STRESS_DIR / "s1_coverage_daily.csv",
                                  encoding="utf-8-sig")
    print(f"[coverage] mean={cov.mean():.1%} min={cov.min():.1%} @"
          f"{cov.idxmin().date()} days<70%: {len(bad)}/{len(cov)} "
          f"({len(bad)/len(cov):.1%})", flush=True)
    for s in seg_recs:
        print(f"  seg {s['start']}~{s['end']} n={s['n_days']}d "
              f"min={s['min_cov']:.1%} mean={s['mean_cov']:.1%}", flush=True)
    print("[coverage] -> results/s1_stress/s1_stress_coverage.json + "
          "s1_coverage_daily.csv", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", required=True,
                    choices=["halves", "fee2x", "coverage"])
    args = ap.parse_args()
    STRESS_DIR.mkdir(parents=True, exist_ok=True)
    dict(halves=test_halves, fee2x=test_fee2x, coverage=test_coverage)[args.test]()


if __name__ == "__main__":
    main()
