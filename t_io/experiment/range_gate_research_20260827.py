# -*- coding: utf-8 -*-
"""震荡市门控深度研究（2026-08-27）
问题：现行 regime 门控在 range 市结构性封锁 signal（已连续 15 天），
     是否应允许"震荡市小仓位首笔"？需要数据回答三个子问题：
  A. range 市内部是否存在可识别的正 edge 子集（个股结构/极端超跌/因子组合）？
  B. regime 定义本身能否细化（ratio 子带 × MA60 斜率），把"有edge的range"分出来？
  C. range 段落生命周期：末期埋伏 vs 突破后追入，哪个更好？门控的机会成本多大？
  D. 若找到子集：评估"range 市 1/3 首笔 + 梯级补仓"规则的历史表现。

纯离线，复用 build_factor_mining_20260827 的加载/特征函数。偏差声明同该脚本。
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_factor_mining_20260827 import (  # noqa: E402
    CACHE_DIR, UP_BUF, DN_BUF, load_stock, add_features, events_of,
)


def load_index_full():
    d = json.load(open(os.path.join(CACHE_DIR, "index_sh000001.json"), encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["close"] = df["close"].astype(float)
    df["ma60"] = df["close"].rolling(60).mean()
    df["ratio"] = df["close"] / df["ma60"]
    df["regime"] = np.where(df["ratio"] > UP_BUF, "trend_up",
                    np.where(df["ratio"] < DN_BUF, "trend_dn",
                             np.where(df["ma60"].isna(), "unknown", "range")))
    df["ma60_up"] = df["ma60"] > df["ma60"].shift(5)   # MA60 五日线斜率
    return df


def build_episode_table(idx):
    """range 段落表：date → (ep_id, third, exit_dir, ep_len, days_to_end)。
    仅 len>=5 的 range 段计入；exit_dir = 段落结束后首个非 range regime。"""
    info = {}
    regimes = list(idx["regime"])
    dates = list(idx["date"].astype(str))
    n = len(regimes)
    i = 0
    ep = 0
    while i < n:
        if regimes[i] != "range":
            i += 1
            continue
        j = i
        while j < n and regimes[j] == "range":
            j += 1
        ep_len = j - i
        exit_dir = regimes[j] if j < n else "ongoing"
        if ep_len >= 5:
            ep += 1
            for k in range(i, j):
                pos = (k - i) / ep_len
                third = "early" if pos < 1 / 3 else ("mid" if pos < 2 / 3 else "late")
                info[dates[k]] = (ep, third, exit_dir, ep_len, j - 1 - k)
        i = j
    return info


def stat_row(df_, label):
    a = df_[["fwd5", "fwd10", "fwd20"]].dropna()
    if len(a) < 30:
        return {"subset": label, "n": len(a)}
    return {"subset": label, "n": len(a),
            "w5": (a["fwd5"] > 0).mean(), "r5": a["fwd5"].mean(),
            "w10": (a["fwd10"] > 0).mean(), "r10": a["fwd10"].mean(),
            "w20": (a["fwd20"] > 0).mean(), "r20": a["fwd20"].mean(),
            "p5_10": np.percentile(a["fwd10"], 5)}


def main():
    idx = load_index_full()
    regime_map = dict(zip(idx["date"].astype(str), idx["regime"]))
    band_map = {}
    slope_map = {}
    for _, r in idx.iterrows():
        d = str(r["date"])
        if r["regime"] != "range":
            continue
        ratio = r["ratio"]
        band = ("[0.97,0.99)" if ratio < 0.99 else
                ("[0.99,1.0)" if ratio < 1.0 else "[1.0,1.005)"))
        band_map[d] = band
        slope_map[d] = "MA60上行" if r["ma60_up"] else "MA60下行"
    ep_map = build_episode_table(idx)

    # 段落概览
    eps = {}
    for d, (e, third, exit_dir, ln, dte) in ep_map.items():
        eps[e] = (exit_dir, ln)
    print("历史 range 段落(len≥5):")
    for e, (exit_dir, ln) in eps.items():
        print(f"  ep{e}: len={ln} exit={exit_dir}")

    files = [f for f in glob.glob(os.path.join(CACHE_DIR, "*.json"))
             if "index_" not in os.path.basename(f)]
    range_parts = []
    rule_events = defaultdict(list)   # Part D
    n_used = 0
    for k, path in enumerate(files):
        df = load_stock(path)
        if df is None:
            continue
        df = add_features(df)
        ds = df["date"]
        regime_s = ds.map(regime_map)
        usable = df["ma60"].notna() & df["fwd20"].notna()
        if usable.sum() < 50:
            continue
        n_used += 1
        rmask = usable & (regime_s == "range")
        if rmask.sum() > 0:
            sub = df.loc[rmask, ["date", "dd20", "rsi14", "dist_ma60", "bb_pct",
                                 "vol_ratio20", "above_ma60", "trend_struct",
                                 "fwd5", "fwd10", "fwd20"]].copy()
            sub["band"] = sub["date"].map(band_map)
            sub["slope"] = sub["date"].map(slope_map)
            epinfo = sub["date"].map(ep_map)
            sub["ep_third"] = [t[1] if isinstance(t, tuple) else None for t in epinfo]
            sub["ep_exit"] = [t[2] if isinstance(t, tuple) else None for t in epinfo]
            range_parts.append(sub)
        # Part D：候选规则事件（range 市内，事件级去重）
        dip_deep = usable & (regime_s == "range") & df["above_ma60"] & (df["dd20"] <= -0.10)
        oversold = usable & (regime_s == "range") & (df["bb_pct"] < 0)
        for name, m in [("D1_range+MA60上+深回撤≤-10%", dip_deep),
                        ("D2_range+破布林下轨", oversold)]:
            ev = events_of(m.fillna(False))
            for i in np.flatnonzero(ev.values):
                if i + 20 < len(df):
                    rule_events[name].append((df["fwd5"].iat[i], df["fwd10"].iat[i],
                                              df["fwd20"].iat[i]))
        if (k + 1) % 800 == 0:
            print(f"  进度 {k + 1}/{len(files)}", flush=True)

    P = pd.concat(range_parts, ignore_index=True)
    print(f"\nrange 市股票-日样本: {len(P):,}（标的 {n_used}）")

    print("\n" + "=" * 76)
    print("Part A  range 市内部：个股结构/因子子集 edge（w=胜率 r=均收益）")
    print("=" * 76)
    cands = [
        ("range 基线(全部)", P),
        ("站上MA60", P[P["above_ma60"]]),
        ("多头结构(>MA20&MA60)", P[P["trend_struct"]]),
        ("MA60上+浅回撤[-5,-3)%", P[P["above_ma60"] & (P["dd20"] >= -0.05) & (P["dd20"] < -0.03)]),
        ("MA60上+中回撤[-10,-5)%", P[P["above_ma60"] & (P["dd20"] >= -0.10) & (P["dd20"] < -0.05)]),
        ("MA60上+深回撤≤-10%", P[P["above_ma60"] & (P["dd20"] <= -0.10)]),
        ("极端超跌 dd≤-20%", P[P["dd20"] <= -0.20]),
        ("远离MA60≤-15%", P[P["dist_ma60"] <= -0.15]),
        ("破布林下轨 bb<0", P[P["bb_pct"] < 0]),
        ("RSI<20", P[P["rsi14"] < 20]),
        ("MA60上+RSI<30", P[P["above_ma60"] & (P["rsi14"] < 30)]),
        ("破布林+MA60上", P[(P["bb_pct"] < 0) & P["above_ma60"]]),
        ("MA60上+贴新高(-1%)", P[P["above_ma60"] & (P["dd20"] >= -0.01)]),
    ]
    rows = [stat_row(d_, lab) for lab, d_ in cands]
    print(pd.DataFrame(rows).set_index("subset").to_string(
        float_format=lambda x: f"{x:.3f}"))

    print("\n" + "=" * 76)
    print("Part B  range 细化：ratio 子带 × MA60 斜率")
    print("=" * 76)
    g = P.groupby(["band", "slope"], observed=True).apply(
        lambda d_: pd.Series(stat_row(d_, "")), include_groups=False)
    print(g[["n", "w10", "r10", "w20", "r20"]].to_string(float_format=lambda x: f"{x:.3f}"))
    print("\n  斜率×结构 交叉（range 内）:")
    for slope in ["MA60上行", "MA60下行"]:
        d_ = P[(P["slope"] == slope) & P["above_ma60"]]
        r = stat_row(d_, f"{slope}+站上MA60")
        print(f"  {r}")

    print("\n" + "=" * 76)
    print("Part C  range 段落生命周期")
    print("=" * 76)
    Q = P[P["ep_third"].notna()]
    print("C1 按段落位置（third）:")
    g = Q.groupby("ep_third").apply(lambda d_: pd.Series(stat_row(d_, "")), include_groups=False)
    print(g[["n", "w10", "r10", "w20", "r20"]].to_string(float_format=lambda x: f"{x:.3f}"))
    print("\nC2 按段落结局（exit_dir）× 位置:")
    g = Q.groupby(["ep_exit", "ep_third"]).apply(
        lambda d_: pd.Series(stat_row(d_, "")), include_groups=False)
    print(g[["n", "w10", "r10", "w20", "r20"]].to_string(float_format=lambda x: f"{x:.3f}"))
    print("\nC3 站在MA60上的股票，按段落位置:")
    Q2 = Q[Q["above_ma60"]]
    g = Q2.groupby("ep_third").apply(lambda d_: pd.Series(stat_row(d_, "")), include_groups=False)
    print(g[["n", "w10", "r10", "w20", "r20"]].to_string(float_format=lambda x: f"{x:.3f}"))

    print("\n" + "=" * 76)
    print("Part D  候选'range 小仓位首笔'规则（事件级）")
    print("=" * 76)
    for name, evs in rule_events.items():
        a = np.array([e for e in evs if not any(pd.isna(x) for x in e)])
        if len(a) < 30:
            print(f"  {name}: 样本不足 n={len(a)}")
            continue
        print(f"  {name}: n={len(a)}  w10={ (a[:,1]>0).mean():.3f} r10={a[:,1].mean():+.3f}  "
              f"w20={(a[:,2]>0).mean():.3f} r20={a[:,2].mean():+.3f}  "
              f"p5_10={np.percentile(a[:,1],5):+.3f}  "
              f"若1/3仓: 满配口径r10={a[:,1].mean()/3:+.3f}")


if __name__ == "__main__":
    main()
