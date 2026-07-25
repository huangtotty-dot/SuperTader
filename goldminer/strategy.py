# coding=utf-8
"""掘金量化策略 — VWAP深V低吸 T+0 做T系统 (V1.0)

架构：本文件是策略主体，被掘金 main.py 入口文件 import。
核心引擎 (signal_engine / config / position_sizer) 复用 E:\06_T 下的模块。

使用方式：
  1. 在掘金项目中创建 main.py，内容为：
     import sys; sys.path.insert(0, r'E:\06_T')
     from goldminer.strategy import *
  2. 或者直接将本文件复制到掘金项目目录

依赖：
  - E:\06_T\signal_engine.py
  - E:\06_T\config.py
  - E:\06_T\position_sizer.py
"""

from __future__ import print_function, absolute_import, division
from gm.api import *
from datetime import datetime, timedelta, time as dtime
import numpy as np
import pandas as pd
import os
import sys

# ── 确保能 import E:\06_T 下的模块 ──
_06T_DIR = r"E:\06_T"
if _06T_DIR not in sys.path:
    sys.path.insert(0, _06T_DIR)

import signal_engine as _se
from config import STOCK_PARAMS, MORNING_ALERT_PARAMS, ETF_T0_PARAMS
from position_sizer import PositionSizer

# ── 常量 ────────────────────────────────────────────────
STOCKS = {
    "000988": "SZSE.000988",
    "600481": "SHSE.600481",
    "600176": "SHSE.600176",
    "603667": "SHSE.603667",
    "588170": "SHSE.588170",
}
STOCK_NAMES = {
    "000988": "华工科技", "600481": "双良节能",
    "600176": "中国巨石", "603667": "五洲新春",
    "588170": "科创芯片ETF",
}
REVERSE_MAP = {v: k for k, v in STOCKS.items()}

COMMISSION = 0.00015
STAMP_TAX = 0.0005
MAX_BUYS = 3
MAX_SELLS = 3
MIN_BARS = 25


def _raw_code(symbol):
    return symbol.replace("SHSE.", "").replace("SZSE.", "")


def _build_bar_df(context, code, gm_symbol):
    """从累积 bar 构建带指标的 DataFrame"""
    rows = context.bar_cache.get(gm_symbol, [])
    if len(rows) < MIN_BARS:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "amount"])
    df = df.sort_values("time").reset_index(drop=True)
    c = df["close"]

    # RSI
    d = c.diff(); g = d.clip(lower=0).rolling(14, min_periods=1).mean()
    l = (-d).clip(upper=0).rolling(14, min_periods=1).mean()
    df["rsi"] = 100 - 100 / (1 + g / l.replace(0, np.nan))

    # Bollinger
    ma = c.rolling(20, min_periods=1).mean(); sd = c.rolling(20, min_periods=1).std()
    df["bb_up"] = ma + 2 * sd; df["bb_dn"] = ma - 2 * sd
    df["bb_pct"] = (c - df["bb_dn"]) / (df["bb_up"] - df["bb_dn"]).replace(0, np.nan)

    # MACD
    e1 = c.ewm(span=12, adjust=False).mean(); e2 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = e1 - e2; df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd"] - df["macd_signal"]) * 2

    # EMA
    df["ema_fast"] = c.ewm(span=3, adjust=False).mean()
    df["ema_slow"] = c.ewm(span=6, adjust=False).mean()
    df["ema_spread"] = (df["ema_fast"] - df["ema_slow"]) / df["ema_slow"].replace(0, np.nan)

    # VWAP
    if df["amount"].notna().sum() > 0 and df["volume"].sum() > 0:
        df["vwap"] = df["amount"].cumsum() / df["volume"].cumsum().replace(0, np.nan)
    else:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)
    df["vwap"] = df["vwap"].ffill().fillna(c)

    df["date"] = pd.to_datetime(df["time"]).dt.date
    dh = df.groupby("date")["high"].transform("max"); dl = df.groupby("date")["low"].transform("min")
    df["day_amplitude"] = (dh - dl) / dl.replace(0, np.nan)
    df["range_pos"] = (c - dl) / (dh - dl + 1e-9)
    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    df["mom5"] = c.pct_change(5)
    k = df["high"] - df["low"] + 1e-5
    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / k
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / k
    df["prev_close"] = c.shift(1); df["prev_high"] = df["high"].shift(1)
    df["today_ret"] = (c - df["prev_close"]) / df["prev_close"].replace(0, np.nan)
    df["today_open"] = df.groupby("date")["open"].transform("first")
    return df


# ==================== 策略生命周期 ====================

def init(context):
    """策略初始化"""
    context.bar_cache = {}
    context.engine = _se.SignalEngine()

    for code in STOCKS:
        sp = STOCK_PARAMS.get(code, {})
        if sp: _se.PARAMS.update(sp)
        _se.MINUTE_FETCH_STATUS[code] = "ok"

    context.sizer = PositionSizer(params=_se.PARAMS, virtual_trades=_se.VIRTUAL_TRADES)
    context.daily_buy_count = {}; context.daily_sell_count = {}
    context.last_positions = {}

    symbols = list(STOCKS.values())
    subscribe(symbols=symbols, frequency="60s", count=240, wait_group=False)
    schedule(schedule_func=daily_summary, date_rule="1d", time_rule="14:59:00")
    schedule(schedule_func=reset_daily, date_rule="1d", time_rule="09:25:00")
    print(f"[GM] init done: {len(symbols)} stocks subscribed")


def reset_daily(context):
    _se.VIRTUAL_TRADES.clear()
    context.daily_buy_count = {}; context.daily_sell_count = {}


def on_bar(context, bars):
    now = context.now if hasattr(context, "now") else datetime.now()
    t = now.time()
    if t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
        return

    for bar in bars:
        gm_sym = bar.symbol; code = _raw_code(gm_sym)
        if code not in STOCKS: continue

        row = {"time": bar.bob, "open": bar.open, "high": bar.high, "low": bar.low,
               "close": bar.close, "volume": bar.volume,
               "amount": bar.amount if hasattr(bar, "amount") and bar.amount else 0}
        context.bar_cache.setdefault(gm_sym, []).append(row)
        if len(context.bar_cache[gm_sym]) > 300:
            context.bar_cache[gm_sym] = context.bar_cache[gm_sym][-300:]

        df = _build_bar_df(context, code, gm_sym)
        if df.empty: continue

        stock_params = STOCK_PARAMS.get(code, {})
        _se.PARAMS.update(stock_params)

        h = _get_holding(context, code, gm_sym)
        if h.get("qty", 0) <= 0: continue

        daily_ctx = {
            "daily_status": "ok", "daily_buy_t_ok": True,
            "daily_ma5": float(df["close"].tail(2).mean()),
            "daily_ma5_state": "above_ma5_trend", "daily_above_ma5": True,
            "daily_breakdown_risk": False, "daily_overheated": False,
            "index_regime": "range", "intraday_alerts": [],
        }

        try:
            bs, ss, sig = context.engine.evaluate(code, STOCK_NAMES.get(code, code), df, h, daily_ctx)
        except Exception:
            continue
        if sig is None: continue

        cp = bar.close
        if sig.action in ("BUY_LOW", "ADD_POS"):
            nth = stock_params.get("notify_buy_threshold", 68)
        elif t >= dtime(10, 0):
            nth = stock_params.get("notify_sell_threshold", 65)
        else:
            nth = stock_params.get("notify_sell_early_threshold", 75)
        if sig.score < nth: continue

        _h = {"name": STOCK_NAMES.get(code, code), "qty": int(h.get("qty", 0)),
              "t_qty": int(h.get("t_qty", h.get("qty", 0))),
              "type": "etf" if code.startswith("5") else "stock",
              "cost": float(h.get("cost", cp))}

        if sig.action in ("BUY_LOW", "ADD_POS"):
            bc = context.daily_buy_count.get(code, 0)
            if bc >= MAX_BUYS: continue
            bq = context.sizer.calc_buy_qty(code, _h, None, sig.score, 42.0) or 200
            bq = max(100, (bq // 100) * 100)
            try:
                order_volume(symbol=gm_sym, volume=bq, side=OrderSide_Buy,
                             order_type=OrderType_Market, position_effect=PositionEffect_Open)
                context.daily_buy_count[code] = bc + 1
                context.engine.record_signal(code, sig.action, cp, sig.score)
                context.engine.record_trade_action(code, "BUY_LOW", bq)
                print(f"[{now:%H:%M:%S}] BUY {code} {bq}@{cp:.2f} s={sig.score:.0f}")
            except Exception as e:
                print(f"[{code}] BUY err: {e}")

        elif sig.action == "SELL_HIGH":
            sc = context.daily_sell_count.get(code, 0)
            if sc >= MAX_SELLS: continue
            sq = context.sizer.calc_sell_qty(code, _h, None, sig.score, 42.0, sc) or 200
            sq = max(100, (sq // 100) * 100); sq = min(sq, int(_h["qty"]))
            if sq >= 100:
                try:
                    order_volume(symbol=gm_sym, volume=sq, side=OrderSide_Sell,
                                 order_type=OrderType_Market, position_effect=PositionEffect_Close)
                    context.daily_sell_count[code] = sc + 1
                    context.engine.record_signal(code, sig.action, cp, sig.score)
                    context.engine.record_trade_action(code, "SELL_HIGH", sq)
                    print(f"[{now:%H:%M:%S}] SELL {code} {sq}@{cp:.2f} s={sig.score:.0f}")
                except Exception as e:
                    print(f"[{code}] SELL err: {e}")


def daily_summary(context):
    today = datetime.now().strftime("%Y-%m-%d")
    tv, tt0 = 0.0, 0.0
    for code in STOCKS:
        h = _get_holding(context, code, STOCKS[code])
        q, p = h.get("qty", 0), h.get("price", 0)
        tv += (p * q) if (p and q) else 0
        vt = _se.VIRTUAL_TRADES.get(code) or {}
        sells = vt.get("SELL_HIGH", []); buys = vt.get("BUY_LOW", [])
        sq = sum(t.get("qty", 0) for t in sells); bq = sum(t.get("qty", 0) for t in buys)
        sa = sum(t.get("qty", 0) * max(t.get("price", 0), 0) for t in sells)
        ba = sum(t.get("qty", 0) * max(t.get("price", 0), 0) for t in buys)
        m = min(sq, bq)
        t0 = round(m * (sa / max(sq, 1) - ba / max(bq, 1)) - (sa + ba) * COMMISSION, 2)
        tt0 += t0
    print(f"[GM] {today} 市值:{tv:,.0f} T0:{tt0:+,.0f}")


def _get_holding(context, code, gm_symbol):
    try:
        pos = context.account().positions(symbol=gm_symbol, side=PositionSide_Long)
        if pos and len(pos) > 0:
            p = pos[0]
            return {"name": STOCK_NAMES.get(code, code), "qty": p.volume,
                    "available": p.available, "t_qty": p.volume,
                    "cost": p.vwap, "type": "etf" if code.startswith("5") else "stock",
                    "pre_close": float(p.vwap or 0), "price": float(getattr(p, "price", 0) or 0)}
    except: pass
    if code not in context.last_positions:
        context.last_positions[code] = {"qty": 0, "available": 0, "t_qty": 0, "cost": 0,
                                         "type": "etf" if code.startswith("5") else "stock",
                                         "pre_close": 0, "price": 0}
    return context.last_positions[code]
