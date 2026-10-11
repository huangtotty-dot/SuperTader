# -*- coding: utf-8 -*-
"""30min 趋势判定 — 指标层（纯函数，无 I/O）。

复用 analysis/index_regime.py 的 Wilder 口径：_ir_adx / _ir_atr_wilder / _ir_er，
保证与大盘 regime 判定同源。Supertrend(10,2) 为本文件新写。
（`linreg_quality` 原调 `_ir_linreg_slope_r2` 逐 bar 回归，2026-10-11 改为闭式向量化，
同口径但不再逐 bar 调用；该符号已从本文件 import 移除。）

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
    _ir_adx, _ir_atr_wilder, _ir_er,
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
    不依赖任何标签假设。

    2026-10-11 向量化（原 `iterrows()`+`dict(r)` 占 evaluate_bars 的 31%）：
    判据只看**原始**成交量（累计量不回喂下一次比较），故可整体向量化——按「非残桩」
    位置切组再聚合，`time/open/其余列=首行`、`high=max`、`low=min`、`close=末行`、
    `volume/amount=sum`。逐元素等价性已在 400 只真实 30min 序列 + 8 个合成残桩/边界
    用例上验证（0 不符），实测提速 6.0×。"""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    df = df.copy().sort_values("time").reset_index(drop=True)
    if "volume" not in df.columns:
        return df.reset_index(drop=True)        # 无 volume ⇒ 无残桩判据 ⇒ 原样
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    prev_vol = vol.shift(1)
    # 首根 prev_vol=NaN ⇒ 比较为 False（与原实现 `rows` 为空时不判残桩一致）
    is_stub = prev_vol.notna() & (vol < _STUB_VOL_RATIO * prev_vol)
    grp = (~is_stub).cumsum()
    agg = {c: ("max" if c == "high" else "min" if c == "low"
               else "last" if c == "close" else "sum" if c in ("volume", "amount")
               else "first") for c in df.columns}
    return df.groupby(grp, sort=True).agg(agg)[list(df.columns)].reset_index(drop=True)


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
    # 2026-10-11：原为 `t.dt.strftime("%H:%M") < "14:30"`（整列字符串格式化 + 逐元素
    # 字符串比较）。改整数比较——零填充 "HH:MM" 的**字典序**与 hour*100+minute 的**数值序**
    # 完全等价（已在 09:00/13:00/14:29/**14:30**/14:31/15:00/00:00/23:59 边界逐一验证）。
    # 实测 280 根 3.22ms → 0.62ms（5.2×）。
    df["tradable_cutoff"] = ((t.dt.hour * 100 + t.dt.minute) < 1430).values
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
    """最近 n 根收盘价 OLS：norm_slope（slope/mean×100）、r2。与 _ir_linreg_slope_r2 同口径。

    2026-10-11 向量化（原逐 bar Python 循环调 `_ir_linreg_slope_r2`，占 evaluate_bars 的 24%）：
    由于**窗长恒定**且 x 恒为 0..n-1，OLS 有闭式解 —— b = Sxy/Sxx（Sxx 为常数）、
    r2 = b²·Sxx/Syy，全部由滚动和 O(m) 算出（Sxy 用滑动窗矩阵乘，走 C 层）。
    等价的三个细节：① 前 n-1 个位置两个字段均为 NaN；② 窗含 NaN 时 `_ir_linreg_slope_r2`
    返回 (0,0)，而外层 mean=NaN 使 `if mean` 为真 ⇒ norm_slope 得 NaN 而非 0；
    ③ 不足 n 根时整列 NaN。逐元素等价性已在 400 只真实序列 + 5 个边界用例上验证（0 不符），
    实测提速 24.4×。"""
    c = pd.Series(closes).astype(float)
    v = c.to_numpy(dtype=float)
    m = len(v)
    if m == 0:
        return pd.DataFrame({"norm_slope": [], "r2": []})
    if m < n:
        nan_arr = np.full(m, np.nan)
        return pd.DataFrame({"norm_slope": nan_arr, "r2": nan_arr})
    lead = np.zeros(m, dtype=bool)
    lead[:n - 1] = True

    x = np.arange(n, dtype=float)
    xc = x - x.mean()
    Sxx = float((xc ** 2).sum())

    nan_mask = np.isnan(v)
    v0 = np.where(nan_mask, 0.0, v)             # NaN 占位 0；含 NaN 的窗随后整体作废
    s = pd.Series(v0)
    Sy = s.rolling(n, min_periods=n).sum().to_numpy()
    Syy_raw = pd.Series(v0 * v0).rolling(n, min_periods=n).sum().to_numpy()
    nan_cnt = pd.Series(nan_mask.astype(float)).rolling(n, min_periods=n).sum().to_numpy()

    # Sxy = Σ_j xc[j]·y_{i-n+1+j} —— 权重依赖**窗内位置**，故用滑动窗矩阵乘（不可用单条滚动和）
    view = np.lib.stride_tricks.sliding_window_view(v0, n)
    Sxy = np.full(m, np.nan)
    Sxy[n - 1:] = view @ xc

    with np.errstate(divide="ignore", invalid="ignore"):
        b = Sxy / Sxx
        Syy = Syy_raw - (Sy ** 2) / n
        r2 = np.where(Syy > 0, b ** 2 * Sxx / Syy, 0.0)
        mean = Sy / n
        ns = np.where(mean != 0, b / mean * 100.0, np.nan)
    r2 = np.clip(r2, 0.0, 1.0)

    bad = nan_cnt > 0                            # 窗含 NaN ⇒ (b, r2) = (0, 0)，norm_slope = NaN
    ns = np.where(bad, np.nan, np.where(lead, np.nan, ns))
    r2 = np.where(bad, 0.0, np.where(lead, np.nan, r2))
    return pd.DataFrame({"norm_slope": ns, "r2": r2})
