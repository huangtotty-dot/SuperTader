# -*- coding: utf-8 -*-
"""S2-T1 step4: OGR 做T增强叠加引擎 + α/β 归因（主实验脚本）。

口径（全部预注册可对账）：
  · 触发：规则核 core/open_gap_reversal.py 原版判定 —— mkt_gap = 当日 981 缓存池
    gap 中位数（task 指定；非冻结的 L20 代理名单）、mkt_gap<0 且 rel=gap-mkt_gap≤-1%；
  · 执行：09:30 bar open 买 → 10:00 bar close 卖（预注册文档 §执行口径）；
  · 底仓：α 账户 open 成交后的持仓，且 entry_d<d（T+1：当日新买槽位不开腿）；
  · 腿资金 = 槽市值(open) × LEG_FRAC(25%)；腿费 = stock 双边 0.069%（每边 0.0345%）；
  · 每票每日 ≤1 腿；腿与底仓分账：β 单独成账，不回灌 α 决策；
  · 臂A 无门控全触发；臂B 叠加 trend30 门控（T-1 末根 30min 状态 == BULL 才开腿）。

输出 results/s2_ogr/:
  legs_armA.csv / legs_armB.csv     逐腿明细
  beta_daily.csv                    两臂逐日 β 盈亏
  attribution.csv / attribution.json 全窗+切半×两臂 的 α/β/α+β 归因表
"""
from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "s2_ogr"
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import open_gap_reversal as OGR  # noqa: E402

FEE = 0.000345                    # 每边 0.0345%（双边 0.069%）
LEG_FRAC = 0.25                   # 腿资金 = 槽市值 × 25%（参数化）
INIT = 1_000_000.0
CUT = "2025-06-11"                # 切半点


def load_gate() -> dict:
    g = pd.concat([pd.read_parquet(p) for p in
                   sorted(glob.glob(str(OUT / "trend30_gate_*_*.parquet")))],
                  ignore_index=True)
    return {(c, d): s for c, d, s in zip(g["code"], g["date"], g["state"])}


def slice_metrics(nav: pd.Series) -> dict:
    nav = nav.dropna()
    ret = nav.pct_change().dropna()
    n = len(nav)
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (252.0 / max(n - 1, 1)) - 1.0
    mdd = float((nav / nav.cummax() - 1.0).min())
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252.0)) if ret.std() > 0 else np.nan
    return dict(ann=ann, mdd=mdd, sharpe=sharpe, n=n)


def beta_stats(legs: pd.DataFrame, days: pd.Series, n_days: int) -> dict:
    """β 账统计：腿数/胜率/每腿净bp + 日度盈亏 Sharpe（对 1M 名义本金）。"""
    if legs.empty:
        return dict(legs=0, win=np.nan, bp_mean=np.nan, bp_wmean=np.nan,
                    pnl=0.0, ann_contrib=0.0, sharpe=np.nan)
    pnl = float(legs["pnl"].sum())
    daily = days.to_frame("date").merge(
        legs.groupby("date")["pnl"].sum().rename("p"), on="date", how="left"
    )["p"].fillna(0.0) / INIT
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252.0)) if daily.std() > 0 else np.nan
    w = legs["notional"]
    return dict(legs=int(len(legs)), win=float((legs["pnl"] > 0).mean()),
                bp_mean=float(legs["net_bp"].mean()),
                bp_wmean=float((legs["net_bp"] * w).sum() / w.sum()),
                pnl=pnl, ann_contrib=pnl / INIT * 252.0 / max(n_days, 1),
                sharpe=sharpe)


def main() -> None:
    t0 = time.time()
    nav = pd.read_csv(OUT / "nav_alpha.csv", index_col=0, parse_dates=True).iloc[:, 0]
    holds = pd.read_parquet(OUT / "holdings_daily.parquet")
    mi = pd.read_parquet(OUT / "min_index.parquet")
    gate = load_gate()

    # ── 每日池 gap（981 池，规则核 median 口径）──
    pool = {d: dict(zip(g["code"], g["gap"]))
            for d, g in mi.dropna(subset=["gap"]).groupby("date")}
    mi_key = {(c, d): (o, c10) for c, d, o, c10 in
              zip(mi["code"], mi["date"], mi["o930"], mi["c1000"])}

    holds["dstr"] = holds["date"].dt.strftime("%Y-%m-%d")
    days = pd.Series(sorted(holds["dstr"].unique()), name="date")
    elig = holds[holds["eligible"]].copy()

    legs = []
    cov = dict(elig_slot_days=len(elig), no_min_cache=0, no_gap=0,
               pool_thin=0, mkt_not_neg=0, triggered=0)
    for r in elig.itertuples():
        key = (r.code, r.dstr)
        px = mi_key.get(key)
        if px is None:
            cov["no_min_cache"] += 1
            continue
        gaps = pool.get(r.dstr, {})
        mg = OGR.market_gap({c: g for c, g in gaps.items()})   # 核内 MIN_POOL=5
        if mg is None:
            cov["pool_thin"] += 1
            continue
        gap = gaps.get(r.code, np.nan)
        ok, _why = OGR.decide_one(gap, mg)                     # 核原版判定
        if not ok:
            if np.isfinite(mg) and not (mg < 0):
                cov["mkt_not_neg"] += 1
            continue
        cov["triggered"] += 1
        o930, c1000 = px
        if not (np.isfinite(o930) and np.isfinite(c1000) and o930 > 0):
            continue
        notional = LEG_FRAC * r.slot_value
        gross = c1000 / o930 - 1.0
        net = c1000 / o930 * (1.0 - FEE) / (1.0 + FEE) - 1.0
        legs.append(dict(date=r.dstr, code=r.code, notional=notional,
                         gap=gap, rel=gap - mg, mkt_gap=mg,
                         gate=gate.get(key, "NONE"),
                         gross_bp=gross * 1e4, net_bp=net * 1e4,
                         pnl=notional * net))
    legs = pd.DataFrame(legs)
    legs.to_csv(OUT / "legs_armA.csv", index=False, encoding="utf-8-sig")
    legsB = legs[legs["gate"] == "BULL"].copy()
    legsB.to_csv(OUT / "legs_armB.csv", index=False, encoding="utf-8-sig")

    # ── 逐日 β ──
    beta_daily = days.to_frame("date")
    for arm, lg in (("A", legs), ("B", legsB)):
        s = lg.groupby("date")["pnl"].sum()
        beta_daily[f"beta_{arm}"] = beta_daily["date"].map(s).fillna(0.0)
        beta_daily[f"legs_{arm}"] = beta_daily["date"].map(
            lg.groupby("date").size()).fillna(0).astype(int)
    beta_daily.to_csv(OUT / "beta_daily.csv", index=False, encoding="utf-8-sig")

    # ── 归因表（全窗 + 切半 × 两臂）──
    nav_d = nav.copy()
    nav_d.index = nav_d.index.strftime("%Y-%m-%d")
    rows = []
    spans = dict(FULL=(days.iloc[0], days.iloc[-1]),
                 H1=(days.iloc[0], "2025-06-10"), H2=(CUT, days.iloc[-1]))
    for span, (d0, d1) in spans.items():
        m = (nav_d.index >= d0) & (nav_d.index <= d1)
        nav_s = nav_d[m]
        days_s = days[(days >= d0) & (days <= d1)]
        a = slice_metrics(nav_s)
        row = dict(span=span, start=d0, end=d1, n_days=int(m.sum()),
                   a_ann=a["ann"], a_sharpe=a["sharpe"], a_mdd=a["mdd"])
        for arm, lg in (("A", legs), ("B", legsB)):
            lg_s = lg[(lg["date"] >= d0) & (lg["date"] <= d1)]
            b = beta_stats(lg_s, days_s, int(m.sum()))
            comb = nav_s + lg_s.groupby("date")["pnl"].sum().reindex(
                nav_s.index).fillna(0.0).cumsum()
            c = slice_metrics(comb)
            row.update({f"b{arm}_legs": b["legs"], f"b{arm}_win": b["win"],
                        f"b{arm}_bp": b["bp_wmean"], f"b{arm}_pnl": b["pnl"],
                        f"b{arm}_ann": b["ann_contrib"],
                        f"b{arm}_sharpe": b["sharpe"],
                        f"ab{arm}_ann": c["ann"], f"ab{arm}_sharpe": c["sharpe"],
                        f"ab{arm}_mdd": c["mdd"]})
        rows.append(row)
    attr = pd.DataFrame(rows)
    attr.to_csv(OUT / "attribution.csv", index=False, encoding="utf-8-sig")
    (OUT / "attribution.json").write_text(
        json.dumps(dict(coverage=cov, leg_frac=LEG_FRAC, fee_side=FEE,
                        cut=CUT, rows=rows), ensure_ascii=False, indent=2,
                   default=float), encoding="utf-8")

    pd.set_option("display.width", 250)
    print(attr.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))
    print(f"[cov] {cov}", flush=True)
    print(f"({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
