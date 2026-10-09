# -*- coding: utf-8 -*-
"""S1 第三轮 step1: 合并 108 个 run JSON -> results/s1_grid_master.csv"""
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
RUNS = HERE / "results" / "s1_runs"

rows = []
for p in sorted(RUNS.glob("s1_eq_N*.json")):
    d = json.loads(p.read_text(encoding="utf-8"))
    rows.append(dict(
        tag=d["tag"], n=d["n"], m=d["m"], min_hold=d["min_hold"],
        tp_arm=d["tp_arm"], score=d["score"],
        ann_ret=d["ann_ret"], max_dd=d["max_dd"], sharpe=d["sharpe"],
        win_rate=d["win_rate"], rot_count=d["rot_count"],
        fee_total=d["fee_total"], avg_exposure=d["avg_exposure"],
        avg_slots=d["avg_slots"], n_days=d["n_days"],
        final_nav=d["final_nav"]))

df = pd.DataFrame(rows).sort_values("sharpe", ascending=False).reset_index(drop=True)
out = HERE / "results" / "s1_grid_master.csv"
df.to_csv(out, index=False, encoding="utf-8-sig")
print(f"[master] {len(df)} rows -> {out}")
print("\nTop 15:")
print(df.head(15)[["tag", "ann_ret", "max_dd", "sharpe", "win_rate",
                   "rot_count", "fee_total", "avg_exposure"]].to_string(index=False))
sel = df[df["sharpe"] >= 0.75]
print(f"\n[select] sharpe>=0.75: {len(sel)} combos")
print(sel[["tag", "sharpe"]].to_string(index=False))
sel[["tag", "n", "m", "min_hold", "tp_arm", "sharpe"]].to_csv(
    HERE / "results" / "s1_r3_sel075.csv", index=False, encoding="utf-8-sig")
