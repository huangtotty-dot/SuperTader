# -*- coding: utf-8 -*-
"""建仓条件实现审视实验（2026-08-27）
纯离线：只读 t_io/cache/daily_kline 缓存 + watchlist_buy.json，不联网、不改状态。

实验1：市场状态(re regime)可达性 —— 现行规则下 signal 在时间上有多大比例结构性不可达
实验2：反事实 signal 密度 —— 市场有方向的日子里，当前 41 只池子实际能出多少 signal，及前瞻收益
实验3：阈值敏感性 —— regime 缓冲带 / 回撤阈值微调对 signal 密度的杠杆效应
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(BASE, "t_io", "cache", "daily_kline")
WATCHLIST = os.path.join(BASE, "t_io", "state", "watchlist_buy.json")

UP_BUF, DN_BUF = 1.005, 0.97          # 现行 regime 缓冲带（timing_gate.py:136-143）
DD_UP, DD_DN, RSI_LIM = -0.03, -0.10, 20  # 现行回撤/RSI 阈值（timing_gate.py:198/209-215）


def load_index():
    d = json.load(open(os.path.join(CACHE_DIR, "index_sh000001.json"), encoding="utf-8"))
    df = pd.DataFrame(d["rows"])
    df["close"] = df["close"].astype(float)
    df["ma60"] = df["close"].rolling(60).mean()
    df["ratio"] = df["close"] / df["ma60"]
    return df


def classify(ratio, up=UP_BUF, dn=DN_BUF):
    if pd.isna(ratio):
        return "unknown"
    if ratio > up:
        return "trend_up"
    if ratio < dn:
        return "trend_dn"
    return "range"


def load_pool():
    w = json.load(open(WATCHLIST, encoding="utf-8"))
    stocks = w.get("stocks", w)
    if isinstance(stocks, dict):
        items = [(k, v) for k, v in stocks.items()]   # 键即代码，条目内无 code 字段
    else:
        items = [(str(it.get("code", "")), it) for it in stocks]
    pool = []
    for key, it in items:
        code = str(it.get("code", "") or key).zfill(6)
        status = it.get("status", "")
        if status in ("monitoring", "signal") and code and not code.startswith("_"):
            pool.append((code, it.get("name", "")))
    return pool


def load_stock(code):
    p = os.path.join(CACHE_DIR, f"{code}.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p, encoding="utf-8"))
    rows = d.get("rows", d) if isinstance(d, dict) else d
    df = pd.DataFrame(rows)
    if df.empty or "close" not in df.columns:
        return None
    for col in ("close", "high"):
        df[col] = df[col].astype(float)
    df["date"] = df["date"].astype(str)
    return df.sort_values("date").reset_index(drop=True)


def stock_features(df):
    """逐日向量化计算与 timing_gate._stock_features 相同的特征（无未来）。"""
    c = df["close"]
    df["ma20"] = c.rolling(20).mean()
    df["ma60"] = c.rolling(60).mean()
    df["rec_high20"] = df["high"].rolling(20).max()
    df["drawdown"] = c / df["rec_high20"] - 1
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, math.nan))
    df["fwd5"] = c.shift(-5) / c - 1     # 前瞻5日收益（仅用于信号质量评估）
    df["fwd10"] = c.shift(-10) / c - 1
    return df


def main():
    idx = load_index()
    idx["regime"] = idx["ratio"].apply(classify)
    valid = idx.dropna(subset=["ma60"]).copy()

    print("=" * 70)
    print("实验1：regime 可达性（signal 只在 trend_up/trend_dn 可达）")
    print("=" * 70)
    for label, sub in [("全部(MA60可用起)", valid),
                       ("近250交易日", valid.tail(250)),
                       ("近60交易日", valid.tail(60)),
                       ("近20交易日", valid.tail(20))]:
        cnt = Counter(sub["regime"])
        n = len(sub)
        reachable = cnt.get("trend_up", 0) + cnt.get("trend_dn", 0)
        print(f"{label:>14}: n={n:4d}  trend_up={cnt.get('trend_up',0):3d}  "
              f"trend_dn={cnt.get('trend_dn',0):3d}  range={cnt.get('range',0):3d}  "
              f"可达占比={reachable / n:.1%}")
    # 最长连续 range（结构性封锁）段
    streak = best = 0
    cur_end = None
    regimes = list(valid["regime"])
    dates = list(valid["date"])
    for i, r in enumerate(regimes):
        streak = streak + 1 if r == "range" else 0
        if streak > best:
            best, cur_end = streak, i
    cur = 0
    for r in reversed(regimes):
        if r == "range":
            cur += 1
        else:
            break
    print(f"历史最长连续 range: {best} 天（止于 {dates[cur_end]}）")
    print(f"当前连续 range: {cur} 天（自 {dates[len(regimes) - cur]} 起）")
    last = valid.iloc[-1]
    print(f"最新: {last['date']} close={last['close']:.2f} MA60={last['ma60']:.2f} "
          f"ratio={last['ratio']:.4f} → {last['regime']}")
    print(f"  转 trend_up 需指数 > {last['ma60'] * UP_BUF:.1f}（{(last['ma60'] * UP_BUF / last['close'] - 1):+.2%}）")
    print(f"  转 trend_dn 需指数 < {last['ma60'] * DN_BUF:.1f}（{(last['ma60'] * DN_BUF / last['close'] - 1):+.2%}）")

    pool = load_pool()
    print(f"\n股票池: {len(pool)} 只")

    print("\n" + "=" * 70)
    print("实验2：反事实 signal 密度 + 前瞻收益（趋势日逐日判定，口径=timing_gate）")
    print("=" * 70)
    regime_by_date = dict(zip(valid["date"], valid["regime"]))
    sig_rows = []
    trend_day_cnt = Counter()
    pool_day_total = 0
    no_data = []
    for code, name in pool:
        df = load_stock(code)
        if df is None or len(df) < 70:
            no_data.append(code)
            continue
        df = stock_features(df)
        for _, row in df.iloc[60:].iterrows():
            rg = regime_by_date.get(row["date"])
            if rg not in ("trend_up", "trend_dn"):
                continue
            trend_day_cnt[rg] += 1
            pool_day_total += 1
            sig = False
            if rg == "trend_up":
                sig = bool(row["close"] > row["ma20"] and row["close"] > row["ma60"]
                           and row["drawdown"] >= DD_UP)
            else:
                sig = bool(row["drawdown"] < DD_DN and row["rsi14"] < RSI_LIM)
            if sig:
                sig_rows.append({"code": code, "name": name, "date": row["date"],
                                 "regime": rg, "fwd5": row["fwd5"], "fwd10": row["fwd10"]})
    sig = pd.DataFrame(sig_rows)
    print(f"趋势日 股票-日 样本: {pool_day_total}（trend_up={trend_day_cnt['trend_up']}, "
          f"trend_dn={trend_day_cnt['trend_dn']}）")
    if len(sig):
        sig["fwd5"] = sig["fwd5"].astype(float)
        sig["fwd10"] = sig["fwd10"].astype(float)
        print(f"反事实 signal 总数: {len(sig)}  密度={len(sig) / pool_day_total:.2%} of 趋势股票-日")
        by_rg = sig.groupby("regime").agg(
            n=("code", "size"),
            fwd5_win=("fwd5", lambda s: (s > 0).mean()),
            fwd5_avg=("fwd5", "mean"),
            fwd10_win=("fwd10", lambda s: (s > 0).mean()),
            fwd10_avg=("fwd10", "mean"))
        print(by_rg.to_string(float_format=lambda x: f"{x:.3f}"))
        # 按年分布（信号是否集中在某段行情）
        sig["year"] = sig["date"].str[:4]
        print("\n按年分布:\n", sig.groupby(["year", "regime"]).size().to_string())
        print("\n近250日内 signal 数:", len(sig[sig["date"] >= valid.tail(250)["date"].iloc[0]]))
        top = sig.groupby(["code", "name"]).size().sort_values(ascending=False).head(8)
        print("signal 最多的标的:\n", top.to_string())
    else:
        print("无任何反事实 signal！")
    if no_data:
        print(f"\n无缓存/数据不足标的({len(no_data)}): {no_data}")

    print("\n" + "=" * 70)
    print("实验3：阈值敏感性（近250趋势可达日，trend_up 口径）")
    print("=" * 70)
    recent_dates = set(valid.tail(250)["date"])
    # regime 缓冲带敏感性：改缓冲带会改 regime 划分本身
    print("a) regime 缓冲带 → 近250日中各 regime 天数：")
    for up, dn in [(1.005, 0.97), (1.002, 0.98), (1.0, 0.99), (1.01, 0.95)]:
        rc = Counter(classify(r, up, dn) for r in valid.tail(250)["ratio"])
        print(f"  up={up:<5} dn={dn:<4}: trend_up={rc.get('trend_up',0):3d} "
              f"trend_dn={rc.get('trend_dn',0):3d} range={rc.get('range',0):3d}")
    # 回撤阈值敏感性：在现行 regime 的趋势日里，改 DD_UP
    print("b) 多头回撤阈值 → signal 股票-日数（近250日趋势日，现行 regime）：")
    up_days = {d for d, r in regime_by_date.items() if r == "trend_up"} & recent_dates
    for dd in [-0.03, -0.05, -0.08, -0.12]:
        n_sig = 0
        n_tot = 0
        for code, name in pool:
            df = load_stock(code)
            if df is None or len(df) < 70:
                continue
            df = stock_features(df)
            sub = df[df["date"].isin(up_days)]
            sub = sub.iloc[60:] if len(sub) > 60 else sub.dropna(subset=["ma60"])
            n_tot += len(sub)
            n_sig += int(((sub["close"] > sub["ma20"]) & (sub["close"] > sub["ma60"])
                          & (sub["drawdown"] >= dd)).sum())
        print(f"  回撤>={dd:+.0%}: signal={n_sig:4d} / {n_tot} 股票-日 ({(n_sig / n_tot if n_tot else 0):.2%})")

    print("\n说明: 反事实口径与 timing_gate.py 完全一致；前瞻收益含 2024-2026 上行段幸存者/行情偏差，"
          "仅用于相对比较，不构成收益断言。")


if __name__ == "__main__":
    main()
