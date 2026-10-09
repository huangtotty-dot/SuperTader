# -*- coding: utf-8 -*-
"""S3-B 候选改造 · step2: 候选变体面板测试（预注册冻结后执行，不调变体）。

口径见 doc/experiment/2026-10-10_S3b_候选改造.md §2（预注册冻结）：
  变体 v0~v5 定义、通用过滤器、两指标（隔夜段毛 bp / 腿净 bp）、V-G 闸全部写死。
  隔夜段 = o930(T+1)/c1500(T) − 1（毛，bp/仓位夜）；
  腿 = 核原版 decide_one(gap(T+1), mkt_gap(T+1)) 触发 且 a930(T+1)≥1e5，
       net = c1000×(1−fs)/(o930×(1+fb)) − 1（费率走 core/cost_model.py）。
  子段 POST = 2026-02-24 起，按收益实现日 T+1 划分。

运行：python s3b_panel.py   （秒级）
输出：results/s3b/panel/{panel_positions.csv, panel_summary.csv, metrics.json}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import open_gap_reversal as OGR          # noqa: E402
from core import cost_model as CM                  # noqa: E402

FEE_S, FEE_B = CM.fees('stock')
POST = "2026-02-24"
MIN_A930 = 1_000_000.0
MIN_LEG_A930 = 100_000.0        # size_cap(0.10×a930) ≥ MIN_LEG 10,000 的等价线
N_TOP = 4
N_BASKET = 5
AMP_WIN = 20

VARIANTS = ["v0", "v1", "v2", "v3", "v4", "v5"]


def tstat(s: pd.Series) -> float:
    s = s.dropna()
    if len(s) < 2 or s.std(ddof=1) <= 0:
        return float("nan")
    return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))


def summarize(df: pd.DataFrame, span: str) -> dict:
    on = df["overnight_bp"].dropna()
    legs = df[df["leg_net_bp"].notna()]
    lb = legs["leg_net_bp"]
    return dict(
        span=span,
        on_n=int(len(on)), on_mean=float(on.mean()) if len(on) else np.nan,
        on_median=float(on.median()) if len(on) else np.nan,
        on_t=tstat(on),
        on_pct_neg=float((on < 0).mean()) if len(on) else np.nan,
        leg_n=int(len(lb)),
        leg_mean=float(lb.mean()) if len(lb) else np.nan,
        leg_win=float((lb > 0).mean()) if len(lb) else np.nan,
        leg_t=tstat(lb),
    )


def main() -> None:
    t0 = time.time()
    out = HERE / "results" / "s3b" / "panel"
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(HERE / "results" / "s3b" / "min_index_s3b.parquet")
    df = df[(df["date"] >= "2025-03-31") & (df["date"] <= "2026-09-17")].copy()
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    days = sorted(df["date"].unique())
    next_day = {days[i]: days[i + 1] for i in range(len(days) - 1)}

    # mkt_gap（981 全池含 ETF，核原版 market_gap）
    mg_by_day = {}
    for d, g in df.groupby("date"):
        mg_by_day[d] = OGR.market_gap({c: v for c, v in
                                       zip(g["code"], g["gap"])
                                       if np.isfinite(v)})
    df["mg"] = df["date"].map(mg_by_day)
    df["rel"] = df["gap"] - df["mg"]
    df["rel_prev"] = df.groupby("code")["rel"].shift(1)
    df["amp"] = df["hi_day"] / df["lo_day"] - 1.0
    df["amp20"] = (df.groupby("code")["amp"]
                     .transform(lambda s: s.rolling(AMP_WIN, min_periods=AMP_WIN).mean()))
    rng = (df["hi_day"] - df["lo_day"]).replace(0, np.nan)
    df["close_pos"] = ((df["c1500"] - df["lo_day"]) / rng).fillna(1.0)
    df["strong_close"] = (df["close_pos"] >= 0.5) | (df["c1500"] > df["vwap_day"])

    is_etf = df["code"].str.startswith("5")
    base_mask = (~is_etf) & (df["a930"] >= MIN_A930) & df["gap"].notna() \
        & df["c1500"].notna() & (df["c1500"] > 0)

    # T+1 查询键
    key = {(c, d): (o, c10, a9, gp) for c, d, o, c10, a9, gp in
           zip(df["code"], df["date"], df["o930"], df["c1000"],
               df["a930"], df["gap"])}

    by_day = {d: g for d, g in df.groupby("date")}
    recs = []

    def top_rel(g: pd.DataFrame, mask: pd.Series, n: int) -> pd.DataFrame:
        return g[mask].nsmallest(n, "rel")

    for d in days[:-1]:                     # 最后一日不形成候选（无 T+1）
        g = by_day[d]
        mg = mg_by_day[d]
        u = g[base_mask.loc[g.index]]
        d1 = next_day[d]
        mg1 = mg_by_day[d1]

        picks: dict[str, pd.DataFrame] = {}
        if mg is not None and mg < 0:
            picks["v0"] = top_rel(u, pd.Series(True, index=u.index), N_TOP)
            picks["v1"] = top_rel(u, (u["rel"] < 0) & (u["rel_prev"] < 0), N_TOP)
            picks["v2"] = top_rel(u, u["rel_prev"] > 0, N_TOP)
            picks["v3"] = top_rel(u, u["strong_close"], N_TOP)
            picks["v5"] = top_rel(u, (u["rel_prev"] > 0) & u["strong_close"], N_TOP)
        else:
            for v in ("v0", "v1", "v2", "v3", "v5"):
                picks[v] = u.iloc[0:0]
        # v4：反转倾向篮，不要求 mkt_gap<0
        picks["v4"] = u[u["amp20"].notna()].nlargest(N_BASKET, "amp20")

        for v, p in picks.items():
            for _, r in p.iterrows():
                c = r["code"]
                px1 = key.get((c, d1))
                on_bp = np.nan
                trig = False
                cap_ok = False
                leg_bp = np.nan
                if px1 is not None:
                    o1, c101, a91, gap1 = px1
                    if np.isfinite(o1) and o1 > 0:
                        on_bp = (o1 / r["c1500"] - 1.0) * 1e4
                        ok, _why = OGR.decide_one(gap1, mg1)
                        trig = bool(ok)
                        cap_ok = bool(np.isfinite(a91) and a91 >= MIN_LEG_A930)
                        if trig and cap_ok and np.isfinite(c101) and c101 > 0:
                            leg_bp = ((c101 * (1 - FEE_S))
                                      / (o1 * (1 + FEE_B)) - 1.0) * 1e4
                recs.append(dict(variant=v, code=c, date_T=d, date_T1=d1,
                                 rel_T=r["rel"], rel_prev=r["rel_prev"],
                                 amp20=r["amp20"], strong_close=bool(r["strong_close"]),
                                 mkt_gap_T=mg, mkt_gap_T1=mg1,
                                 overnight_bp=on_bp, triggered=trig,
                                 capacity_ok=cap_ok, leg_net_bp=leg_bp))

    panel = pd.DataFrame(recs)
    panel.to_csv(out / "panel_positions.csv", index=False, encoding="utf-8-sig")

    # 汇总：variant × {FULL, POST}（子段按收益实现日 T+1 划分）
    rows = []
    for v in VARIANTS:
        pv = panel[panel["variant"] == v]
        full = dict(variant=v, **summarize(pv, "FULL"))
        post = dict(variant=v, **summarize(pv[pv["date_T1"] >= POST], "POST"))
        rows += [full, post]
    summ = pd.DataFrame(rows)
    summ.to_csv(out / "panel_summary.csv", index=False, encoding="utf-8-sig")

    # V-G 闸（FULL 口径，预注册写死）
    gates = {}
    for v in VARIANTS:
        f = summ[(summ["variant"] == v) & (summ["span"] == "FULL")].iloc[0]
        gates[v] = dict(
            VG1_on_ge_neg20=bool(f["on_mean"] >= -20.0),
            VG2_leg_ge_30=bool(f["leg_mean"] >= 30.0),
            VG3_legs_ge_200=bool(f["leg_n"] >= 200),
            PASS=bool(f["on_mean"] >= -20.0 and f["leg_mean"] >= 30.0
                      and f["leg_n"] >= 200),
        )
    metrics = dict(variants=VARIANTS,
                   params=dict(N_TOP=N_TOP, N_BASKET=N_BASKET, AMP_WIN=AMP_WIN,
                               MIN_A930=MIN_A930, MIN_LEG_A930=MIN_LEG_A930,
                               fee_buy=FEE_B, fee_sell=FEE_S, post=POST),
                   gates=gates, elapsed=round(time.time() - t0, 1))
    (out / "metrics.json").write_text(json.dumps(
        metrics, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    pd.set_option("display.width", 250)
    print(summ.to_string(index=False, float_format=lambda x: f"{x:,.2f}"), flush=True)
    print(f"\n[V-G gates] {json.dumps(gates, ensure_ascii=False, default=float)}",
          flush=True)
    print(f"({metrics['elapsed']}s)", flush=True)


if __name__ == "__main__":
    main()
