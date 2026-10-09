# -*- coding: utf-8 -*-
"""S3-容量：β 臂（OGR 低开反转 · 独立候选池 + 现金袖珍仓）容量与参与率核算。

问题：腿成交集中在竞价与开盘 30 分钟。面板毛利 +64bp/腿（S2，22,225 腿，t=31.3）
在参与率约束下能容纳多少资金？本金 100万/500万/1000万 三档下腿放弃/降级比例与
每腿净 bp 侵蚀多少？

口径（如实声明）：
  · 触发：规则核 core/open_gap_reversal.py 原版判定 —— mkt_gap = 当日缓存池 gap
    中位数（核内 MIN_POOL=5 fail-closed），mkt_gap<0 且 rel = gap − mkt_gap ≤ −1%；
    每日按 rel 最深取 Top-N（N ∈ {4,8,16}）。
  · 执行：09:30 bar open 买 → 10:00 bar close 卖（与 S2 预注册口径一致）。
  · 成交额代理（数据缺口声明）：30min 缓存**无竞价成交额字段**。
      - amt0930 = 09:30~10:00 首根 30min bar 成交额（含竞价撮合额）；
      - amt1000 = 10:00~10:30 bar 成交额（卖出窗口代理）；
      - auction_est = 0.013 × day_amt（09-29 文档口径：竞价额 ≈ 全日成交额中位
        1.30%；属估计值，偏差方向见文档）。
    用 amt0930 直接当竞价代理会**高估**竞价流动性（容量偏乐观）；auction_est
    口径偏保守。两口径并列报告。
  · 两种执行模式：
      M1「竞价硬卡」= production size_cap_by_auction 语义：买侧上限 p×auction_est，
                       卖侧上限 p×amt1000；
      M2「首30分钟分批」：买侧上限 p×amt0930，卖侧上限 p×amt1000。
  · 参与率上限 p ∈ {5%,10%,20%}；单腿上限 = min(买侧上限, 卖侧上限)。
  · 换算链：组合 C → 袖珍仓 = 25%×C（β 臂专用现金）→ 单腿预算 b = 袖珍仓/N
    （N 个槽位均分，固定配额，不随当日实际腿数浮动）。
  · T+1：10:00 卖出的是前一交易日买入解冻的底仓，09:30 新买部分次日才解冻。
    两腿名义相等（卖旧买新），容量约束双边对称施加。
  · 费用：core/cost_model.py 单一真源（stock/etf 按代码前缀 5 判 ETF）。
  · 冲击：平方根模型 impact_bp = Y·σ_exec·√(Q/V)·1e4，Y=0.7（敏感 0.5/1.0），
    σ_exec = 当日 30min 已实现波动 / √8（单根 bar 尺度），V 取执行窗口成交额。
  · 单腿下限 LEG_MIN = 6 万（cost_model 5 元最低佣金边界 58,548 元之上；
    低于此的腿费用率恶化且无实战意义，判放弃）。

子段：FULL（全窗）与 POST（2026-02-24 起；S2-D1 衰减拐点修正口径）。
输出：t_io/cache/capacity_beta/{amount_index.parquet, legs_topN.csv,
      capacity_grid.csv, amount_dist.csv, summary.json}
复跑：python scripts/capacity_beta.py   （幂等；amount_index 有缓存则跳过重建）
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core import open_gap_reversal as OGR          # noqa: E402
from core import cost_model as CM                  # noqa: E402

CACHE = BASE / "t_io" / "cache" / "tushare_mins"
OUT = BASE / "t_io" / "cache" / "capacity_beta"
IDX = OUT / "amount_index.parquet"

NS = (4, 8, 16)
PCAPS = (0.05, 0.10, 0.20)
CAPITALS = (1_000_000.0, 5_000_000.0, 10_000_000.0)
POCKET_FRAC = 0.25                 # 袖珍仓 = 组合 25%
LEG_MIN = 60_000.0                 # 单腿下限（5元最低佣金边界之上）
Y_IMPACT = 0.7                     # 平方根冲击系数基线
AUCTION_FRAC_OF_DAY = 0.013        # 竞价额 ≈ 全日成交额 1.30%（09-29 文档口径）
POST_CUT = "2026-02-24"            # S2-D1 衰减拐点
BARS_PER_DAY = 8                   # 30min bar 数/日（σ_exec 折算用）


# ────────────────────────── Stage 1: 量额索引 ──────────────────────────

def build_amount_index() -> pd.DataFrame:
    files = sorted(glob.glob(str(CACHE / "*_30min_d540.json")))
    recs = []
    bad = []
    for k, fp in enumerate(files):
        base = os.path.basename(fp)
        ts = base.split("_")[0]                    # e.g. 000001.SZ
        code6 = ts.split(".")[0]
        try:
            with open(fp, encoding="utf-8") as f:
                rows = json.load(f)["rows"]
        except Exception:
            bad.append(base)
            continue
        per_day: dict[str, dict] = {}
        for r in rows:
            d = r["time"][:10]
            tm = r["time"][11:16]
            e = per_day.setdefault(d, {"lastc": np.nan, "amt": 0.0, "r2": 0.0,
                                       "prevc": np.nan})
            if tm == "09:30":
                e["o930"] = r["open"]
                e["amt0930"] = r["amount"]
            elif tm == "10:00":
                e["c1000"] = r["close"]
                e["amt1000"] = r["amount"]
            c = r["close"]
            if np.isfinite(e["prevc"]) and e["prevc"] > 0 and c > 0:
                lr = math.log(c / e["prevc"])
                e["r2"] += lr * lr
            e["prevc"] = c
            e["lastc"] = c
            e["amt"] += r["amount"]
        dates = sorted(per_day)
        for i, d in enumerate(dates):
            e = per_day[d]
            if "o930" not in e or "c1000" not in e:
                continue
            pc = per_day[dates[i - 1]]["lastc"] if i > 0 else np.nan
            recs.append((code6, d, e["o930"], e["c1000"], pc,
                         e.get("amt0930", np.nan), e.get("amt1000", np.nan),
                         e["amt"], math.sqrt(e["r2"])))
        if (k + 1) % 200 == 0:
            print(f"  [idx] {k+1}/{len(files)} files", flush=True)
    df = pd.DataFrame(recs, columns=["code", "date", "o930", "c1000",
                                     "prev_close", "amt0930", "amt1000",
                                     "day_amt", "rv"])
    df["gap"] = np.where((df["prev_close"] > 0) & (df["o930"] > 0),
                         df["o930"] / df["prev_close"] - 1.0, np.nan)
    print(f"  [idx] rows={len(df)} codes={df['code'].nunique()} "
          f"{df['date'].min()}~{df['date'].max()} bad={len(bad)}", flush=True)
    return df


# ────────────────────────── Stage 2: Top-N 腿集 ──────────────────────────

def build_legs(mi: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    g = mi.dropna(subset=["gap"])
    mg = g.groupby("date")["gap"].median()          # 981 池当日 gap 中位数
    pool_n = g.groupby("date")["gap"].size()
    ok_days = mg[(mg < 0) & (pool_n >= OGR.MIN_POOL)].index
    g = g[g["date"].isin(ok_days)].copy()
    g["mkt_gap"] = g["date"].map(mg)
    g["rel"] = g["gap"] - g["mkt_gap"]
    cand = g[g["rel"] <= OGR.REL_THRESHOLD].copy()
    cand["gross_bp"] = np.where(
        (cand["o930"] > 0) & (cand["c1000"] > 0),
        (cand["c1000"] / cand["o930"] - 1.0) * 1e4, np.nan)
    cand = cand.dropna(subset=["gross_bp"])
    cand["auction_est"] = AUCTION_FRAC_OF_DAY * cand["day_amt"]
    cand["venue"] = np.where(cand["code"].str[0] == "5", "etf", "stock")
    cand["fee_bp"] = cand["venue"].map(
        {v: CM.round_trip(v) * 1e4 for v in ("stock", "etf")})
    cand["rank_rel"] = cand.groupby("date")["rel"].rank(method="first")
    legs = []
    for n in NS:
        sub = cand[cand["rank_rel"] <= n].copy()
        sub["N"] = n
        legs.append(sub)
    legs = pd.concat(legs, ignore_index=True)
    trig = cand.groupby("date").size()
    stats = dict(n_days_total=int(mi["date"].nunique()),
                 n_days_mkt_neg=int(len(ok_days)),
                 n_days_any_trigger=int((trig > 0).sum()),
                 trig_median=float(trig.median()) if len(trig) else np.nan,
                 trig_p90=float(trig.quantile(0.9)) if len(trig) else np.nan,
                 n_candidates=int(len(cand)))
    return legs, stats


# ────────────────────── Stage 3: 容量网格 + 冲击侵蚀 ──────────────────────

def _tstat(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 2 or x.std() == 0:
        return np.nan
    return float(x.mean() / (x.std() / math.sqrt(len(x))))


def capacity_grid(legs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for n in NS:
        lg = legs[legs["N"] == n]
        for mode in ("M1_auction", "M2_bar30"):
            v_buy = lg["auction_est"] if mode == "M1_auction" else lg["amt0930"]
            v_buy = v_buy.clip(lower=1.0)
            v_sell = lg["amt1000"].clip(lower=1.0)
            for p in PCAPS:
                cap = np.minimum(p * v_buy, p * v_sell)
                for C in CAPITALS:
                    b = POCKET_FRAC * C / n
                    funded = np.minimum(b, cap)
                    exec_leg = funded >= LEG_MIN          # 低于单腿下限 ⇒ 放弃
                    funded_x = np.where(exec_leg, funded, 0.0)
                    dropped = ~exec_leg
                    degraded = exec_leg & (funded < 0.999 * b)
                    sig = (lg["rv"] / math.sqrt(BARS_PER_DAY)).fillna(0.0)
                    imp = (Y_IMPACT * sig *
                           (np.sqrt(funded_x / v_buy) +
                            np.sqrt(funded_x / v_sell))) * 1e4
                    net = lg["gross_bp"] - lg["fee_bp"] - imp
                    # 无约束基线：足额 b、无冲击、不判放弃
                    net_uc = lg["gross_bp"] - lg["fee_bp"]
                    pnl_c = float((funded_x * net / 1e4).sum())
                    pnl_u = float((b * net_uc / 1e4).sum())
                    w = funded_x[exec_leg]
                    rows.append(dict(
                        N=n, mode=mode, p_cap=p, capital=C,
                        leg_budget=b, budget_ok=bool(b >= LEG_MIN),
                        legs=len(lg),
                        drop_pct=float(dropped.mean() * 100),
                        drop_cap_pct=float((cap < LEG_MIN).mean() * 100),
                        degrade_pct=float(degraded.mean() * 100),
                        full_pct=float((exec_leg & ~degraded).mean() * 100),
                        cap_med=float(cap.median()),
                        cap_p10=float(cap.quantile(0.10)),
                        gross_bp=float(lg["gross_bp"].mean()),
                        gross_t=_tstat(lg["gross_bp"]),
                        fee_bp=float(lg["fee_bp"].mean()),
                        impact_bp=float(imp[exec_leg].mean())
                        if exec_leg.any() else np.nan,
                        net_bp=float(net[exec_leg].mean())
                        if exec_leg.any() else np.nan,
                        net_t=_tstat(net[exec_leg]),
                        net_bp_w=float((net[exec_leg] * w).sum() / w.sum())
                        if w.sum() > 0 else np.nan,
                        pnl_constr=pnl_c, pnl_unconstr=pnl_u,
                        retain_pct=(pnl_c / pnl_u * 100) if pnl_u > 0 else np.nan,
                    ))
    return pd.DataFrame(rows)


def amount_distribution(legs: pd.DataFrame) -> pd.DataFrame:
    """② 触发候选的成交额分布 + 单腿/每日可容纳资金 + 账户规模上限。"""
    rows = []
    for n in NS:
        lg = legs[legs["N"] == n]
        for span, sub in (("FULL", lg),
                          ("POST", lg[lg["date"] >= POST_CUT])):
            if sub.empty:
                continue
            daily = sub.groupby("date").size()
            rec = dict(N=n, span=span, days=int(sub["date"].nunique()),
                       legs=int(len(sub)),
                       legs_per_day_med=float(daily.median()),
                       amt0930_med=float(sub["amt0930"].median()),
                       amt0930_p25=float(sub["amt0930"].quantile(0.25)),
                       amt0930_p10=float(sub["amt0930"].quantile(0.10)),
                       amt1000_med=float(sub["amt1000"].median()),
                       amt1000_p10=float(sub["amt1000"].quantile(0.10)),
                       auction_est_med=float(sub["auction_est"].median()),
                       auction_est_p10=float(sub["auction_est"].quantile(0.10)),
                       a0930_of_day=float((sub["amt0930"] /
                                           sub["day_amt"]).median()))
            for p in PCAPS:
                cb = np.minimum(p * sub["amt0930"], p * sub["amt1000"])
                ca = np.minimum(p * sub["auction_est"], p * sub["amt1000"])
                rec[f"leg_cap_M2_p{int(p*100)}"] = float(cb.median())
                rec[f"leg_cap_M1_p{int(p*100)}"] = float(ca.median())
                # 每日合计可容纳（M2）：逐日 Σ cap 的中位数
                dsum = pd.Series(cb).groupby(sub["date"].values).sum()
                rec[f"day_cap_M2_p{int(p*100)}"] = float(dsum.median())
                # 账户上限：袖珍仓=N×腿预算 ≤ N×腿容量 → C ≤ 腿容量×N/0.25
                rec[f"Cmax_M2_p{int(p*100)}"] = float(cb.median() * n / POCKET_FRAC)
                rec[f"Cmax_M1_p{int(p*100)}"] = float(ca.median() * n / POCKET_FRAC)
            rows.append(rec)
    return pd.DataFrame(rows)


# ────────────────────────────── main ──────────────────────────────

def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    if IDX.exists():
        mi = pd.read_parquet(IDX)
        print(f"[idx] cache hit: {len(mi)} rows", flush=True)
    else:
        print("[idx] building amount index ...", flush=True)
        mi = build_amount_index()
        mi.to_parquet(IDX, index=False)
        print(f"[idx] saved -> {IDX} "
              f"({IDX.stat().st_size/1e6:.1f}MB)", flush=True)

    legs, trig_stats = build_legs(mi)
    legs_out = legs[["N", "date", "rank_rel", "code", "venue", "gap", "mkt_gap",
                     "rel", "o930", "c1000", "gross_bp", "fee_bp", "amt0930",
                     "amt1000", "auction_est", "day_amt", "rv"]]
    legs_out.to_csv(OUT / "legs_topN.csv", index=False, encoding="utf-8-sig")
    print(f"[legs] {trig_stats}", flush=True)

    dist = amount_distribution(legs)
    dist.to_csv(OUT / "amount_dist.csv", index=False, encoding="utf-8-sig")

    grid = capacity_grid(legs)
    grid.to_csv(OUT / "capacity_grid.csv", index=False, encoding="utf-8-sig")

    # POST 子段的容量网格（拐点后，成交额环境更接近当前）
    legs_post = legs[legs["date"] >= POST_CUT]
    grid_post = capacity_grid(legs_post)
    grid_post.to_csv(OUT / "capacity_grid_post.csv", index=False,
                     encoding="utf-8-sig")

    # 冲击系数敏感性（Y=0.5/1.0，M2，p=10%，对 net_bp 的影响）
    global Y_IMPACT
    sens = {}
    base_y = Y_IMPACT
    for y in (0.5, 1.0):
        Y_IMPACT = y
        g = capacity_grid(legs)
        m = g[(g["mode"] == "M2_bar30") & (g["p_cap"] == 0.10)]
        sens[f"Y{y}"] = {f"C{int(r.capital/1e6)}M_N{int(r.N)}":
                         round(float(r.impact_bp), 2)
                         for r in m.itertuples()}
    Y_IMPACT = base_y

    summary = dict(window=[str(mi["date"].min()), str(mi["date"].max())],
                   post_cut=POST_CUT, n_codes=int(mi["code"].nunique()),
                   trigger=trig_stats, pocket_frac=POCKET_FRAC, leg_min=LEG_MIN,
                   y_impact=Y_IMPACT, auction_frac_of_day=AUCTION_FRAC_OF_DAY,
                   impact_sensitivity=sens,
                   idx_size_mb=round(IDX.stat().st_size / 1e6, 1))
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8")

    pd.set_option("display.width", 260)
    print("\n===== amount_dist =====")
    print(dist[["N", "span", "days", "legs", "amt0930_med", "amt0930_p10",
                "auction_est_med", "a0930_of_day",
                "leg_cap_M2_p10", "leg_cap_M1_p10",
                "day_cap_M2_p10", "Cmax_M2_p10", "Cmax_M1_p10"]].to_string(
        index=False, float_format=lambda x: f"{x:,.2f}"))
    for tag, g in (("FULL", grid), ("POST", grid_post)):
        for mode in ("M2_bar30", "M1_auction"):
            m = g[g["mode"] == mode]
            print(f"\n===== capacity_grid ({tag}, {mode}) =====")
            print(m[["N", "p_cap", "capital", "leg_budget", "budget_ok",
                     "drop_cap_pct", "drop_pct", "degrade_pct", "gross_bp",
                     "impact_bp", "net_bp", "net_t", "retain_pct"]].to_string(
                index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\n[done] {time.time()-t0:.0f}s -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
