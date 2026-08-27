# -*- coding: utf-8 -*-
"""建仓因子挖掘 + 分批/逢低建仓对比实验（2026-08-27）
纯离线：t_io/cache/daily_kline 全量缓存（约2570只 × 800天，qfq），不联网、不改状态。

Part 1 因子挖掘：候选因子分桶 → 前瞻5/10/20日收益统计（总体 + 按指数regime分层）
Part 2 建仓规则对比：现行(追强/抄底) vs 逢低变体 vs 放宽变体，事件级（去重）对比
Part 3 分批 vs 一步到位：同一触发点上比较两种建仓机械的期望收益与尾部风险

口径注意：
- regime 判定与 core/timing_gate.py 完全一致（上证 close vs MA60×1.005/×0.97）
- 事件=条件由假转真的首日（避免连续触发日重复计数）
- 前瞻收益 close→close，含 2024-2026 上行段行情偏差；缓存池有选股偏差，仅相对比较
"""
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(BASE, "t_io", "cache", "daily_kline")

UP_BUF, DN_BUF = 1.005, 0.97
MIN_ROWS = 300          # 个股最少日线行数
HORIZONS = (5, 10, 20)

# ---------------- 数据加载 ----------------

def load_index():
    d = json.load(open(os.path.join(CACHE_DIR, "index_sh000001.json"), encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["close"] = df["close"].astype(float)
    df["ma60"] = df["close"].rolling(60).mean()
    ratio = df["close"] / df["ma60"]
    df["regime"] = np.where(ratio > UP_BUF, "trend_up",
                    np.where(ratio < DN_BUF, "trend_dn",
                             np.where(df["ma60"].isna(), "unknown", "range")))
    return dict(zip(df["date"].astype(str), df["regime"]))


def load_stock(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    rows = d.get("rows", d) if isinstance(d, dict) else d
    if len(rows) < MIN_ROWS:
        return None
    df = pd.DataFrame(rows)
    if df.empty or "close" not in df.columns or "high" not in df.columns:
        return None
    for col in ("close", "high", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = df["date"].astype(str)
    return df.sort_values("date").reset_index(drop=True)


def add_features(df):
    """与 timing_gate._stock_features 同口径 + 挖掘用扩展因子。无未来函数。"""
    c, h, v = df["close"], df["high"], df["volume"]
    df["ma20"] = c.rolling(20).mean()
    df["ma60"] = c.rolling(60).mean()
    df["dd20"] = c / h.rolling(20).max() - 1                      # 距20日高点回撤（≤0）
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, math.nan))
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False).mean()
    cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    df["golden5d"] = cross.rolling(5).max().fillna(0).astype(int)  # 近5日有金叉
    df["dist_ma20"] = c / df["ma20"] - 1
    df["dist_ma60"] = c / df["ma60"] - 1
    df["vol_ratio20"] = v / v.rolling(20).mean()
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    df["bb_pct"] = (c - (mid - 2 * sd)) / (4 * sd)                 # BOLL %b
    df["trend_struct"] = (c > df["ma20"]) & (c > df["ma60"])       # 多头结构
    df["above_ma60"] = c > df["ma60"]
    df["rsi_up"] = (df["rsi14"] > df["rsi14"].shift(1))            # 止跌确认
    df["up_day"] = c > c.shift(1)
    for h_ in HORIZONS:
        df[f"fwd{h_}"] = c.shift(-h_) / c - 1
    return df


# ---------------- Part 1: 因子分桶 ----------------

BINS = {
    "dd20":       [-1, -0.20, -0.15, -0.10, -0.08, -0.05, -0.03, -0.01, 0.01],
    "rsi14":      [0, 20, 30, 40, 50, 60, 70, 80, 101],
    "dist_ma20":  [-1, -0.10, -0.05, -0.02, 0, 0.02, 0.05, 0.10, 10],
    "dist_ma60":  [-1, -0.15, -0.10, -0.05, 0, 0.05, 0.10, 0.20, 10],
    "vol_ratio20": [0, 0.5, 0.8, 1.2, 2.0, 3.0, 1e9],
    "bb_pct":     [-1e9, 0, 0.2, 0.4, 0.6, 0.8, 1.0, 1e9],
}


def bucket_stats(panel, factor, by_regime=False):
    edges = BINS[factor]
    lab = pd.cut(panel[factor], edges, right=False)
    keys = [lab] if not by_regime else [panel["regime"], lab]
    g = panel.groupby(keys, observed=True)
    out = g.agg(n=("fwd5", "size"),
                w5=("fwd5", lambda s: (s > 0).mean()),
                r5=("fwd5", "mean"),
                w10=("fwd10", lambda s: (s > 0).mean()),
                r10=("fwd10", "mean"),
                w20=("fwd20", lambda s: (s > 0).mean()),
                r20=("fwd20", "mean"))
    return out


# ---------------- Part 2: 规则对比 ----------------

def rule_masks(df, regime_s):
    """返回 {规则名: bool Series}。事件=由假转真首日。"""
    up = regime_s == "trend_up"
    dn = regime_s == "trend_dn"
    rng = regime_s == "range"
    known = up | dn | rng
    m = {}
    # R0 现行：追强 + 抄底
    m["R0现行_追强(up)"] = up & df["trend_struct"] & (df["dd20"] >= -0.03)
    m["R0现行_抄底(dn)"] = dn & (df["dd20"] < -0.10) & (df["rsi14"] < 20)
    # R2 放宽：回撤阈值 -3%→-5%，仍要 regime + 多头结构
    m["R2放宽_追强(up,dd≥-5%)"] = up & df["trend_struct"] & (df["dd20"] >= -0.05)
    # R1 逢低：站上MA60 + 回撤5%~20% + RSI不超买
    dip = df["above_ma60"] & (df["dd20"] <= -0.05) & (df["dd20"] >= -0.20) & (df["rsi14"] < 45)
    m["R1逢低_不限regime"] = known & dip
    m["R1b逢低_排除trend_dn"] = (up | rng) & dip
    # R3 逢低+止跌确认
    m["R3逢低+止跌确认"] = (up | rng) & dip & df["rsi_up"] & df["up_day"]
    return m


def events_of(mask):
    mask = mask.fillna(False)
    return mask & ~mask.shift(1, fill_value=False)


# ---------------- Part 3: 分批 vs 一步到位 ----------------

def staged_sim(df, regime_s, allow_dn=False):
    """触发：站上MA60 且 dd20 下穿 -3%（事件日）。窗口10天。
    一步到位：触发日收盘 100% 买入，+15 交易日收盘估值。
    分批：触发日 1/3；窗口内 dd≤-6% 加 1/3；dd≤-9% 再加 1/3；+15 交易日估值。
    返回 list of dict。"""
    ok_regime = regime_s.isin(["trend_up", "range"]) if not allow_dn else regime_s.isin(["trend_up", "range", "trend_dn"])
    trig = df["above_ma60"] & (df["dd20"] <= -0.03) & ok_regime
    ev = events_of(trig)
    idxs = np.flatnonzero(ev.values)
    closes = df["close"].values
    dds = df["dd20"].values
    regs = regime_s.values
    out = []
    N = len(df)
    for i in idxs:
        if i + 15 >= N:
            continue
        entry = closes[i]
        # 分批
        fills = [(i, 1 / 3)]
        for j in range(i + 1, min(i + 11, N)):
            if dds[j] <= -0.06 and len(fills) == 1:
                fills.append((j, 1 / 3))
            elif dds[j] <= -0.09 and len(fills) == 2:
                fills.append((j, 1 / 3))
        cost = sum(closes[j] * w for j, w in fills) / sum(w for _, w in fills)
        frac = sum(w for _, w in fills)
        exit_px = closes[i + 15]
        out.append({
            "regime": regs[i],
            "frac_filled": frac,
            "ret_maxcap_staged": (exit_px / cost - 1) * frac,   # 占满配口径
            "ret_deployed_staged": exit_px / cost - 1,          # 占已投口径
            "ret_oneshot": exit_px / entry - 1,
        })
    return out


# ---------------- 主流程 ----------------

def fmt_stats(df, n_col=True):
    return df.to_string(float_format=lambda x: f"{x:.3f}")


def main():
    regime_map = load_index()
    files = [f for f in glob.glob(os.path.join(CACHE_DIR, "*.json"))
             if "index_" not in os.path.basename(f)]
    print(f"缓存文件: {len(files)}")

    panel_parts = []
    rule_events = defaultdict(list)      # rule -> list of (fwd5,fwd10,fwd20,regime)
    staged_rows = []
    n_used = 0
    for k, path in enumerate(files):
        df = load_stock(path)
        if df is None:
            continue
        df = add_features(df)
        regime_s = df["date"].map(regime_map).fillna("unknown")
        usable = df["ma60"].notna() & df["fwd10"].notna() & (regime_s != "unknown")
        if usable.sum() < 50:
            continue
        n_used += 1
        sub = df.loc[usable].copy()
        sub["regime"] = regime_s[usable].values
        panel_parts.append(sub[["dd20", "rsi14", "dist_ma20", "dist_ma60", "vol_ratio20",
                                "bb_pct", "golden5d", "fwd5", "fwd10", "fwd20", "regime"]]
                             .astype({"dd20": "float32", "rsi14": "float32",
                                      "dist_ma20": "float32", "dist_ma60": "float32",
                                      "vol_ratio20": "float32", "bb_pct": "float32"}))
        # Part 2 规则事件
        masks = rule_masks(df, regime_s)
        for rname, mask in masks.items():
            ev = events_of(mask)
            eidx = np.flatnonzero(ev.values)
            for i in eidx:
                if i + 20 >= len(df):
                    continue
                rule_events[rname].append((
                    df["fwd5"].iat[i], df["fwd10"].iat[i], df["fwd20"].iat[i],
                    regime_s.iat[i]))
        # Part 3 分批模拟（排除 trend_dn 版）
        staged_rows.extend(staged_sim(df, regime_s, allow_dn=False))
        if (k + 1) % 500 == 0:
            print(f"  进度 {k + 1}/{len(files)}，已用 {n_used}", flush=True)

    panel = pd.concat(panel_parts, ignore_index=True)
    print(f"\n可用标的: {n_used}，股票-日样本: {len(panel):,}")
    print(f"regime 分布: {dict(Counter(panel['regime']))}")

    print("\n" + "=" * 78)
    print("Part 1  因子分桶（w=胜率 r=平均收益，fwd 单位为小数）")
    print("=" * 78)
    for fac in ["dd20", "rsi14", "dist_ma20", "dist_ma60", "vol_ratio20", "bb_pct"]:
        print(f"\n--- {fac} ---")
        print(fmt_stats(bucket_stats(panel, fac)))
    print("\n--- golden5d（近5日MACD金叉） ---")
    g = panel.groupby("golden5d").agg(n=("fwd5", "size"),
                                      w5=("fwd5", lambda s: (s > 0).mean()),
                                      r5=("fwd5", "mean"),
                                      w10=("fwd10", lambda s: (s > 0).mean()),
                                      r10=("fwd10", "mean"))
    print(fmt_stats(g))
    print("\n--- regime 本身作为因子（检验方向门控价值） ---")
    g = panel.groupby("regime").agg(n=("fwd5", "size"),
                                    w5=("fwd5", lambda s: (s > 0).mean()),
                                    r5=("fwd5", "mean"),
                                    w10=("fwd10", lambda s: (s > 0).mean()),
                                    r10=("fwd10", "mean"),
                                    w20=("fwd20", lambda s: (s > 0).mean()),
                                    r20=("fwd20", "mean"))
    print(fmt_stats(g))
    print("\n--- dd20 × regime 分层 ---")
    print(fmt_stats(bucket_stats(panel, "dd20", by_regime=True)))

    print("\n" + "=" * 78)
    print("Part 2  建仓规则对比（事件级，fwd 前瞻收益）")
    print("=" * 78)
    rows = []
    for rname, evs in rule_events.items():
        a = np.array([(e[0], e[1], e[2]) for e in evs if not any(pd.isna(x) for x in e[:3])])
        if len(a) == 0:
            continue
        regs = Counter(e[3] for e in evs)
        rows.append({
            "rule": rname, "n": len(a),
            "w5": (a[:, 0] > 0).mean(), "r5": a[:, 0].mean(),
            "w10": (a[:, 1] > 0).mean(), "r10": a[:, 1].mean(),
            "w20": (a[:, 2] > 0).mean(), "r20": a[:, 2].mean(),
            "p5_fwd10": np.percentile(a[:, 1], 5),   # 尾部风险
            "regimes": dict(regs),
        })
    cmp_df = pd.DataFrame(rows).set_index("rule")
    print(cmp_df[["n", "w5", "r5", "w10", "r10", "w20", "r20", "p5_fwd10"]]
          .to_string(float_format=lambda x: f"{x:.3f}"))
    for rname, evs in rule_events.items():
        print(f"  {rname} regime分布: {dict(Counter(e[3] for e in evs))}")

    print("\n" + "=" * 78)
    print("Part 3  分批逢低 vs 一步到位（同触发点：站上MA60 & dd下穿-3%，排除trend_dn）")
    print("=" * 78)
    s = pd.DataFrame(staged_rows)
    if len(s):
        print(f"setup 数: {len(s)}")
        agg = {
            "n": len(s),
            "平均填充比例": s["frac_filled"].mean(),
            "仅1/3成交占比": (s["frac_filled"] < 0.5).mean(),
            "满仓成交占比": (s["frac_filled"] > 0.99).mean(),
        }
        print(agg)
        comp = pd.DataFrame({
            "mean": [s["ret_oneshot"].mean(), s["ret_maxcap_staged"].mean(), s["ret_deployed_staged"].mean()],
            "median": [s["ret_oneshot"].median(), s["ret_maxcap_staged"].median(), s["ret_deployed_staged"].median()],
            "win": [(s["ret_oneshot"] > 0).mean(), (s["ret_maxcap_staged"] > 0).mean(), (s["ret_deployed_staged"] > 0).mean()],
            "p5(尾部)": [np.percentile(s["ret_oneshot"], 5), np.percentile(s["ret_maxcap_staged"], 5), np.percentile(s["ret_deployed_staged"], 5)],
            "p95": [np.percentile(s["ret_oneshot"], 95), np.percentile(s["ret_maxcap_staged"], 95), np.percentile(s["ret_deployed_staged"], 95)],
        }, index=["一步到位(100%@触发)", "分批(占满配口径)", "分批(占已投口径)"])
        print(comp.to_string(float_format=lambda x: f"{x:.4f}"))
        print("\n按填充程度分组（分批, 占已投口径）:")
        s["fill_grp"] = pd.cut(s["frac_filled"], [0, 0.5, 0.99, 1.01], labels=["仅1/3", "2/3", "满仓"])
        print(s.groupby("fill_grp", observed=True).agg(
            n=("ret_deployed_staged", "size"),
            ret_deployed=("ret_deployed_staged", "mean"),
            ret_oneshot=("ret_oneshot", "mean")).to_string(float_format=lambda x: f"{x:.4f}"))
        print("\n按 regime 分组:")
        print(s.groupby("regime").agg(
            n=("ret_oneshot", "size"),
            oneshot=("ret_oneshot", "mean"),
            staged_maxcap=("ret_maxcap_staged", "mean"),
            staged_deployed=("ret_deployed_staged", "mean")).to_string(float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()
