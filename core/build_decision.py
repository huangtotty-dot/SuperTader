# -*- coding: utf-8 -*-
"""
core/build_decision.py — 建仓判定决策核（P3 建仓加仓同源：双侧单一真源）

方案A（2026-08-15 owner 拍板）建仓判定 = 时机门控 go（regime 条件化）+ 否决因子 + W35 日内确认。
本模块是该判定的**纯函数决策核**：无 IO、无 superTrader/goldminer 环境依赖，仅用标准库 + numpy/pandas。

消费方：
  · superTrader：core/timing_gate.py（数据获取后委托本模块决策）、core/position_builder.py
    （verdict 映射 + W35 日内确认委托本模块）
  · goldminer：signals/build_gate.py 经 SUPERTRADER_ROOT 以 importlib 绝对路径加载本文件
    （与 config/auto_pool.py 同一跨仓消费模式）

纪律：
  · 本模块函数是双侧 verdict 一致性的唯一来源——改动必须双侧同步验证（scripts/build_verdict_parity.py）。
  · 禁止在本模块加 IO/网络/文件读写；数据获取留在两侧各自的 adapter。
  · 量能只用比值（vol_ratio20），股/手单位无关。
"""
import numpy as np
import pandas as pd

# 与 config.ENTRY_TIMING_PARAMS 同值（goldminer 侧无法 import superTrader config.py——其顶层
# import requests 等重依赖；以此为本模块内嵌兜底）。同步由单测守卫：
# t_io/validation/build_decision/test_build_decision.py::test_default_params_match_config
DEFAULT_TIMING_PARAMS = {
    "enabled": True,
    "regime_ma60": True,
    "regime_up_buffer": 1.005,
    "trend_up_drawdown_min": -0.03,
    "trend_dn_drawdown_max": -0.10,
    "trend_dn_rsi_max": 20.0,
    "intraday_confirm_gate": True,
    "intraday_confirm_vol_min": 1.2,
    "veto_vol_spike": 3.0,
    "veto_dist_ma60_max": 0.20,
}


# ═══════════════════════════════════════════
# 市场状态（指数 vs MA60，无未来函数）
# ═══════════════════════════════════════════

def regime_from_index_daily(df: pd.DataFrame, date_str: str, params: dict = None) -> dict:
    """指数日线 → 市场状态。df 需含 date/close 列；按 <=date_str 截断（调用方保证无未来）。
    返回 {regime, close, ma60, ratio}；数据 <61 行 → regime=unknown。"""
    p = params or DEFAULT_TIMING_PARAMS
    idx = df[df["date"].astype(str) <= str(date_str)]
    if len(idx) < 61:
        return {"regime": "unknown", "close": None, "ma60": None}
    close = float(idx["close"].iloc[-1])
    ma60 = float(idx["close"].astype(float).rolling(60).mean().iloc[-1])
    up_buffer = float(p.get("regime_up_buffer", 1.005))
    if close > ma60 * up_buffer:
        regime = "trend_up"
    elif close < ma60 * 0.97:
        regime = "trend_dn"
    else:
        regime = "range"
    return {"regime": regime, "close": close, "ma60": round(ma60, 3),
            "ratio": round(close / ma60, 4)}


# ═══════════════════════════════════════════
# 个股日线时机特征（截止 date_str，无未来）
# ═══════════════════════════════════════════

def features_from_daily(df: pd.DataFrame, date_str: str) -> dict:
    """个股日线 → 时机特征。df 需含 date/open/high/low/close/volume 列；<61 行 → {}（数据不足）。"""
    if df is None or df.empty:
        return {}
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    sub = df[df["date"] <= str(date_str)]
    if len(sub) < 61:
        return {}
    c = sub["close"].astype(float)
    h = sub["high"].astype(float)
    price = float(c.iloc[-1])
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma60 = float(c.rolling(60).mean().iloc[-1])
    rec_high = float(h.tail(20).max())
    # MACD 金叉（近5日）
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False).mean()
    golden = bool(((dif > dea) & (dif.shift(1) <= dea.shift(1))).tail(5).any())
    # RSI(14)（空头抄底超卖极值用）
    _delta = c.diff()
    _gain = _delta.clip(lower=0).rolling(14).mean()
    _loss = (-_delta.clip(upper=0)).rolling(14).mean()
    _rsi = float((100 - 100 / (1 + _gain / _loss.replace(0, float("nan")))).iloc[-1]) if _loss.iloc[-1] and _loss.iloc[-1] > 0 else 50.0
    # 2026-08-27 因子挖掘（127万股票-日）：两个否决因子的特征
    _vol_ratio20 = None
    if "volume" in sub.columns:
        _v = pd.to_numeric(sub["volume"], errors="coerce")
        _v20 = float(_v.rolling(20).mean().iloc[-1]) if len(_v) >= 20 else float("nan")
        if _v20 and _v20 > 0:
            _vol_ratio20 = round(float(_v.iloc[-1]) / _v20, 2)
    return {
        "price": round(price, 3),
        "trend_multihead": bool(price > ma20 and price > ma60),
        "above_ma60": bool(price > ma60),
        "drawdown": round(price / rec_high - 1, 4) if rec_high > 0 else 0.0,
        "macd_golden_5d": golden,
        "rsi": round(_rsi, 1),
        "ma20": round(ma20, 3), "ma60": round(ma60, 3),
        "vol_ratio20": _vol_ratio20,
        "dist_ma60": round(price / ma60 - 1, 4),
    }


def dd_threshold_ok(drawdown: float, regime: str) -> bool:
    """回撤到位判定（B-4: range 市观察态回撤用多头口径 ≥-3%）。"""
    if regime == "trend_up":
        return drawdown >= -0.03
    if regime == "trend_dn":
        return drawdown < -0.10
    return drawdown >= -0.03


# ═══════════════════════════════════════════
# 时机决策（regime 条件化 go + 否决因子）
# ═══════════════════════════════════════════

def timing_decision(features: dict, regime: str, params: dict = None) -> dict:
    """时机门控决策。返回 {go, veto, reasons}。features={}（数据不足）由调用方先行拦截。"""
    p = params or DEFAULT_TIMING_PARAMS
    f = features
    reasons = []
    vetoes = []
    if regime == "trend_up":
        # 多头趋势 → 追强；两个硬否决（仅追强侧；抄底侧爆量是恐慌出清常态，不否决）
        _vol_max = float(p.get("veto_vol_spike", 3.0))
        _dist_max = float(p.get("veto_dist_ma60_max", 0.20))
        _vr = f.get("vol_ratio20")
        if _vr is not None and _vr >= _vol_max:
            vetoes.append(f"爆量{_vr:g}倍≥{_vol_max:g}")
        _dm = f.get("dist_ma60")
        if _dm is not None and _dm > _dist_max:
            vetoes.append(f"偏离MA60{_dm:+.1%}>{_dist_max:+.0%}")
        cond = f["trend_multihead"] and f["drawdown"] >= -0.03 and not vetoes
        reasons.append(f"多头趋势: 追强(多头{'✓' if f['trend_multihead'] else '✗'}+浅回撤{'✓' if f['drawdown']>=-0.03 else '✗'})")
        if vetoes:
            reasons.append(f"否决: {'、'.join(vetoes)}")
        if f["macd_golden_5d"]:
            reasons.append("MACD金叉近5日 ✓（加分）")
        go = cond
    elif regime == "trend_dn":
        # 空头趋势 → 抄底超跌极值（深回撤 + RSI 深度超卖）
        _rsi_lim = float(p.get("trend_dn_rsi_max", 20))
        _dd_ok = f["drawdown"] < -0.10
        _rsi_ok = (f.get("rsi") or 50) < _rsi_lim
        cond = _dd_ok and _rsi_ok
        reasons.append(f"空头趋势: 抄底(深回撤{'✓' if _dd_ok else '✗'} + RSI极值{'✓' if _rsi_ok else '✗'} "
                       f"rsi={f.get('rsi')} drawdown={f['drawdown']:.1%})")
        go = cond
    else:
        # 震荡/unknown → 降频
        go = False
        reasons.append("震荡市: 降频，暂不建仓/加仓")
    return {"go": bool(go), "veto": vetoes, "reasons": reasons}


# ═══════════════════════════════════════════
# verdict 映射（scan_stock 生产口径）
# ═══════════════════════════════════════════

def verdict_from_timing(go: bool, regime: str, features: dict, data_insufficient: bool = False) -> tuple:
    """go/regime/features → (verdict, score)。
    go→signal；range+多头结构+浅回撤→watch_signal（只留痕不推送）；有方向且结构/回撤过一→approaching；否则 weak。
    data_insufficient（features={}）时结构/回撤/金叉一律不通过（数据失败不得伪装成条件通过）。"""
    f = features or {}
    _dir_ok = regime in ("trend_up", "trend_dn")
    _trend = bool(f.get("trend_multihead"))
    if data_insufficient:
        _dd_ok = False
    else:
        _dd_ok = dd_threshold_ok(float(f["drawdown"]), regime) if "drawdown" in f else False
    _golden = bool(f.get("macd_golden_5d"))
    _score = (30 if _dir_ok else 0) + (30 if _trend else 0) + (30 if _dd_ok else 0) + (10 if _golden else 0)
    if go:
        _v = "signal"
    elif regime == "range" and _trend and _dd_ok:
        _v = "watch_signal"
    elif _dir_ok and (_trend or _dd_ok):
        _v = "approaching"
    else:
        _v = "weak"
    return _v, _score


# ═══════════════════════════════════════════
# W35 日内右侧确认（15m 站上EMA8 + 放量 + 站上VWAP）
# ═══════════════════════════════════════════

def _resample_15min(df: pd.DataFrame) -> pd.DataFrame:
    """1分钟 → 15分钟聚合（与 analysis/indicators.resample_to_15min 同口径）。"""
    if df.empty or len(df) < 15:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_15m"] = df["time"].dt.floor("15min")
    agg = df.groupby("time_15m").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum", "amount": "sum",
    }).reset_index()
    return agg.rename(columns={"time_15m": "time"})


def _add_15min_confirm_cols(df15: pd.DataFrame) -> pd.DataFrame:
    """W35 确认所需的两列（与 analysis/indicators.add_15min_indicators 中同名列同口径）。"""
    if df15.empty or len(df15) < 3:
        return df15
    c = df15["close"]
    df15["ema_fast_15m"] = c.ewm(span=8, adjust=False).mean()
    df15["vol_ma4_15m"] = df15["volume"].rolling(4, min_periods=1).mean()
    df15["vol_ratio_15m"] = df15["volume"] / df15["vol_ma4_15m"].replace(0, np.nan)
    return df15


def intraday_confirm(df_1min, vol_min: float = 1.2) -> tuple:
    """当日盘中右侧买点确认。返回 (passed, detail, insufficient)。

    无未来函数：只用截止最新一根【已收盘】15m bar 的数据；未收盘的当前根不参与。
    df_1min 为当日 1 分钟线（time/open/high/low/close/volume/amount）。数据不足时 insufficient=True。
    """
    if df_1min is None or df_1min.empty or len(df_1min) < 20:
        return False, "日内分钟数据不足", True
    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    # 当日累计 VWAP（Σamount/Σvol；缺 amount 用 close*vol 代理）
    if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
        cum_amt = d["amount"].fillna(0).cumsum()
    else:
        cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
    cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
    d["vwap_cum"] = cum_amt / cum_vol

    df15 = _add_15min_confirm_cols(_resample_15min(d))
    if df15 is None or df15.empty:
        return False, "15分钟数据不足", True
    df15 = df15.copy()
    df15["time"] = pd.to_datetime(df15["time"], errors="coerce")
    last_min_ts = d["time"].iloc[-1]
    # 取最新一根【已收盘】15m bar（收盘时刻 <= 当日最新分钟+1min）
    closed = df15[(df15["time"] + pd.Timedelta(minutes=15)) <= (last_min_ts + pd.Timedelta(minutes=1))]
    if closed.empty:
        return False, "尚无已收盘15分钟bar", True
    bar = closed.iloc[-1]
    c = bar.get("close")
    ema8 = bar.get("ema_fast_15m")
    volr = bar.get("vol_ratio_15m")
    if any(pd.isna(x) for x in (c, ema8, volr)):
        return False, "15分钟指标NaN", True
    close_ts = bar["time"] + pd.Timedelta(minutes=15)
    vw_rows = d[d["time"] <= close_ts]
    vwap = float(vw_rows["vwap_cum"].iloc[-1]) if (not vw_rows.empty and pd.notna(vw_rows["vwap_cum"].iloc[-1])) else None
    ema_ok = float(c) > float(ema8)
    vol_ok = float(volr) > vol_min
    vwap_ok = (vwap is None) or (float(c) >= vwap)
    passed = ema_ok and vol_ok and vwap_ok
    detail = (f"15分钟确认: 站上EMA8={ema_ok}(c={float(c):.3f}/ema8={float(ema8):.3f}) "
              f"放量={vol_ok}(量比{float(volr):.2f}>{vol_min}) 站上VWAP={vwap_ok}"
              f"{f'(vwap={vwap:.3f})' if vwap is not None else ''}")
    return passed, detail, False
