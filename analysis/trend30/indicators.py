# -*- coding: utf-8 -*-
"""30min 趋势判定 — 指标层（纯函数，无 I/O）。

复用 analysis/index_regime.py 的 Wilder 口径：_ir_adx / _ir_atr_wilder /
_ir_linreg_slope_r2 / _ir_er，保证与大盘 regime 判定同源。Supertrend(10,2) 为本文件新写。

⚠️ 关于「每天几根 K 线」：实测生产路径（1min 聚合）每天 10 根（含 11:30/15:00 两个
1 分钟残桩），原生长历史路径 9 根（无 13:00）。因此本层**不假定根数**：
  · collapse_stubs 按「残桩」特征（成交量远小于前一根）合并，而非按固定标签；
  · mark_bar_meta 的首/末/午休首根均由**位置 + 时段间隔**派生。
"""
import os
import sys

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from analysis.index_regime import (  # noqa: E402
    _ir_adx, _ir_atr_wilder, _ir_linreg_slope_r2, _ir_er,
)

_LUNCH_GAP_MIN = 60.0       # 相邻 bar 间隔 > 60min 视为午休割裂
_STUB_VOL_RATIO = 0.25      # 残桩判据：本根成交量 < 0.25 × 前一根即视为残桩并入前一根


def drop_forming_bar(df: pd.DataFrame, now=None) -> pd.DataFrame:
    """末根 30min bar 未收盘（now < bar_time + 30min）则丢弃，防未完成 bar 前视。
    bar_time 取该 bar 的时间戳（左沿）。"""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
    df = df.copy().sort_values("time").reset_index(drop=True)
    last_t = pd.Timestamp(df["time"].iloc[-1])
    if now < last_t + pd.Timedelta(minutes=30):
        df = df.iloc[:-1]
    return df.reset_index(drop=True)


def collapse_stubs(df: pd.DataFrame) -> pd.DataFrame:
    """把「残桩」bar 并入前一根（open 首 / close 末 / high-max / low-min / vol、amount 累加）。

    残桩判据是**数据驱动**的：本根成交量 < _STUB_VOL_RATIO × 前一根。这样对
    「1min 聚合（11:30/15:00 两根残桩）」与「原生 30min（无残桩）」两种布局都成立，
    不依赖任何标签假设。"""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    df = df.copy().sort_values("time").reset_index(drop=True)
    has_vol = "volume" in df.columns
    has_amt = "amount" in df.columns
    rows = []
    prev_vol = None
    for _, r in df.iterrows():
        d = dict(r)
        v = float(d.get("volume", 0) or 0) if has_vol else 0.0
        is_stub = (has_vol and rows and prev_vol and v < _STUB_VOL_RATIO * prev_vol)
        if is_stub:
            p = rows[-1]
            p["high"] = max(p["high"], d["high"])
            p["low"] = min(p["low"], d["low"])
            p["close"] = d["close"]
            if has_vol:
                p["volume"] = float(p.get("volume", 0) or 0) + v
            if has_amt:
                p["amount"] = float(p.get("amount", 0) or 0) + float(d.get("amount", 0) or 0)
        else:
            rows.append(d)
        prev_vol = v
    return pd.DataFrame(rows).reset_index(drop=True)


def mark_bar_meta(df: pd.DataFrame) -> pd.DataFrame:
    """追加 bar 元信息。全部按**时段 + 位置**派生，不假定每天固定根数：
      bar_idx_of_day / is_first / is_last / is_lunch_first(间隔>60min 的首根) /
      is_lunch_last / is_limit_locked(一字板) / weight(1.0 或 0.5) / tradable_cutoff(<14:30)"""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    df = df.copy().sort_values("time").reset_index(drop=True)
    t = pd.to_datetime(df["time"])
    day = t.dt.normalize()
    idx_in_day = df.groupby(day).cumcount()
    cnt_in_day = df.groupby(day)["time"].transform("size")
    df["bar_idx_of_day"] = (idx_in_day + 1).astype(int)
    df["is_first"] = (idx_in_day == 0).values
    df["is_last"] = (idx_in_day == (cnt_in_day - 1)).values
    gap_s = t.diff().dt.total_seconds().fillna(0.0)
    is_lunch_first = (gap_s > _LUNCH_GAP_MIN * 60.0) & (~df["is_first"])
    df["is_lunch_first"] = is_lunch_first.values
    df["is_lunch_last"] = is_lunch_first.shift(-1, fill_value=False).values
    # 一字板：整根无振幅（涨跌停封死）。板块涨跌幅差异不影响该判据。
    df["is_limit_locked"] = (df["high"] == df["low"]).values
    # 结构性降权：首根（吃隔夜/竞价跳空）、末根（尾盘做价）、午休首根 一律 0.5，不参与确认
    df["weight"] = np.where(
        df["is_first"] | df["is_last"] | df["is_lunch_first"], 0.5, 1.0).astype(float)
    df["tradable_cutoff"] = (t.dt.strftime("%H:%M") < "14:30").values
    return df


def _supertrend(df: pd.DataFrame, n: int = 10, mult: float = 2.0):
    """Supertrend(n, mult)。返回 (st_value, st_dir)，st_dir ∈ {+1 多头, -1 空头}。
    带方向递推：上轨只降不升、下轨只升不降，收盘破位翻转。"""
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    atr = _ir_atr_wilder(df, n).values
    m = len(df)
    st = np.full(m, np.nan)
    st_dir = np.ones(m, dtype=int)
    upper = np.full(m, np.nan)
    lower = np.full(m, np.nan)
    hl2 = (h + l) / 2.0
    fub = hl2 + mult * atr
    flb = hl2 - mult * atr
    for i in range(m):
        if np.isnan(atr[i]):
            continue
        if i == 0 or np.isnan(upper[i - 1]) or np.isnan(st[i - 1]):
            upper[i], lower[i] = fub[i], flb[i]
            st[i], st_dir[i] = upper[i], 1
            continue
        upper[i] = fub[i] if (fub[i] < upper[i - 1] or c[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = flb[i] if (flb[i] > lower[i - 1] or c[i - 1] < lower[i - 1]) else lower[i - 1]
        if st_dir[i - 1] == 1:
            if c[i] < lower[i]:
                st_dir[i], st[i] = -1, upper[i]
            else:
                st_dir[i], st[i] = 1, lower[i]
        else:
            if c[i] > upper[i]:
                st_dir[i], st[i] = 1, lower[i]
            else:
                st_dir[i], st[i] = -1, upper[i]
    return pd.Series(st, index=df.index), pd.Series(st_dir, index=df.index)


def add_30min_indicators(df: pd.DataFrame, n_adx: int = 14,
                         ema_fast: int = 20, ema_slow: int = 60,
                         st_n: int = 10, st_mult: float = 2.0) -> pd.DataFrame:
    """追加 ema20/ema60/ema_spread/adx/plus_di/minus_di/adx_rising/supertrend/st_dir/
    atr14/atr_ratio/er10。复用 _ir_adx / _ir_atr_wilder / _ir_er。"""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    df = df.copy().reset_index(drop=True)
    c = df["close"].astype(float)
    df["ema20"] = c.ewm(span=ema_fast, adjust=False).mean()
    df["ema60"] = c.ewm(span=ema_slow, adjust=False).mean()
    df["ema_spread"] = (df["ema20"] - df["ema60"]) / df["ema60"].replace(0.0, np.nan)
    adx, pdi, mdi = _ir_adx(df, n_adx)
    df["adx"], df["plus_di"], df["minus_di"] = adx, pdi, mdi
    df["adx_rising"] = (df["adx"] > df["adx"].shift(1)) & (df["adx"] > df["adx"].shift(2))
    atr14 = _ir_atr_wilder(df, 14)
    df["atr14"] = atr14
    df["atr_ratio"] = atr14 / atr14.rolling(20, min_periods=5).mean()
    st, st_dir = _supertrend(df, st_n, st_mult)
    df["supertrend"], df["st_dir"] = st, st_dir
    er, _ = _ir_er(c, 10, 5)
    df["er10"] = er
    return df


def linreg_quality(closes, n: int = 20) -> pd.DataFrame:
    """最近 n 根收盘价 OLS：norm_slope（slope/mean×100）、r2。复用 _ir_linreg_slope_r2。"""
    c = pd.Series(closes).astype(float)
    vals = c.values
    ns, r2s = [], []
    for i in range(len(vals)):
        if i + 1 < n:
            ns.append(np.nan)
            r2s.append(np.nan)
            continue
        w = vals[i + 1 - n:i + 1]
        b, r2 = _ir_linreg_slope_r2(w)
        mean = float(np.mean(w)) if len(w) else 0.0
        ns.append((b / mean * 100.0) if mean else np.nan)
        r2s.append(r2)
    return pd.DataFrame({"norm_slope": ns, "r2": r2s})
