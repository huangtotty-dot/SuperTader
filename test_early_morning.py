# -*- coding: utf-8 -*-
"""
Quick focused test for 07-07 early morning scenarios
Tests 特变电工 at 09:44 and 拓维信息 at 09:34
"""
import sys, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Shared namespace setup (same as main.py and replay script)
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

def test_early_morning():
    engine = SignalEngine()
    
    # Load holdings with pre_close
    holdings_data = {
        "600089": {"name": "特变电工", "type": "stock", "hold_qty": 1200, "pre_close": 21.35, "cost": 21.35},
        "002261": {"name": "拓维信息", "type": "stock", "hold_qty": 300, "pre_close": 30.10, "cost": 30.10},
    }
    
    # Update shared HOLDINGS
    HOLDINGS.update(holdings_data)
    
    # Set minute status for test
    MINUTE_FETCH_STATUS["600089"] = "ok"
    MINUTE_FETCH_STATUS["002261"] = "ok"
    
    print("=" * 60)
    print("TEBIANGONGDIAN (600089) - 09:44 test")
    print("=" * 60)
    
    # Simulate at 09:44 (14th minute bar)
    # open 21.35, drop to ~21.15, rebound to 21.23
    dates = [datetime(2026, 7, 7, 9, 30) + timedelta(minutes=i) for i in range(15)]
    prices = [21.35, 21.30, 21.25, 21.20, 21.18, 21.16, 21.15, 21.15, 21.16, 21.18, 21.20, 21.22, 21.23, 21.22, 21.20]
    
    df = pd.DataFrame({
        "date": [d.time() for d in dates],
        "open": prices,
        "high": [p + 0.01 for p in prices],
        "low": [p - 0.01 for p in prices],
        "close": prices,
        "volume": [1000] * 15,
        "vwap": [21.166] * 15,
        "range_pos": [0.5] * 15,
        "prev_high": [21.35] * 15,
    })
    
    holding = holdings_data["600089"]
    buy_score, sell_score, sig = engine.evaluate("600089", "TEBIANGONGDIAN", df, holding)
    
    print(f"  buy_score: {buy_score}")
    print(f"  sell_score: {sell_score}")
    print(f"  decision: {sig.action if sig else 'None'}")
    print(f"  reason: {sig.reason if sig else 'N/A'}")
    print(f"  price: {prices[-1]}")
    print(f"  today_ret: {(prices[-1] - 21.35) / 21.35 * 100:.2f}%")
    
    print()
    print("=" * 60)
    print("TUOWEI (002261) - 09:34 test")
    print("=" * 60)
    
    # Simulate at 09:34 (4th minute bar, but need 15 rows)
    # open 29.53, drop to 29.40, rebound to 30.30
    dates2 = [datetime(2026, 7, 7, 9, 30) + timedelta(minutes=i) for i in range(15)]
    # Pad with 11 more bars after the rebound
    prices2 = [29.53, 29.40, 29.50, 30.10, 30.30, 30.25, 30.20, 30.15, 30.10, 30.05, 30.00, 29.95, 29.90, 29.85, 29.80]
    
    df2 = pd.DataFrame({
        "date": [d.time() for d in dates2],
        "open": prices2,
        "high": [p + 0.02 for p in prices2],
        "low": [p - 0.02 for p in prices2],
        "close": prices2,
        "volume": [1000] * 15,
        "vwap": [29.85] * 15,
        "range_pos": [0.5] * 15,
        "prev_high": [30.10] * 15,
    })
    
    # Test at index 4 (09:34)
    df2_test = df2.iloc[:5].copy()
    
    holding2 = holdings_data["002261"]
    buy_score2, sell_score2, sig2 = engine.evaluate("002261", "TUOWEI", df2_test, holding2)
    
    print(f"  buy_score: {buy_score2}")
    print(f"  sell_score: {sell_score2}")
    print(f"  decision: {sig2.action if sig2 else 'None'}")
    print(f"  reason: {sig2.reason if sig2 else 'N/A'}")
    print(f"  price: {prices2[4]}")
    print(f"  today_ret: {(prices2[4] - 30.10) / 30.10 * 100:.2f}%")
    
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    if sig and sig.action == "SELL_HIGH":
        print(f"OK TEBIANGONGDIAN: 09:44 triggered SELL_HIGH (score {sell_score})")
    else:
        print(f"FAIL TEBIANGONGDIAN: 09:44 no sell signal (score {sell_score})")
    
    if sig2 and sig2.action == "SELL_HIGH":
        print(f"OK TUOWEI: 09:34 triggered SELL_HIGH (score {sell_score2})")
    else:
        print(f"FAIL TUOWEI: 09:34 no sell signal (score {sell_score2})")

if __name__ == "__main__":
    test_early_morning()
