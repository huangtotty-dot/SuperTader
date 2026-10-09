# -*- coding: utf-8 -*-
"""S1 第三轮 step4: 高原 Top3 终选复跑
- 剔除暖机伪影：仿真窗口从 2024-06-03 起（2024-03-05~05-31 共 59 天 dd60 暖机段
  宇宙覆盖 0%、全程空仓；卫生 mask 为 s1_prep 全历史预计算，窗口切割不影响 mask，
  故窗口切片 ≡ 净值层裁剪，指标口径=切片后窗口全量重算）
- 每组合跑 fee x1 / fee x2 两版
输出: results/s1_r3_final.json + results/s1_stress/nav_r3_final_*.csv
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import s1_sim

HERE = Path(__file__).resolve().parent
RES = HERE / "results"

TOP3 = [
    dict(tag="s1_eq_N4_M8_H1_A", n=4, m=8, min_hold=1, tp_arm="A"),
    dict(tag="s1_eq_N4_M12_H2_C", n=4, m=12, min_hold=2, tp_arm="C"),
    dict(tag="s1_eq_N4_M8_H1_B", n=4, m=8, min_hold=1, tp_arm="B"),
]
WARMUP_END = pd.Timestamp("2024-05-31")


def run(cfg, data, sl, fee_mult):
    old_fee = s1_sim.FEE
    s1_sim.FEE = s1_sim.FEE * fee_mult
    try:
        res = s1_sim.s1_run_sim(
            cfg["tag"], cfg["tp_arm"],
            data["score"][sl], data["opn"][sl], data["cls"][sl],
            data["col_of"], data["dates"][sl],
            n_slots=cfg["n"], buffer_m=cfg["m"],
            min_hold=cfg["min_hold"], proceeds_lag=0)
    finally:
        s1_sim.FEE = old_fee
    rec = s1_sim.metrics(res["nav"], fee_total=res["fee_total"],
                         rot_count=res["rot_count"], n_trades=res["n_trades"],
                         avg_exposure=res["avg_exposure"],
                         avg_slots=res["avg_slots"])
    return rec, res["nav"]


def main():
    data = s1_sim.load_data("score_eq")
    dates = data["dates"]
    start = int(np.searchsorted(dates.values, np.datetime64(WARMUP_END), "right"))
    sl = slice(start, len(dates))
    print(f"[final] window {dates[start].date()}~{dates[-1].date()} "
          f"({len(dates)-start}d, 剔除暖机 {dates[0].date()}~{WARMUP_END.date()})",
          flush=True)

    out = dict(window_start=str(dates[start].date()),
               window_end=str(dates[-1].date()),
               warmup_removed=f"{dates[0].date()}~{WARMUP_END.date()}",
               note="mask 为 prep 全历史预计算，窗口切片≡净值层裁剪；"
                    "指标为切片窗口全量重算",
               configs=[])
    for cfg in TOP3:
        t0 = time.time()
        rec1, nav1 = run(cfg, data, sl, 1.0)
        rec2, nav2 = run(cfg, data, sl, 2.0)
        nav1.to_csv(RES / "s1_stress" / f"nav_r3_final_{cfg['tag']}.csv",
                    encoding="utf-8-sig")
        nav2.to_csv(RES / "s1_stress" / f"nav_r3_final_fee2x_{cfg['tag']}.csv",
                    encoding="utf-8-sig")
        entry = dict(**cfg, fee_x1=rec1, fee_x2=rec2,
                     ann_decay_fee2x=rec2["ann_ret"] - rec1["ann_ret"],
                     sharpe_decay_fee2x=rec2["sharpe"] - rec1["sharpe"])
        out["configs"].append(entry)
        print(f"[final] {cfg['tag']}: fee1x ann={rec1['ann_ret']:+.2%} "
              f"sharpe={rec1['sharpe']:.2f} mdd={rec1['max_dd']:+.2%} "
              f"rot={rec1['rot_count']} fee={rec1['fee_total']:,.0f} | "
              f"fee2x ann={rec2['ann_ret']:+.2%} (decay {entry['ann_decay_fee2x']:+.2%}) "
              f"sharpe={rec2['sharpe']:.2f} ({time.time()-t0:.0f}s)", flush=True)

    (RES / "s1_r3_final.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("-> results/s1_r3_final.json", flush=True)


if __name__ == "__main__":
    main()
