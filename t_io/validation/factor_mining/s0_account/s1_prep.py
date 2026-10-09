# -*- coding: utf-8 -*-
"""
S1 步骤1 · 宇宙卫生规则（预注册修正案①）
=========================================
复用 S0 缓存（pivots.pkl / scores.parquet，只读），构建卫生掩码：
  规则1 低价剔除：信号日 t 收盘价 < 2 元 → 剔除（t 日收盘可得，无未来函数）
  规则2 坠刀剔除：近 60 日窗口**最大回撤** > 60% → 剔除。
        口径说明（预注册）：窗口内 max/min 要求**峰在谷前**（严格回撤语义），
        即 dd60(t) = max_{t-59<=i<=j<=t} close(i)/close(j) - 1；
        向量化实现 = 窗口起点起逐日前缀峰 / 当日价 - 1 的窗口内最大。
        窗口内有效收盘 <20 日（长期停牌/新股）按剔除处理（保守）。
        t 日收盘可得，无未来函数。
卫生后宇宙 = 分数非 NaN 且 两条规则均通过。
因子评估（s1_decile）与账户仿真（s1_sim）共用本掩码（修正案①末条）。

输出（results/）：
  s1_hygiene_mask.pkl    DataFrame(bool), index=分数日期, columns=symbols
  s1_hygiene_daily.csv   每日剔除统计（在分数宇宙内）
  s1_hygiene_stats.json  汇总统计（含朴素 max/min 口径对照剔除量）
"""
from __future__ import annotations

import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"

PX_MIN = 2.0          # 规则1：收盘价下限（元）
DD_WIN = 60           # 规则2：滚动窗口（交易日）
DD_MINP = 20          # 窗口内最少有效收盘数
DD_MAX = 0.60         # 规则2：窗口最大回撤上限


def rolling_dd_orderaware(cls: np.ndarray, win: int) -> np.ndarray:
    """dd60[t, s] = 窗口 [t-win+1, t] 内 峰(先)/谷(后)-1 的最大值。
    峰/谷 NaN 经 np.fmax 自然跳过；窗口起点不足(头部)为 NaN。"""
    n_days, n_sym = cls.shape
    dd = np.full((n_days, n_sym), np.nan)
    for t in range(win - 1, n_days):
        s0 = t - win + 1
        pm = np.full(n_sym, -np.inf)          # 窗口起点起的前缀峰
        best = np.full(n_sym, -np.inf)        # 窗口内最大回撤
        for j in range(s0, t + 1):
            p = cls[j]
            pm = np.fmax(pm, p)
            with np.errstate(invalid="ignore", divide="ignore"):
                cur = pm / p - 1.0            # p<=0/NaN -> NaN/inf
            cur = np.where(np.isfinite(cur), cur, np.nan)
            best = np.fmax(best, cur)
        dd[t] = np.where(np.isfinite(best), best, np.nan)
    return dd


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))

    close_p = piv["close"].reindex(sdates)          # 分数日期 × 全 symbols
    cols = close_p.columns
    cls = close_p.to_numpy(np.float64)
    print(f"[load] close {cls.shape} ({time.time()-t0:.0f}s)", flush=True)

    # ── 规则1：低价 ──
    cond_px = cls >= PX_MIN                          # NaN -> False

    # ── 规则2：窗口最大回撤（峰在谷前，严格语义）──
    dd60 = rolling_dd_orderaware(cls, DD_WIN)
    # 窗口内有效收盘数 >= DD_MINP 才承认窗口；不足按剔除（cond=False）
    n_valid = close_p.notna().rolling(DD_WIN, min_periods=1).sum().to_numpy()
    cond_dd = (dd60 <= DD_MAX) & (n_valid >= DD_MINP)
    cond_dd[:DD_WIN - 1] = False                     # 头部窗口不足
    print(f"[mask] rolling dd60 (order-aware) done ({time.time()-t0:.0f}s)",
          flush=True)

    # 对照：朴素 max/min-1 口径（无序，统计用，不进掩码）
    roll_max = close_p.rolling(DD_WIN, min_periods=DD_MINP).max()
    roll_min = close_p.rolling(DD_WIN, min_periods=DD_MINP).min()
    dd_naive = (roll_max / roll_min - 1.0).to_numpy(np.float64)

    hygiene = cond_px & cond_dd
    mask_df = pd.DataFrame(hygiene, index=sdates, columns=cols)
    with open(OUT_DIR / "s1_hygiene_mask.pkl", "wb") as f:
        pickle.dump(mask_df, f, protocol=4)

    # ── 每日剔除统计（分母 = 当日分数宇宙 = score_eq 非 NaN）──
    sc = piv["score_eq"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    in_univ = np.isfinite(sc)
    bad_px = in_univ & ~cond_px
    bad_dd = in_univ & cond_px & ~cond_dd            # 通过价格但被坠刀剔除
    bad_naive = in_univ & cond_px & ~(dd_naive <= DD_MAX)   # 对照口径
    clean = in_univ & hygiene
    daily = pd.DataFrame(dict(
        n_univ=in_univ.sum(axis=1),
        n_excl_px=bad_px.sum(axis=1),
        n_excl_dd=bad_dd.sum(axis=1),
        n_excl=(in_univ & ~hygiene).sum(axis=1),
        n_clean=clean.sum(axis=1),
        n_bad_naive=bad_naive.sum(axis=1),
    ), index=sdates)
    daily.index.name = "date"
    daily.to_csv(OUT_DIR / "s1_hygiene_daily.csv", encoding="utf-8-sig")

    stats = dict(
        rule_px=f"close < {PX_MIN}",
        rule_dd=(f"rolling{DD_WIN}d 窗口最大回撤(峰在谷前) > {DD_MAX}; "
                 f"窗口有效收盘<{DD_MINP}日按剔除"),
        n_days=int(len(sdates)),
        date_start=str(sdates[0].date()), date_end=str(sdates[-1].date()),
        avg_univ=float(daily["n_univ"].mean()),
        avg_excl_total=float(daily["n_excl"].mean()),
        avg_excl_pct=float((daily["n_excl"] / daily["n_univ"].replace(0, np.nan)).mean()),
        avg_excl_px=float(daily["n_excl_px"].mean()),
        avg_excl_dd=float(daily["n_excl_dd"].mean()),
        avg_clean=float(daily["n_clean"].mean()),
        max_excl_pct=float((daily["n_excl"] / daily["n_univ"].replace(0, np.nan)).max()),
        naive_maxmin_avg_excl=float(daily["n_bad_naive"].mean()),
    )
    (OUT_DIR / "s1_hygiene_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[stats] 日均宇宙 {stats['avg_univ']:.0f} -> 卫生后 {stats['avg_clean']:.0f} "
          f"(剔 {stats['avg_excl_total']:.1f} 只/日 = {stats['avg_excl_pct']:.1%}; "
          f"<2元 {stats['avg_excl_px']:.1f} + 坠刀 {stats['avg_excl_dd']:.1f}; "
          f"对照朴素max/min口径 {stats['naive_maxmin_avg_excl']:.1f})",
          flush=True)
    print(f"[save] s1_hygiene_mask.pkl / daily / stats 总耗时 {time.time()-t0:.0f}s",
          flush=True)


if __name__ == "__main__":
    main()
