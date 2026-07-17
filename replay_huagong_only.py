# -*- coding: utf-8 -*-
"""
Quick replay script for 华工科技 only
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

import tushare as ts
pro = ts.pro_api('9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def')

code = "000988"
holding = {
    "name": "华工科技",
    "cost": 207.205,
    "qty": 200,
    "base": 200,
    "t_qty": 200,
    "type": "stock",
    "account": "账户A",
    "pre_close": 149.700
}
HOLDINGS[code] = holding

df = pro.stk_mins(ts_code='000988.SZ', freq='1min',
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

# Test key indices for 华工科技
# 10:39 (i=69), 13:16 (i=136), 13:34 (i=154), 13:59 (i=179)
indices = [69, 70, 135, 136, 150, 154, 155, 175, 179, 180]
for i in indices:
    if i >= len(df):
        continue
    sub_df = df.iloc[:i].copy()
    current_time = sub_df.iloc[-1]["time"]
    SIM_NOW = current_time
    shared['SIM_NOW'] = SIM_NOW  # 更新 shared 中的 SIM_NOW，确保 _now() 返回模拟时间
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
    
    # Print all details with scores for debugging
    diag = engine.diagnostics.get(code, {})
    if sig and hasattr(sig, 'details') and sig.details:
        all_factors = []
        for d in sig.details:
            if isinstance(d, dict) and d.get('加分', 0) != 0:
                all_factors.append(f"{d.get('指标', '')}({d.get('加分', 0)})")
        if all_factors:
            print(f"  factors_with_score: {all_factors}")
    print(f"  strong_chop: {diag.get('strong_chop_detected', 'N/A')}")
    print(f"  peak_confirmed: {diag.get('peak_confirmed', 'N/A')}")
    print(f"  buy_score={buy_score}, sell_score={sell_score}")
    # Try to get threshold from internal state
    print(f"  diag_buy_threshold: {diag.get('buy_threshold', 'N/A')}")
    print(f"  diag_sell_threshold: {diag.get('sell_threshold', 'N/A')}")
    # Print last VWAP for context
    if len(sub_df) > 0:
        print(f"  price={sub_df.iloc[-1]['close']:.2f}, vwap={sub_df.iloc[-1].get('vwap', 0):.2f}")
    # Print awaiting_buyback status
    ab = engine.awaiting_buyback.get(code)
    if ab:
        expires = ab.get('expires')
        is_active = False
        if expires and isinstance(expires, datetime):
            is_active = _now() < expires
        print(f"  awaiting_buyback: active={is_active}, sell_price={ab.get('sell_price', 'N/A')}")
    else:
        print(f"  awaiting_buyback: None")
    
    # Print detailed buy conditions for debugging
    if action == 'None' and buy_score >= 60:
        diag = engine.diagnostics.get(code, {})
        print(f"  [DEBUG] buy_limit_reason: {diag.get('buy_limit_reason', 'N/A')}")
        print(f"  [DEBUG] buy_block_reasons: {diag.get('buy_block_reasons', 'N/A')}")
        print(f"  [DEBUG] priority_path: {diag.get('priority_path', 'N/A')}")
        print(f"  [DEBUG] buy_candidate: {diag.get('buy_candidate', 'N/A')}")
        bc = diag.get('buy_conditions')
        if bc:
            print(f"  [DEBUG] buy_conditions:")
            for k, v in bc.items():
                print(f"    {k}: {v}")
    
    # Print sell conditions for debugging when sell_score is high but no action
    if action == 'None' and sell_score >= 40:
        diag = engine.diagnostics.get(code, {})
        print(f"  [DEBUG_SELL] sell_block_reasons: {diag.get('sell_block_reasons', 'N/A')}")
        print(f"  [DEBUG_SELL] priority_path: {diag.get('priority_path', 'N/A')}")
        print(f"  [DEBUG_SELL] sell_candidate: {diag.get('sell_candidate', 'N/A')}")
        sc = diag.get('sell_conditions')
        if sc:
            print(f"  [DEBUG_SELL] sell_conditions:")
            for k, v in sc.items():
                print(f"    {k}: {v}")
        print(f"  [DEBUG_SELL] final_sell_score: {diag.get('final_sell_score', 'N/A')}")
        print(f"  [DEBUG_SELL] final_sell_threshold: {diag.get('final_sell_threshold', 'N/A')}")
    if action == 'None' and sell_score >= 40:
        diag = engine.diagnostics.get(code, {})
        print(f"  [DEBUG_SELL] sell_block_reasons: {diag.get('sell_block_reasons', 'N/A')}")
        print(f"  [DEBUG_SELL] priority_path: {diag.get('priority_path', 'N/A')}")
        print(f"  [DEBUG_SELL] sell_candidate: {diag.get('sell_candidate', 'N/A')}")
        sc = diag.get('sell_conditions')
        if sc:
            print(f"  [DEBUG_SELL] sell_conditions:")
            for k, v in sc.items():
                print(f"    {k}: {v}")
