# -*- coding: utf-8 -*-
"""S1 第三轮 step3: 汇总分半 + 高原判定
- 双半为正: half1.sharpe>0 且 half2.sharpe>0；否则「单极驱动」
- 高原: 同 N、|M格序差|<=1、|H格序差|<=1、TP 任意的 3x3x3 邻域(含自身) Sharpe 均值 >= 0.6
输出: results/s1_r3_halves_summary.csv, results/s1_r3_plateau.json
"""
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
RES = HERE / "results"

master = pd.read_csv(RES / "s1_grid_master.csv", encoding="utf-8-sig")
m_idx = {v: i for i, v in enumerate([6, 8, 12])}
h_idx = {v: i for i, v in enumerate([1, 2, 3, 5])}

# ── 分半汇总 ──
recs = [json.loads(l) for l in open(RES / "s1_r3_halves.jsonl", encoding="utf-8")]
half = pd.DataFrame(recs)
half["half"] = half["tag"].str.extract(r"half(\d)").astype(int)
rows = []
for base, g in half.groupby("base"):
    g = g.set_index("half")
    h1, h2 = g.loc[1], g.loc[2]
    rows.append(dict(
        tag=base,
        h1_ann=h1["ann_ret"], h1_sharpe=h1["sharpe"], h1_mdd=h1["max_dd"],
        h2_ann=h2["ann_ret"], h2_sharpe=h2["sharpe"], h2_mdd=h2["max_dd"],
        both_pos=bool(h1["sharpe"] > 0 and h2["sharpe"] > 0)))
summ = pd.DataFrame(rows).merge(
    master[["tag", "n", "m", "min_hold", "tp_arm", "sharpe", "ann_ret",
            "max_dd", "rot_count", "fee_total", "avg_exposure"]],
    on="tag").sort_values("sharpe", ascending=False)
summ["verdict"] = summ["both_pos"].map({True: "双半为正", False: "单极驱动"})
summ.to_csv(RES / "s1_r3_halves_summary.csv", index=False, encoding="utf-8-sig")

print("== 切半稳健性汇总（按全窗 Sharpe 排序）==")
print(summ[["tag", "sharpe", "h1_sharpe", "h2_sharpe", "verdict"]].to_string(index=False))

# ── 高原判定（仅对双半为正组合）──
def neighborhood(n, m, h):
    mi, hi = m_idx[m], h_idx[h]
    sub = master[(master["n"] == n)]
    out = []
    for _, r in sub.iterrows():
        if abs(m_idx[r["m"]] - mi) <= 1 and abs(h_idx[r["min_hold"]] - hi) <= 1:
            out.append(r["sharpe"])
    return out

plateau, isolated = [], []
for _, r in summ[summ["both_pos"]].iterrows():
    nb = neighborhood(int(r["n"]), int(r["m"]), int(r["min_hold"]))
    nb_mean = sum(nb) / len(nb)
    rec = dict(tag=r["tag"], n=int(r["n"]), m=int(r["m"]),
               min_hold=int(r["min_hold"]), tp_arm=r["tp_arm"],
               sharpe=r["sharpe"], h1_sharpe=r["h1_sharpe"],
               h2_sharpe=r["h2_sharpe"], nb_size=len(nb),
               nb_mean=round(nb_mean, 4),
               is_plateau=bool(nb_mean >= 0.6))
    (plateau if nb_mean >= 0.6 else isolated).append(rec)

print("\n== 高原区成员（邻域均值>=0.6）==")
for r in plateau:
    print(f"  {r['tag']}: sharpe={r['sharpe']:.3f} h1={r['h1_sharpe']:.2f} "
          f"h2={r['h2_sharpe']:.2f} nb_mean={r['nb_mean']:.3f} (n={r['nb_size']})")
print("\n== 孤峰（双半为正但邻域均值<0.6）==")
for r in isolated:
    print(f"  {r['tag']}: sharpe={r['sharpe']:.3f} h1={r['h1_sharpe']:.2f} "
          f"h2={r['h2_sharpe']:.2f} nb_mean={r['nb_mean']:.3f} (n={r['nb_size']})")

out = dict(split_date="2025-06-11", both_pos_rule="half1.sharpe>0 & half2.sharpe>0",
           plateau_rule="same N, |M_idx|<=1, |H_idx|<=1, any TP, mean(sharpe)>=0.6",
           n_both_pos=int(summ["both_pos"].sum()),
           n_single_pole=int((~summ["both_pos"]).sum()),
           plateau=plateau, isolated=isolated)
(RES / "s1_r3_plateau.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n-> results/s1_r3_halves_summary.csv + s1_r3_plateau.json")
