# -*- coding: utf-8 -*-
"""
indicators.py — V3.0 指标计算模块
从 data_fetcher.py 抽取，新增标准 5分钟 MACD(12,26,9) / BOLL(20,2) / RSI(14)

三层信号架构：
  第一层 趋势层：5分钟 MACD+BOLL → 定方向（trend_regime.py 消费）
  第二层 择时层：5分钟 RSI → 在趋势方向上找买卖点
  第三层 执行层：1分钟 VWAP/ATR/形态（保留，signal_engine.py 消费）
"""
import numpy as np
import pandas as pd
from typing import Optional

try:
    from config import PARAMS
except ImportError:
    PARAMS = {}


# ============================================================
# 1分钟线指标（原有逻辑，从 data_fetcher.py 重构）
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为1分钟K线计算技术指标：RSI/BOLL/MACD/EMA/VWAP/ATR/影线"""
    if df.empty or len(df) < 2:
        return df
    c = df["close"]

    # RSI(period from PARAMS, default 6)
    rsi_period = PARAMS.get("rsi_period", 6)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(rsi_period, min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(rsi_period, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    # V1.1.2 修复（bug fix，非调优，C 语义）：0/0 钉平窗（gain==0 & loss==0）除零产生 NaN
    # → 填 50 中性；纯上涨窗（loss==0 & gain>0）保持 NaN 与现网一致；预热 leading NaN 不变
    df["rsi"] = (100 - 100 / (1 + rs)).mask((gain == 0) & (loss == 0), 50.0)

    # BOLL(20, 2.0)
    bb_period = PARAMS.get("bb_period", 20)
    bb_std = PARAMS.get("bb_std", 2.0)
    ma = c.rolling(bb_period, min_periods=1).mean()
    sd = c.rolling(bb_period, min_periods=1).std()
    df["bb_up"] = ma + bb_std * sd
    df["bb_dn"] = ma - bb_std * sd
    band_width = (df["bb_up"] - df["bb_dn"]).replace(0, np.nan)
    df["bb_pct"] = (c - df["bb_dn"]) / band_width

    # MACD(12, 26, 9)
    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = exp1 - exp2
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd"] - df["macd_signal"]) * 2

    # EMA(8, 21)
    ema_fast = PARAMS.get("ema_fast_period", 8)
    ema_slow = PARAMS.get("ema_slow_period", 21)
    df["ema_fast"] = c.ewm(span=ema_fast, adjust=False).mean()
    df["ema_slow"] = c.ewm(span=ema_slow, adjust=False).mean()
    df["ema_spread"] = (df["ema_fast"] - df["ema_slow"]) / df["ema_slow"].replace(0, np.nan)

    # VWAP
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = tp * df["volume"]
    time_text = df["time"].astype(str).str.strip()
    parsed_time = pd.to_datetime(time_text, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    if parsed_time.isna().all():
        parsed_hms = pd.to_datetime(time_text, format="%H:%M:%S", errors="coerce")
        if parsed_hms.notna().all():
            parsed_time = pd.Timestamp.now().normalize() + (parsed_hms - parsed_hms.dt.normalize())
        else:
            parsed_hm = pd.to_datetime(time_text, format="%H:%M", errors="coerce")
            if parsed_hm.notna().all():
                parsed_time = pd.Timestamp.now().normalize() + (parsed_hm - parsed_hm.dt.normalize())
    df["date"] = parsed_time.dt.date

    if "amount" in df.columns and df["amount"].notna().sum() > 0:
        df["vwap"] = df.groupby("date")["amount"].cumsum() / (df.groupby("date")["volume"].cumsum() * 100.0)
    else:
        df["vwap"] = df.groupby("date")["tp_vol"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["vwap"].ffill().fillna(df["close"])
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"].replace(0, np.nan)

    # ATR(14) 归一化 VWAP 偏离
    _atr_14 = df["high"].sub(df["low"]).abs().rolling(14, min_periods=1).mean()
    df["vwap_dev_atr"] = df["vwap_dev"] / (_atr_14 / df["close"]).replace(0, np.nan)

    # 日内位置
    day_high = df.groupby("date")["high"].transform("max")
    day_low = df.groupby("date")["low"].transform("min")
    df["day_amplitude"] = (day_high - day_low) / day_low.replace(0, np.nan)
    df["range_pos"] = (c - day_low) / (day_high - day_low + 1e-9)

    last_date = df["date"].iloc[-1]
    prev_data = df[df["date"] < last_date]
    df["prev_high"] = prev_data["high"].max() if not prev_data.empty else df["high"].rolling(120).max()

    # 量能
    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    df["mom5"] = c.pct_change(5)

    # 影线
    k_length = df["high"] - df["low"] + 1e-5
    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / k_length
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / k_length

    return df


# ============================================================
# 5分钟线聚合与指标（V3.0 核心扩展）
# ============================================================

def resample_to_5min(df: pd.DataFrame) -> pd.DataFrame:
    """将1分钟K线聚合为5分钟K线"""
    if df.empty or len(df) < 5:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_5m"] = df["time"].dt.floor("5min")
    agg = df.groupby("time_5m").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    }).reset_index()
    agg = agg.rename(columns={"time_5m": "time"})
    return agg


def add_5min_indicators(df_5min: pd.DataFrame) -> pd.DataFrame:
    """为5分钟K线计算完整技术指标 — V3.0 核心扩展

    新增（V3.0）：
      - MACD(12,26,9)：dif_5m / dea_5m / macd_hist_5m（标准参数，趋势层核心输入）
      - BOLL(20,2)：bb_mid_5m / bb_up_5m / bb_dn_5m / bb_width_5m / bb_pct_5m
      - RSI(14)：rsi_5m（择时层核心输入）

    保留（兼容）：
      - MACD(6,13,5) → macd_hist_5m_fast（旧 V1.17 因子，deprecated）
      - mom2_5m / vol_ratio_5m / low_rising_5m / stop_falling_5m
    """
    if df_5min.empty or len(df_5min) < 3:
        return df_5min

    c = df_5min["close"]
    h = df_5min["high"]
    l = df_5min["low"]
    v = df_5min["volume"]

    # ── 动量 ──
    df_5min["mom2_5m"] = c.pct_change(2)

    # ── 量比（相对前4根均值，~20分钟）──
    df_5min["vol_ma4_5m"] = v.rolling(4, min_periods=1).mean()
    df_5min["vol_ratio_5m"] = v / df_5min["vol_ma4_5m"].replace(0, np.nan)

    # ── V3.0: 标准 MACD(12, 26, 9) — 趋势层核心 ──
    exp1_std = c.ewm(span=12, adjust=False).mean()
    exp2_std = c.ewm(span=26, adjust=False).mean()
    df_5min["dif_5m"] = exp1_std - exp2_std                # DIF 快线
    df_5min["dea_5m"] = df_5min["dif_5m"].ewm(span=9, adjust=False).mean()  # DEA 慢线
    df_5min["macd_hist_5m"] = (df_5min["dif_5m"] - df_5min["dea_5m"]) * 2  # 柱状体

    # ── [deprecated] 旧 MACD(6, 13, 5) — 兼容 V1.17 量能反转因子 ──
    exp1_fast = c.ewm(span=6, adjust=False).mean()
    exp2_fast = c.ewm(span=13, adjust=False).mean()
    macd_fast = exp1_fast - exp2_fast
    macd_signal_fast = macd_fast.ewm(span=5, adjust=False).mean()
    df_5min["macd_hist_5m_fast"] = (macd_fast - macd_signal_fast) * 2

    # ── V3.0: BOLL(20, 2) — 趋势辅助确认 ──
    bb_period = PARAMS.get("bb_period", 20)
    bb_std = PARAMS.get("bb_std", 2.0)
    df_5min["bb_mid_5m"] = c.rolling(bb_period, min_periods=1).mean()
    bb_sd = c.rolling(bb_period, min_periods=1).std()
    df_5min["bb_up_5m"] = df_5min["bb_mid_5m"] + bb_std * bb_sd
    df_5min["bb_dn_5m"] = df_5min["bb_mid_5m"] - bb_std * bb_sd
    bb_width = (df_5min["bb_up_5m"] - df_5min["bb_dn_5m"]).replace(0, np.nan)
    df_5min["bb_width_5m"] = bb_width
    df_5min["bb_pct_5m"] = (c - df_5min["bb_dn_5m"]) / bb_width  # %b (0=下轨, 1=上轨)

    # ── V3.0: RSI(14) — 择时层核心 ──
    rsi_period = PARAMS.get("rsi_period_5m", 14)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(rsi_period, min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(rsi_period, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    # V1.1.2 修复（bug fix，非调优，C 语义）：0/0 钉平窗填 50 中性；
    # 纯上涨窗保持 NaN 与现网一致；预热 leading NaN 不变
    df_5min["rsi_5m"] = (100 - 100 / (1 + rs)).mask((gain == 0) & (loss == 0), 50.0)

    # ── 企稳信号（V1.26 遗留，保留）──
    df_5min["low_5m"] = l
    df_5min["low_rising_5m"] = l > l.shift(1)
    df_5min["stop_falling_5m"] = (c >= df_5min["open"]) | (c > c.shift(1))

    return df_5min


# ============================================================
# 15分钟线聚合与指标
# ============================================================

def resample_to_15min(df: pd.DataFrame) -> pd.DataFrame:
    """将1分钟K线聚合为15分钟K线"""
    if df.empty or len(df) < 15:
        return pd.DataFrame()

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_15m"] = df["time"].dt.floor("15min")

    agg = df.groupby("time_15m").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    }).reset_index()
    agg = agg.rename(columns={"time_15m": "time"})
    return agg


def add_15min_indicators(df_15min: pd.DataFrame) -> pd.DataFrame:
    """为15分钟K线计算技术指标：MACD、RSI、EMA、成交量比"""
    if df_15min.empty or len(df_15min) < 3:
        return df_15min

    c = df_15min["close"]

    # RSI(6)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(6, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(6, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    # V1.1.2 修复（bug fix，非调优，C 语义）：0/0 钉平窗填 50 中性；
    # 纯上涨窗保持 NaN 与现网一致；预热 leading NaN 不变
    df_15min["rsi_15m"] = (100 - 100 / (1 + rs)).mask((gain == 0) & (loss == 0), 50.0)

    # MACD(12, 26, 9)
    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df_15min["macd_15m"] = exp1 - exp2
    df_15min["macd_signal_15m"] = df_15min["macd_15m"].ewm(span=9, adjust=False).mean()
    df_15min["macd_hist_15m"] = (df_15min["macd_15m"] - df_15min["macd_signal_15m"]) * 2

    # EMA(8, 21)
    df_15min["ema_fast_15m"] = c.ewm(span=8, adjust=False).mean()
    df_15min["ema_slow_15m"] = c.ewm(span=21, adjust=False).mean()
    df_15min["ema_spread_15m"] = (df_15min["ema_fast_15m"] - df_15min["ema_slow_15m"]) / df_15min["ema_slow_15m"].replace(0, np.nan)

    # 量比（4根=1小时）
    df_15min["vol_ma4_15m"] = df_15min["volume"].rolling(4, min_periods=1).mean()
    df_15min["vol_ratio_15m"] = df_15min["volume"] / df_15min["vol_ma4_15m"].replace(0, np.nan)

    # 2周期动量（30分钟跨度）
    df_15min["mom2_15m"] = c.pct_change(2)

    return df_15min
