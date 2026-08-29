# -*- coding: utf-8 -*-
"""退出策略因子挖掘（2026-08-28）
目标：对比当前 MA5 破位清仓 与其他止损/止盈/退出规则的表现，寻找更优的 auto 卖出保护策略。

口径：
- 纯离线，数据源 t_io/cache/daily_kline 全量缓存（2570×800 日，qfq）
- 入口沿用 build_factor_mining_20260827.py 的 R0 建仓信号（追强/抄底），模拟买入后持有
- 各退出规则在同一 entry 集合上对比，看相对优劣（非绝对收益）
- 初步用日线 close 模拟触发；更精细的 intraday 触发可用 1 年分钟数据二次验证
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
MIN_ROWS = 300
MAX_HOLD = 20


def target_codes():
    """实验对象 = auto 池（18 只）+ watchlist_buy 中 manual 池股票。
    比全市场 2570 只更贴近 auto/manual 实盘，且计算量可控。"""
    codes = set()
    try:
        sys.path.insert(0, BASE)
        from config.auto_pool import auto_pool_codes
        codes.update(auto_pool_codes())
        sys.path.pop(0)
    except Exception:
        pass
    try:
        wl_path = os.path.join(BASE, "t_io", "state", "watchlist_buy.json")
        if os.path.exists(wl_path):
            wl = json.load(open(wl_path, encoding="utf-8"))
            for c in (wl.get("stocks") or {}):
                codes.add(str(c).split("_")[0])
    except Exception:
        pass
    return sorted(codes)


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
    for col in ("close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = df["date"].astype(str)
    return df.sort_values("date").reset_index(drop=True)


def add_features(df):
    """无未来函数。增加退出分析用特征。"""
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["ma5"] = c.rolling(5).mean().shift(1)          # 当日静态 MA5（不含今日 close）
    df["ma10"] = c.rolling(10).mean().shift(1)
    df["ma20"] = c.rolling(20).mean().shift(1)
    df["ma60"] = c.rolling(60).mean().shift(1)
    # ATR
    tr1 = h - l
    tr2 = (h - c.shift(1)).abs()
    tr3 = (l - c.shift(1)).abs()
    df["atr14"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(14).mean().shift(1)
    df["atr20"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(20).mean().shift(1)
    df["dd20"] = c / h.rolling(20).max() - 1
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, math.nan))
    df["trend_struct"] = (c > df["ma20"]) & (c > df["ma60"])
    df["above_ma60"] = c > df["ma60"]
    df["vol_ratio20"] = v / v.rolling(20).mean()
    # 个股趋势状态（简单版：close < ma20 & ma20 < ma60 且 close < ma60 → 下行；close > ma20 > ma60 → 上行）
    df["trend_dn_flag"] = (c < df["ma20"]) & (df["ma20"] < df["ma60"]) & (c < df["ma60"])
    df["trend_break_flag"] = (df["ma20"].diff() < 0) & (c < df["ma20"])
    # 前瞻收益
    for hh in (5, 10, 20, 60):
        df[f"fwd{hh}"] = c.shift(-hh) / c - 1
    return df


def events_of(mask):
    mask = mask.fillna(False)
    return mask & ~mask.shift(1, fill_value=False)


def generate_entries(df, regime_s):
    """生成两种建仓入口：追强（trend_up+多头结构+回撤≤3%）、抄底（trend_dn+深回撤+RSI低）。"""
    up = regime_s == "trend_up"
    dn = regime_s == "trend_dn"
    rng = regime_s == "range"
    known = up | dn | rng
    m = {}
    m["entry_追强_up"] = up & df["trend_struct"] & (df["dd20"] >= -0.03) & df["above_ma60"]
    m["entry_抄底_dn"] = dn & (df["dd20"] < -0.10) & (df["rsi14"] < 20)
    m["entry_逢低_range"] = rng & df["above_ma60"] & (df["dd20"] <= -0.05) & (df["rsi14"] < 45)
    return m


# ---------------- 退出规则统一接口 ----------------
# 每个函数接收 entry_idx, df, entry_price, max_hold -> (exit_idx, exit_price, reason)
# 规则只能使用 entry_idx 之前或当日已知的信息。

def rule_hold_20d(i, df, ep, max_hold=MAX_HOLD):
    j = min(i + max_hold, len(df) - 1)
    return j, df["close"].iat[j], "hold_20d"


def rule_ma_static(i, df, ep, ma_col, name):
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        ma = df[ma_col].iat[j]
        if pd.isna(ma):
            continue
        if df["close"].iat[j] < ma:
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


def rule_atr_trail(i, df, ep, activate_pct=0.08, k=1.5, min_back=0.03, max_back=0.08, name="atr_trail"):
    """激活后跟踪最高价 close，回撤 > max(min_back, min(k*ATR, max_back)) 退出。"""
    peak = ep
    armed = False
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        cp = df["close"].iat[j]
        atr = df["atr14"].iat[j]
        if not armed and cp >= ep * (1 + activate_pct):
            armed = True
            peak = cp
        if armed:
            peak = max(peak, cp)
            back = max(min_back, min(k * atr, max_back)) if pd.notna(atr) else min_back
            if cp < peak * (1 - back):
                return j, cp, name
    return rule_hold_20d(i, df, ep)


def rule_chandelier(i, df, ep, window=10, k=3.0, name="chandelier"):
    """close < 过去 window 日 highest close - k*ATR 退出。"""
    hh_col = df["close"].rolling(window).max().shift(1)
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        hh = hh_col.iat[j]
        atr = df["atr14"].iat[j]
        if pd.isna(hh) or pd.isna(atr):
            continue
        if df["close"].iat[j] < hh - k * atr:
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


def rule_fixed_stop(i, df, ep, pct, name):
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        if df["close"].iat[j] < ep * (1 + pct):
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


def rule_fixed_take(i, df, ep, pct, name):
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        if df["close"].iat[j] > ep * (1 + pct):
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


def rule_rsi_overbought(i, df, ep, rsi_thr=70, name="rsi70_exit"):
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        if df["rsi14"].iat[j] > rsi_thr:
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


def rule_time_stop(i, df, ep, days, name):
    j = min(i + days, len(df) - 1)
    return j, df["close"].iat[j], name


def rule_trend_break(i, df, ep, name="trend_break"):
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        if df["trend_dn_flag"].iat[j] or df["trend_break_flag"].iat[j]:
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


def rule_vol_spike(i, df, ep, name="vol_spike"):
    for j in range(i + 1, min(i + MAX_HOLD + 1, len(df))):
        if df["vol_ratio20"].iat[j] > 3.0:
            return j, df["close"].iat[j], name
    return rule_hold_20d(i, df, ep)


EXIT_RULES = [
    ("current_MA5_static", lambda i, df, ep: rule_ma_static(i, df, ep, "ma5", "current_MA5_static")),
    ("MA10_static", lambda i, df, ep: rule_ma_static(i, df, ep, "ma10", "MA10_static")),
    ("MA20_static", lambda i, df, ep: rule_ma_static(i, df, ep, "ma20", "MA20_static")),
    ("ATR_trail_8_1.5", lambda i, df, ep: rule_atr_trail(i, df, ep, activate_pct=0.08, k=1.5, min_back=0.03, max_back=0.08)),
    ("ATR_trail_5_2.0", lambda i, df, ep: rule_atr_trail(i, df, ep, activate_pct=0.05, k=2.0, min_back=0.03, max_back=0.10)),
    ("chandelier_10_3", lambda i, df, ep: rule_chandelier(i, df, ep, window=10, k=3.0)),
    ("chandelier_22_3", lambda i, df, ep: rule_chandelier(i, df, ep, window=22, k=3.0)),
    ("stop_loss_5pct", lambda i, df, ep: rule_fixed_stop(i, df, ep, -0.05, "stop_loss_5pct")),
    ("stop_loss_8pct", lambda i, df, ep: rule_fixed_stop(i, df, ep, -0.08, "stop_loss_8pct")),
    ("take_profit_10pct", lambda i, df, ep: rule_fixed_take(i, df, ep, 0.10, "take_profit_10pct")),
    ("take_profit_15pct", lambda i, df, ep: rule_fixed_take(i, df, ep, 0.15, "take_profit_15pct")),
    ("RSI70_overbought", lambda i, df, ep: rule_rsi_overbought(i, df, ep, 70, "RSI70_overbought")),
    ("time_stop_5d", lambda i, df, ep: rule_time_stop(i, df, ep, 5, "time_stop_5d")),
    ("time_stop_10d", lambda i, df, ep: rule_time_stop(i, df, ep, 10, "time_stop_10d")),
    ("trend_break", lambda i, df, ep: rule_trend_break(i, df, ep)),
    ("vol_spike_3x", lambda i, df, ep: rule_vol_spike(i, df, ep)),
]


def simulate_one(df, i, rule_fn):
    """模拟一次 entry→exit。"""
    ep = df["close"].iat[i]
    j, xp, reason = rule_fn(i, df, ep)
    ret = xp / ep - 1
    days = j - i
    # MFE / MAE in holding window (relative to entry)
    window = df.iloc[i:j + 1]
    mfe = window["high"].max() / ep - 1
    mae = window["low"].min() / ep - 1
    return {
        "entry_date": df["date"].iat[i], "exit_date": df["date"].iat[j],
        "entry_price": ep, "exit_price": xp, "return": ret,
        "days": days, "reason": reason,
        "mfe": mfe, "mae": mae,
        "fwd20": df["fwd20"].iat[i] if pd.notna(df["fwd20"].iat[i]) else None,
        "regime": df["regime"].iat[i],
    }


def main():
    regime_map = load_index()
    all_files = [f for f in glob.glob(os.path.join(CACHE_DIR, "*.json"))
                 if "index_" not in os.path.basename(f)]
    targets = set(target_codes())
    files = [f for f in all_files
             if os.path.splitext(os.path.basename(f))[0].split("_")[-1] in targets]
    print(f"目标池代码数: {len(targets)}，命中缓存文件: {len(files)} / {len(all_files)}")

    # 收集每个 entry 类型在不同退出规则下的结果
    # results[entry_type][rule_name] -> list of dict
    results = defaultdict(lambda: defaultdict(list))
    entry_counts = Counter()
    n_used = 0

    for k, path in enumerate(files):
        df = load_stock(path)
        if df is None:
            continue
        df = add_features(df)
        regime_s = df["date"].map(regime_map).fillna("unknown")
        df["regime"] = regime_s.values
        usable = df["ma60"].notna() & df["fwd20"].notna() & (regime_s != "unknown")
        if usable.sum() < 50:
            continue
        n_used += 1

        entries = generate_entries(df, regime_s)
        for entry_name, mask in entries.items():
            ev = events_of(mask)
            eidx = np.flatnonzero(ev.values)
            entry_counts[entry_name] += len(eidx)
            for i in eidx:
                if i + 1 >= len(df):
                    continue
                ep = df["close"].iat[i]
                # ATR1.5 stop 特判
                atr_val = df["atr14"].iat[i]
                if pd.notna(atr_val) and atr_val > 0:
                    sl_atr = -1.5 * atr_val / ep
                else:
                    sl_atr = -0.08
                local_rules = EXIT_RULES + [
                    ("stop_loss_ATR1.5", lambda i, df, ep, sl=sl_atr: rule_fixed_stop(i, df, ep, sl, "stop_loss_ATR1.5")),
                ]
                for rule_name, rule_fn in local_rules:
                    try:
                        rec = simulate_one(df, i, rule_fn)
                        results[entry_name][rule_name].append(rec)
                    except Exception as e:
                        print(f"err {entry_name} {rule_name} {df['date'].iat[i]}: {e}")

        if (k + 1) % 500 == 0:
            print(f"  进度 {k + 1}/{len(files)}，已用 {n_used}", flush=True)

    print(f"\n可用标的: {n_used}")
    print("entry 类型样本数:", dict(entry_counts))

    print("\n" + "=" * 80)
    print("退出规则对比（统一 entry 集合；ret=exit/entry-1，费/滑点未扣）")
    print("=" * 80)
    rows = []
    for entry_name, rules in results.items():
        for rule_name, recs in rules.items():
            if not recs:
                continue
            rets = np.array([r["return"] for r in recs])
            days = np.array([r["days"] for r in recs])
            mfes = np.array([r["mfe"] for r in recs])
            maes = np.array([r["mae"] for r in recs])
            wins = rets > 0
            gains = rets[rets > 0].sum() if wins.any() else 0
            losses = -rets[rets <= 0].sum() if (~wins).any() else 0
            pf = gains / losses if losses > 0 else math.inf
            rows.append({
                "entry": entry_name, "rule": rule_name, "n": len(rets),
                "win_rate": wins.mean(), "mean_ret": rets.mean(), "median_ret": np.median(rets),
                "profit_factor": pf,
                "avg_days": days.mean(),
                "max_mae": maes.min(),
                "p5_ret": np.percentile(rets, 5), "p10_ret": np.percentile(rets, 10),
                "p90_ret": np.percentile(rets, 90),
                "avg_mfe": mfes.mean(),
            })

    out = pd.DataFrame(rows).sort_values(["entry", "mean_ret"], ascending=[True, False])
    print(out.to_string(float_format=lambda x: f"{x:.3f}"))

    # 每个 entry 类型的最优规则
    print("\n" + "=" * 80)
    print("每个 entry 类型 mean_ret 前三")
    print("=" * 80)
    for entry_name in sorted(results.keys()):
        sub = out[out["entry"] == entry_name].sort_values("mean_ret", ascending=False).head(3)
        print(f"\n{entry_name}:")
        print(sub[["rule", "n", "win_rate", "mean_ret", "profit_factor", "avg_days", "max_mae"]]
              .to_string(float_format=lambda x: f"{x:.3f}"))

    # 综合推荐：以追强/抄底两个主要入口加权
    print("\n" + "=" * 80)
    print("综合推荐（entry_追强_up + entry_抄底_dn 合并后按 mean_ret 排序）")
    print("=" * 80)
    combined = []
    for rule_name in out["rule"].unique():
        vals = []
        for recs in [results["entry_追强_up"].get(rule_name, []),
                     results["entry_抄底_dn"].get(rule_name, [])]:
            vals.extend([r["return"] for r in recs])
        if vals:
            arr = np.array(vals)
            wins = arr > 0
            gains = arr[arr > 0].sum() if wins.any() else 0
            losses = -arr[arr <= 0].sum() if (~wins).any() else 0
            pf = gains / losses if losses > 0 else math.inf
            combined.append({
                "rule": rule_name, "n": len(arr), "win_rate": wins.mean(),
                "mean_ret": arr.mean(), "median_ret": np.median(arr),
                "profit_factor": pf,
                "p5": np.percentile(arr, 5), "p10": np.percentile(arr, 10),
                "max_loss": arr.min(),
            })
    comb_df = pd.DataFrame(combined).sort_values("mean_ret", ascending=False)
    print(comb_df.to_string(float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
