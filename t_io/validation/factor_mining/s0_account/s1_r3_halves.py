# -*- coding: utf-8 -*-
"""S1 第三轮 step2: 对 Sharpe>=0.75 组合做子时段分半稳健性
用法: python s1_r3_halves.py --start 0 --end 8   # 处理 sel075 列表的 [start,end) 个组合
结果追加落 results/s1_r3_halves.jsonl，净值落 results/s1_stress/nav_r3_*.csv
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
RES = HERE / "results"
OUT_JSONL = RES / "s1_r3_halves.jsonl"


def run_one(tag, cfg, data, sl, fee=None):
    old_fee = s1_sim.FEE
    if fee is not None:
        s1_sim.FEE = fee
    try:
        res = s1_sim.s1_run_sim(
            tag, cfg["tp_arm"],
            data["score"][sl], data["opn"][sl], data["cls"][sl],
            data["col_of"], data["dates"][sl],
            n_slots=int(cfg["n"]), buffer_m=int(cfg["m"]),
            min_hold=int(cfg["min_hold"]), proceeds_lag=0)
    finally:
        s1_sim.FEE = old_fee
    rec = s1_sim.metrics(res["nav"], fee_total=res["fee_total"],
                         rot_count=res["rot_count"], n_trades=res["n_trades"],
                         avg_exposure=res["avg_exposure"],
                         avg_slots=res["avg_slots"])
    rec.update(tag=tag, base=cfg["tag"],
               date_start=str(res["nav"].index[0].date()),
               date_end=str(res["nav"].index[-1].date()))
    nav_path = RES / "s1_stress" / f"nav_{tag}.csv"
    res["nav"].to_csv(nav_path, encoding="utf-8-sig")
    rec["nav_path"] = str(nav_path)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=99)
    args = ap.parse_args()

    sel = pd.read_csv(RES / "s1_r3_sel075.csv", encoding="utf-8-sig")
    sel = sel.iloc[args.start:args.end]
    data = s1_sim.load_data("score_eq")
    dates = data["dates"]
    mid = dates[0] + (dates[-1] - dates[0]) / 2
    cut = int(np.searchsorted(dates.values, np.datetime64(mid)))
    print(f"[halves] full {dates[0].date()}~{dates[-1].date()} "
          f"split@{dates[cut].date()} (idx {cut}/{len(dates)})", flush=True)

    with open(OUT_JSONL, "a", encoding="utf-8") as fout:
        for _, cfg in sel.iterrows():
            t0 = time.time()
            for i, sl in enumerate((slice(0, cut), slice(cut, len(dates))), 1):
                tag = f"s1_r3_half{i}_{cfg['tag']}"
                rec = run_one(tag, cfg, data, sl)
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                print(f"  {tag}: ann={rec['ann_ret']:+.2%} "
                      f"sharpe={rec['sharpe']:.2f} mdd={rec['max_dd']:+.2%}",
                      flush=True)
            print(f"[done] {cfg['tag']} ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
