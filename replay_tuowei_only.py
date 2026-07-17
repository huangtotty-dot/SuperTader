# -*- coding: utf-8 -*-
"""
Quick replay script for 拓维信息 only
"""
import sys, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

shared = {
    '__name__': '__main__',
    '__file__': __file__,
    'os': _os,
    'sys': _sys,
    'json': _json,
    'time': _time,
    'logging': _logging,
    'traceback': _traceback,
    'importlib': _importlib_util,
    'importlib.util': _importlib_util,
    'dataclass': dataclass,
    'field': field,
    'datetime': datetime,
    'timedelta': timedelta,
    'dtime': dtime,
    'Dict': Dict,
    'List': List,
    'Optional': Optional,
    'Any': Any,
    'np': np,
    'pd': pd,
    'requests': requests,
    'urllib': urllib.request,
    'urllib.request': urllib.request,
    'urllib.error': urllib.error,
}

try:
    import akshare as ak
    shared['akshare'] = ak
    shared['ak'] = ak
except Exception:
    pass

try:
    import log_enhancer as _log_enhancer
    shared['_log_enhancer'] = _log_enhancer
except Exception:
    shared['_log_enhancer'] = None

module_order = ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']
for mod_name in module_order:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    if not _os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

import tushare as ts
pro = ts.pro_api('9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def')

code = "002261"
holding = {
    "name": "拓维信息",
    "cost": 47.325,
    "qty": 300,
    "base": 300,
    "t_qty": 300,
    "type": "stock",
    "account": "账户A",
    "pre_close": 30.100
}
HOLDINGS[code] = holding

df = pro.stk_mins(ts_code='002261.SZ', freq='1min',
                  start_date='20260707 09:00:00',
                  end_date='20260707 19:00:00')

df = df.rename(columns={'trade_time': 'time', 'vol': 'volume', 'amount': 'amount'})
df['time'] = pd.to_datetime(df['time'])
df['date'] = df['time'].dt.date
df = df.sort_values('time').reset_index(drop=True)
for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')

df = add_indicators(df)

state = {
    "name": holding.get("name", code),
    "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
    "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
    "type": holding.get("type", "stock"),
    "cost": float(holding.get("cost") or 0),
}

engine = SignalEngine()
engine.state_reset_date = '20260707'
engine.buy_count_per_stock[code] = 0
engine.sell_count_per_stock[code] = 0
engine.post_sell_block_until[code] = None

DAILY_DECISION_STATS[code] = _ensure_daily_decision_stats(code, holding)
AI_REVIEW_STATS[code] = _ensure_ai_review_stats(code, holding)

# Test indices 5-25 (09:34-09:55)
for i in range(5, 26):
    sub_df = df.iloc[:i].copy()
    current_time = sub_df.iloc[-1]["time"]
    SIM_NOW = current_time
    t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
    
    MINUTE_FETCH_STATUS[code] = "ok"
    MINUTE_FETCH_DETAIL[code] = "tushare"
    
    daily_ctx = _default_daily_context(code)
    
    try:
        buy_score, sell_score, sig = engine.evaluate(
            code, holding.get("name", code), sub_df, state, daily_ctx=daily_ctx
        )
    except Exception as e:
        print(f"[!] {code} {SIM_NOW.strftime('%H:%M')} evaluate failed: {e}")
        continue
    
    action = sig.action if sig else 'None'
    score = sig.score if sig else 'N/A'
    print(f"{SIM_NOW.strftime('%H:%M')} i={i} buy={buy_score} sell={sell_score} action={action} score={score}")
    
    # Print price and profit info
    sub_last = sub_df.iloc[-1]
    price_now = float(sub_last['close'])
    vwap_now = float(sub_last['vwap']) if 'vwap' in sub_last else 0
    pre_close = holding['pre_close']
    cost = holding['cost']
    profit_pct = (price_now - cost) / cost if cost > 0 else 0
    sell_profit_space = (price_now - vwap_now) / vwap_now if vwap_now else 0
    print(f"  price={price_now:.2f} vwap={vwap_now:.2f} profit_pct={profit_pct*100:.1f}% sell_profit_space={sell_profit_space*100:.2f}%")
    
    # Print diagnostic info even when sig is None
    diag = engine.diagnostics.get(code, {})
    sell_block = diag.get("sell_block_reasons", [])
    buy_block = diag.get("buy_block_reasons", [])
    if sell_block or buy_block:
        print(f"  sell_block: {sell_block}")
        print(f"  buy_block: {buy_block}")
    print(f"  stand_down: {diag.get('last_stand_down_reason', 'N/A')}")
