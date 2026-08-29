# -*- coding: utf-8 -*-
"""建仓信号触发率 1 年回测（2026-08-27）
纯离线：只读 t_io/cache/daily_kline 缓存 + watchlist_buy.json，不联网、不改状态。
口径 = timing_gate.timing_verdict 逐日复刻（含 2026-08-27 否决因子：爆量≥3x/偏离MA60>20%）。

目标：回答"新建仓逻辑（含否决因子）触发率到底多低"——
  · 结构可达性：signal 只在 trend_up/trend_dn 可达，range 永不触发
  · 实际触发数/触发率（分母=全部股票-日 与 分母=趋势日股票-日 两种口径）
  · 否决因子影响：有多少本会 GO 的 signal 被爆量/远离MA60 拦掉
  · 触发质量：fwd5/fwd10 胜率与均值（仅评估，不参与触发）
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(BASE, "t_io", "cache", "daily_kline")
WATCHLIST = os.path.join(BASE, "t_io", "state", "watchlist_buy.json")

UP_BUF, DN_BUF = 1.005, 0.97          # 现行 regime 缓冲带（timing_gate.py:138-143）
DD_UP, DD_DN, RSI_LIM = -0.03, -0.10, 20   # 现行回撤/RSI 阈值
VETO_VOL, VETO_DIST = 3.0, 0.20       # 现行否决阈值（timing_gate.py:210-217）
MIN_ROWS = 61                         # 与 timing_gate 一致的最小数据量
WINDOW = 250                          # 回测窗口：最近 250 个交易日


def load_json(fp, default):
    try:
        return json.load(open(fp, encoding="utf-8"))
    except Exception:
        return default


def load_index():
    d = load_json(os.path.join(CACHE_DIR, "index_sh000001.json"), {})
    df = pd.DataFrame(d.get("rows", d))
    df["date"] = df["date"].astype(str)
    df["close"] = df["close"].astype(float)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def index_regime_series(idx):
    """逐日 regime（截止当日，无未来）：close vs MA60*1.005 / MA60*0.97。"""
    c = idx["close"]
    ma60 = c.rolling(60).mean()
    ratio = c / ma60
    regime = []
    for r in ratio:
        if pd.isna(r):
            regime.append("unknown")
        elif r > UP_BUF:
            regime.append("trend_up")
        elif r < DN_BUF:
            regime.append("trend_dn")
        else:
            regime.append("range")
    idx = idx.copy()
    idx["regime"] = regime
    return idx


def load_stock(code):
    p = os.path.join(CACHE_DIR, f"{code}.json")
    if not os.path.exists(p):
        return None
    d = load_json(p, {})
    rows = d.get("rows", d)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for col in ("close", "high", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = df["date"].astype(str)
    return df.sort_values("date").reset_index(drop=True)


def stock_features_vectorized(df):
    """向量化计算 timing_gate._stock_features 的逐日特征（无未来）。"""
    c = df["close"]
    df = df.copy()
    df["ma20"] = c.rolling(20).mean()
    df["ma60"] = c.rolling(60).mean()
    df["rec_high20"] = df["high"].rolling(20).max()
    df["drawdown"] = c / df["rec_high20"] - 1
    df["dist_ma60"] = c / df["ma60"] - 1
    df["trend_multihead"] = (c > df["ma20"]) & (c > df["ma60"])
    # MACD 金叉（近5日任一）
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False).mean()
    cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    df["golden_5d"] = cross.rolling(5, min_periods=1).max().astype(bool)
    # RSI(14)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - 100 / (1 + rs)
    # 爆量否决：今日量 / 20日均量
    v = df["volume"]
    v20 = v.rolling(20).mean()
    df["vol_ratio20"] = v / v20
    # 前瞻收益（仅评估）
    df["fwd5"] = c.shift(-5) / c - 1
    df["fwd10"] = c.shift(-10) / c - 1
    return df


def main():
    idx = index_regime_series(load_index())
    valid = idx[idx["regime"] != "unknown"].reset_index(drop=True)
    # 回测窗口：最近 WINDOW 个交易日
    win = valid.tail(WINDOW).reset_index(drop=True)
    win_dates = set(win["date"])
    regime_by_date = dict(zip(valid["date"], valid["regime"]))
    print(f"回测窗口: {win['date'].iloc[0]} ~ {win['date'].iloc[-1]}  ({len(win)} 个交易日)")

    # 市场状态可达性
    rc = Counter(win["regime"])
    n = len(win)
    reach = rc.get("trend_up", 0) + rc.get("trend_dn", 0)
    print("\n=== 市场状态可达性（signal 只在 trend_up/trend_dn 可达）===")
    print(f"trend_up={rc.get('trend_up',0):3d} ({rc.get('trend_up',0)/n:.1%})  "
          f"trend_dn={rc.get('trend_dn',0):3d} ({rc.get('trend_dn',0)/n:.1%})  "
          f"range={rc.get('range',0):3d} ({rc.get('range',0)/n:.1%})  "
          f"signal可达占比={reach/n:.1%}")

    # 候选池
    w = load_json(WATCHLIST, {})
    stocks = w.get("stocks", w)
    pool = [(str(k).zfill(6), v.get("name", "")) for k, v in stocks.items()
            if v.get("status") in ("monitoring", "signal") and str(k).isdigit()]
    print(f"候选池: {len(pool)} 只")

    # 逐股逐日判定
    rows = []      # 所有 (code,date) 的判定
    sig_rows = []  # go=True 的 signal
    veto_rows = []  # trend_up 下 go 本为真但被否决的
    no_data = []
    for code, name in pool:
        df = load_stock(code)
        if df is None or len(df) < MIN_ROWS:
            no_data.append(code)
            continue
        df = stock_features_vectorized(df)
        for _, r in df.iterrows():
            date = r["date"]
            if date not in win_dates:
                continue
            regime = regime_by_date.get(date, "range")
            # 数据不足：与 timing_gate 一致（截止当日不足 61 根）
            if df[df["date"] <= date].shape[0] < MIN_ROWS:
                rows.append({"code": code, "name": name, "date": date, "regime": regime,
                             "go": False, "veto": [], "reason": "数据不足"})
                continue
            go = False
            vetoes = []
            reason = ""
            if regime == "trend_up":
                vr = r["vol_ratio20"]
                dm = r["dist_ma60"]
                if vr is not None and not math.isnan(vr) and vr >= VETO_VOL:
                    vetoes.append(f"爆量{vr:.1f}x")
                if dm is not None and not math.isnan(dm) and dm > VETO_DIST:
                    vetoes.append(f"离MA60+{dm:.1%}")
                base_ok = bool(r["trend_multihead"]) and r["drawdown"] >= DD_UP
                go = base_ok and not vetoes
                reason = f"trend_up: 多头{bool(r['trend_multihead'])} 回撤{r['drawdown']:+.1%} 否决{len(vetoes)}"
                if base_ok and vetoes:
                    veto_rows.append({"code": code, "name": name, "date": date, "veto": vetoes,
                                      "drawdown": r["drawdown"], "fwd5": r["fwd5"], "fwd10": r["fwd10"]})
            elif regime == "trend_dn":
                dd_ok = r["drawdown"] < DD_DN
                rsi_ok = (r["rsi"] if not math.isnan(r["rsi"]) else 50) < RSI_LIM
                go = dd_ok and rsi_ok
                reason = f"trend_dn: 回撤{r['drawdown']:+.1%} RSI{r['rsi']:.0f}"
            else:
                reason = "range: 降频不触发"
            rows.append({"code": code, "name": name, "date": date, "regime": regime,
                         "go": bool(go), "veto": vetoes, "reason": reason,
                         "fwd5": r["fwd5"], "fwd10": r["fwd10"]})
            if go:
                sig_rows.append({"code": code, "name": name, "date": date, "regime": regime,
                                 "fwd5": r["fwd5"], "fwd10": r["fwd10"]})

    sdf = pd.DataFrame(rows)
    pool_day_total = len(sdf)
    trend_pool_days = int(((sdf["regime"] == "trend_up") | (sdf["regime"] == "trend_dn")).sum())
    n_sig = len(sig_rows)

    print(f"\n=== 触发率（1 年，{len(pool)} 只 × {len(win)} 日）===")
    print(f"全部 股票-日: {pool_day_total}")
    print(f"趋势日 股票-日: {trend_pool_days} ({trend_pool_days/pool_day_total:.1%})")
    print(f"signal 触发数: {n_sig}")
    print(f"触发率(分母=全部股票-日): {n_sig/pool_day_total:.2%}")
    print(f"触发率(分母=趋势日股票-日): {n_sig/trend_pool_days if trend_pool_days else 0:.2%}")

    if sig_rows:
        sig = pd.DataFrame(sig_rows)
        print(f"\n=== 触发分布 ===")
        print("按 regime:\n", sig.groupby("regime").size().to_string())
        sig["month"] = sig["date"].str[:7]
        print("\n按月份:\n", sig.groupby("month").size().to_string())
        print("\n按标的(触发≥1次):\n", sig.groupby(["code", "name"]).size().sort_values(ascending=False).to_string())
        print("\n=== 触发质量（fwd 收益，仅评估）===")
        sig["fwd5"] = sig["fwd5"].astype(float)
        sig["fwd10"] = sig["fwd10"].astype(float)
        for col in ("fwd5", "fwd10"):
            sub = sig.dropna(subset=[col])
            if len(sub):
                win_r = (sub[col] > 0).mean()
                print(f"{col}: n={len(sub)} 胜率={win_r:.1%} 均值={sub[col].mean():+.2%} "
                      f"中位={sub[col].median():+.2%} 最好={sub[col].max():+.2%} 最差={sub[col].min():+.2%}")

    if veto_rows:
        vdf = pd.DataFrame(veto_rows)
        print(f"\n=== 否决因子影响（trend_up 下被拦掉的潜在 GO）===")
        print(f"被否决数: {len(vdf)}  (本会 go 但因否决因子被拦)")
        vc = Counter()
        for vs in vdf["veto"]:
            has_vol = any("爆量" in v for v in vs)
            has_dist = any("MA60" in v for v in vs)
            if has_vol and has_dist:
                vc["爆量+离MA60 双因子"] += 1
            elif has_vol:
                vc["爆量≥3x"] += 1
            elif has_dist:
                vc["离MA60>20%"] += 1
        print("按因子:\n", "\n".join(f"  {k}: {n}次" for k, n in vc.most_common()))
        vdf["fwd5"] = vdf["fwd5"].astype(float)
        vdf["fwd10"] = vdf["fwd10"].astype(float)
        for col in ("fwd5", "fwd10"):
            sub = vdf.dropna(subset=[col])
            if len(sub):
                print(f"被否决样本 {col}: n={len(sub)} 胜率={(sub[col]>0).mean():.1%} 均值={sub[col].mean():+.2%}")

    if no_data:
        print(f"\n无缓存/数据不足: {no_data}")
    print("\n注: 口径=timing_gate 现行规则逐日复刻（含否决因子），fwd 收益含行情/幸存者偏差，仅相对比较。")


if __name__ == "__main__":
    main()
