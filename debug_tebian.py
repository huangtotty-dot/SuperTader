# -*- coding: utf-8 -*-
"""
Debug script for 特变电工 09:44 evaluation
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
    'urllib': urllib,
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

def debug_tebian():
    engine = SignalEngine()
    
    # Fetch real data
    import tushare as ts
    pro = ts.pro_api('9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def')
    df = pro.stk_mins(ts_code='600089.SH', freq='1min',
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
    
    holding = {"name": "特变电工", "type": "stock", "t_qty": 1200, "pre_close": 21.35, "cost": 21.35}
    HOLDINGS["600089"] = holding
    MINUTE_FETCH_STATUS["600089"] = "ok"
    
    # Test at 09:44 (index 14, 15 bars)
    sub_df = df.iloc[:15].copy()
    SIM_NOW = sub_df.iloc[-1]["time"]
    t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
    
    print(f"Testing at {SIM_NOW.strftime('%H:%M')} (t_val={t_val})")
    print(f"Price: {sub_df.iloc[-1]['close']}")
    print(f"High so far: {sub_df['high'].max()}")
    print(f"Low so far: {sub_df['low'].min()}")
    print(f"Pre_close: {holding['pre_close']}")
    print()
    
    # Monkey-patch to capture debug info
    original_should_stand_down = engine._should_stand_down
    
    def debug_stand_down(code, holding, df, buy_score, sell_score, market_state, can_sell, today_ret=0.0, minutes_since_open=0):
        result = original_should_stand_down(code, holding, df, buy_score, sell_score, market_state, can_sell, today_ret, minutes_since_open)
        print(f"  _should_stand_down: market_state={market_state}, minutes={minutes_since_open}, today_ret={today_ret*100:.2f}%, buy_score={buy_score}, sell_score={sell_score}, can_sell={can_sell}, result={result}")
        return result
    
    engine._should_stand_down = debug_stand_down
    
    buy_score, sell_score, sig = engine.evaluate("600089", "特变电工", sub_df, holding)
    
    print(f"\nFinal result with 'holding':")
    print(f"  buy_score: {buy_score}")
    print(f"  sell_score: {sell_score}")
    print(f"  sig: {sig.action if sig else 'None'}")
    if sig:
        print(f"  sig.score: {sig.score}")
    
    # Also test with 'state' like replay script
    state = {
        "name": holding.get("name", "600089"),
        "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
        "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
        "type": holding.get("type", "stock"),
        "cost": float(holding.get("cost") or 0),
    }
    
    buy_score2, sell_score2, sig2 = engine.evaluate("600089", "特变电工", sub_df, state)
    
    print(f"\nFinal result with 'state' (like replay script):")
    print(f"  buy_score: {buy_score2}")
    print(f"  sell_score: {sell_score2}")
    print(f"  sig: {sig2.action if sig2 else 'None'}")
    if sig2:
        print(f"  sig.score: {sig2.score}")
    
    print(f"\nManual morning scene check:")
    price = float(sub_df.iloc[-1]["close"])
    pre_close = 21.35
    today_open = float(sub_df.iloc[0]["open"])
    morning_low = float(sub_df.iloc[:15]["low"].min())
    morning_high = float(sub_df.iloc[:15]["high"].max())
    current_idx = 14
    minutes_since_open = current_idx
    
    had_breakdown = morning_low < pre_close * 0.995
    rebound_from_low = (morning_high - morning_low) / morning_low if morning_low > 0 else 0
    pullback_from_high = (morning_high - price) / morning_high if morning_high > 0 else 0
    
    print(f"  morning_low: {morning_low}")
    print(f"  morning_high: {morning_high}")
    print(f"  price: {price}")
    print(f"  pre_close: {pre_close}")
    print(f"  pre_close*0.995: {pre_close*0.995}")
    print(f"  had_breakdown: {had_breakdown} ({morning_low} < {pre_close*0.995})")
    print(f"  rebound_from_low: {rebound_from_low*100:.2f}% (threshold: 0.2%)")
    print(f"  pullback_from_high: {pullback_from_high*100:.2f}% (threshold: 0.5%)")
    print(f"  Case A: {morning_high < pre_close * 0.995 and price < pre_close}")
    print(f"  Case B: {morning_high >= pre_close * 0.995 and price < pre_close * 0.995 and pullback_from_high > 0.005}")

if __name__ == "__main__":
    debug_tebian()
