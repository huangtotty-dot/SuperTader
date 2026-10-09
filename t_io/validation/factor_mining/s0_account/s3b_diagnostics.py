# -*- coding: utf-8 -*-
"""S3-B 候选改造 · 机制诊断（面板跑完后执行；只读 panel_positions.csv，不改任何口径）。

目的：为「候选改造失败」提供机制解释，全部属补充披露（非闸、非变体调整）：
  D1 全池隔夜均值（通用过滤器宇宙，全部夜）——v4 的 −46bp 是否只是池级隔夜漂移；
  D2 各变体隔夜均值按 T+1 是否触发 OGR 分组——隔夜毒性是否集中在触发组；
  D3 各变体 rel_T 深度统计——腿利润与 rel 深度（=隔夜毒性）的同源性；
  D4 v4 篮隔夜 vs 同夜全池隔夜——预选 rel 与隔夜毒性的因果分离。
输出 results/s3b/panel/diagnostics.json + 控制台表。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
POST = "2026-02-24"


def tstat(s: pd.Series) -> float:
    s = s.dropna()
    if len(s) < 2 or s.std(ddof=1) <= 0:
        return float("nan")
    return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))


def main() -> None:
    out = HERE / "results" / "s3b" / "panel"
    panel = pd.read_csv(out / "panel_positions.csv",
                        dtype={"code": str, "date_T": str, "date_T1": str})
    df = pd.read_parquet(HERE / "results" / "s3b" / "min_index_s3b.parquet")
    df = df[(df["date"] >= "2025-03-31") & (df["date"] <= "2026-09-17")].copy()
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    # ── D1/D4：全池隔夜（通用过滤器宇宙，非 ETF、a930(T)≥100万、两端价有效）──
    is_etf = df["code"].str.startswith("5")
    df["c1500_next_o930"] = df.groupby("code")["o930"].shift(-1)
    df["date_T1"] = df.groupby("code")["date"].shift(-1)
    pool = df[(~is_etf) & (df["a930"] >= 1_000_000) & df["c1500"].notna()
              & (df["c1500"] > 0) & df["c1500_next_o930"].notna()
              & (df["c1500_next_o930"] > 0)].copy()
    pool["overnight_bp"] = (pool["c1500_next_o930"] / pool["c1500"] - 1.0) * 1e4
    pool_on = pool["overnight_bp"]
    d1 = dict(n=int(len(pool_on)), mean=float(pool_on.mean()),
              median=float(pool_on.median()), t=tstat(pool_on),
              pct_neg=float((pool_on < 0).mean()))
    pool_post = pool.loc[pool["date_T1"] >= POST, "overnight_bp"]
    d1["post_mean"] = float(pool_post.mean())
    d1["post_n"] = int(len(pool_post))

    # ── D2：各变体隔夜按触发分组；D3：rel 深度 ──
    d2, d3 = {}, {}
    for v, pv in panel.groupby("variant"):
        trg = pv[pv["triggered"]]
        notrg = pv[~pv["triggered"]]
        d2[v] = dict(
            trig_n=int(len(trg)),
            trig_on_mean=float(trg["overnight_bp"].mean()),
            notrig_n=int(len(notrg)),
            notrig_on_mean=float(notrg["overnight_bp"].mean()),
        )
        d3[v] = dict(rel_mean=float(pv["rel_T"].mean()),
                     rel_p10=float(pv["rel_T"].quantile(0.1)),
                     rel_p90=float(pv["rel_T"].quantile(0.9)))

    # ── D4：v4 篮隔夜 vs 同夜全池隔夜（同 (code?, 夜) 配对：按夜均值差）──
    v4 = panel[panel["variant"] == "v4"][["date_T", "overnight_bp"]].dropna()
    pool_night = pool.groupby(pool["date"])["overnight_bp"].mean()
    v4_night = v4.groupby("date_T")["overnight_bp"].mean()
    both = pd.concat([v4_night.rename("v4"), pool_night.rename("pool")],
                     axis=1, join="inner")
    d4 = dict(n_nights=int(len(both)),
              v4_night_mean=float(both["v4"].mean()),
              pool_night_mean=float(both["pool"].mean()),
              excess_mean=float((both["v4"] - both["pool"]).mean()),
              excess_t=tstat(both["v4"] - both["pool"]))

    diag = dict(D1_pool_overnight=d1, D2_overnight_by_trigger=d2,
                D3_rel_depth=d3, D4_v4_vs_pool_same_nights=d4)
    (out / "diagnostics.json").write_text(json.dumps(
        diag, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    pd.set_option("display.width", 250)
    print(f"[D1] 全池隔夜: {json.dumps(d1, default=float)}")
    print("\n[D2] 隔夜均值按 T+1 是否触发分组（bp）：")
    print(pd.DataFrame(d2).T.to_string(float_format=lambda x: f"{x:,.1f}"))
    print("\n[D3] rel_T 深度（小数）：")
    print(pd.DataFrame(d3).T.to_string(float_format=lambda x: f"{x:,.4f}"))
    print(f"\n[D4] v4 vs 同夜全池: {json.dumps(d4, default=float)}")


if __name__ == "__main__":
    main()
