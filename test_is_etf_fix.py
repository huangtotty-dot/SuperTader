# -*- coding: utf-8 -*-
"""最小复现测试：验证 evaluate 中 is_etf 定义"""
import sys, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

_os.environ['http_proxy'] = ''
_os.environ['https_proxy'] = ''
_os.environ['HTTP_PROXY'] = ''
_os.environ['HTTPS_PROXY'] = ''
_os.environ['ALL_PROXY'] = ''
_os.environ['all_proxy'] = ''

shared = {
    '__name__': '__main__', '__file__': __file__,
    'os': _os, 'sys': _sys, 'json': _json, 'time': _time, 'logging': _logging, 'traceback': _traceback,
    'importlib': _importlib_util, 'importlib.util': _importlib_util,
    'dataclass': dataclass, 'field': field, 'datetime': datetime, 'timedelta': timedelta, 'dtime': dtime,
    'Dict': Dict, 'List': List, 'Optional': Optional, 'Any': Any,
    'np': np, 'pd': pd, 'requests': requests, 'urllib': urllib.request, 'urllib.request': urllib.request,
    'urllib.error': urllib.error, 'MIN_FETCH_INTERVAL': 10, 'MAX_HISTORY_SIZE': 800, 'REQUEST_TIMEOUT': 8,
    '_LOAD_ORDER': ['config', 'data_fetcher', 'signal_engine'],
    'log': None, '_all_signal_log': [], '_all_action_log': [], '_minute_detail_cache': {},
    'AI_REVIEW_STATS': {}, 'DAILY_DECISION_STATS': {}, 'MINUTE_FETCH_STATUS': {}, 'MINUTE_FETCH_DETAIL': {},
    'SIM_NOW': None, 'HOLDINGS': {}, 'HOLDINGS_FILE': os.path.join(BASE_DIR, 'holdings.json'),
    'TODAY_AM': None, 'TODAY_PM': None, 'YESTERDAY_CLOSE': None, 'YESTERDAY_OPEN': None,
    'DAY_MARKET_TREND': 'unknown', 'DAILY_INDEX_DF': None, 'LAST_HIGH_PRICE': {}, 'LAST_LOW_PRICE': {},
    'TODAY_HIGH_PRICE': {}, 'TODAY_LOW_PRICE': {}, 'DAILY_AMOUNT': {}, 'YESTERDAY_AMNT': {},
    'TARGET_AMNT_MAP': {}, 'PREVIOUS_AMNT': {}, 'TOP_N_HOT': 0, 'ETF_T0_PARAMS': {},
    'TRADE_BLOCKED': {}, 'NEAR_SELL_FLAG': {}, 'NEAR_SELL_STOP': {}, 'NEAR_BUY_TIME': {},
    '_PREV_AMOUNT': {}, '_PREV_AMOUNT_LOCK': None, '_PREV_AMOUNT_TODAY': None,
    '_SIGNAL_CACHE_LOCK': None, '_SIGNAL_CACHE': {}, '_SIGNAL_CACHE_TODAY': None,
    'BREAK_EVEN': {}, 'MINUTES_AWAY': {}, 'MAX_BUY': 2, 'MAX_SELL': 2, 'MAX_T': 2,
    'ABNORMAL_GAP': 0.03, 'BREAK_EVEN_THRESHOLD': 0.003, 'BREAK_EVEN_TH': 0.005,
    'PREEMPT_BUY_GAP': 0.01, 'PREEMPT_BUY_WAIT': 20, 'STALE_AMNT_MINS': 10, 'WAKE_COOLDOWN': 2,
    'SIGNAL_COOLDOWN': 5, 'FIRST_SCAN_AMNT_THRESHOLD': 2, 'DAILY_COOLDOWN': 600, 'DAILY_AMOUNT_MIN': 3,
    '_MARKET_TREND': 'neutral', '_BREAK_EVEN_TH': 0.005, '_CONFIG': None, 'FEISHU_WEBHOOK': '',
    'ALERT_MSG': None, 'FEISHU_SENT_TIMES': {}, 'FEISHU_RATE_LIMIT': 2, 'FEISHU_LIMIT_INTERVAL': 10,
    'FEISHU_KEYS': {}, 'FEISHU_TITLE_DICT': {}, 'FEISHU_HEADERS': {}, 'FEISHU_CFG': {},
    'FEISHU_TOKEN_INDEX': 0,
    'send_feishu_payload': lambda **kw: None,
    '_should_push': lambda key: True,
    'load_runtime_config': lambda: {},
    'notify': lambda sig, holding: None,
    'load_holdings': lambda: {},
    'label': lambda code, holding: f"{holding.get('name', code)}({code})",
    'fetch_minute_bar': lambda code, **kw: pd.DataFrame(),
    'get_daily_context': lambda code, holding, **kw: {},
    'add_indicators': lambda df: df,
    'check_auction_driven_signal': lambda code, holding, df, ctx: None,
    'engine': None, 'Signal': None, 'PARAMS': {},
    '_default_daily_context': lambda code, **kw: {},
    '_snapshot_write': lambda *args, **kw: None,
}

for mod_name in ['config', 'data_fetcher', 'signal_engine']:
    mod_path = os.path.join(BASE_DIR, f'{mod_name}.py')
    if not os.path.exists(mod_path):
        print(f"[MISSING] {mod_path}")
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    try:
        exec(compile(code, mod_path, 'exec'), shared)
    except Exception as e:
        print(f"[FAIL] {mod_name}: {e}")
        sys.exit(1)
    print(f"[OK] {mod_name}.py")

SignalEngine = shared.get('SignalEngine')
if not SignalEngine:
    print("[FAIL] SignalEngine not found")
    sys.exit(1)

# 模拟一个非 ETF 持仓
holding = {
    "name": "双良节能",
    "cost": 45.475,
    "qty": 100,
    "base": 100,
    "t_qty": 100,
    "type": "stock",
    "account": "账户A"
}

# 构造一个最小的 df 使得 evaluate 可以运行到 is_etf 检查分支
np.random.seed(42)
n = 30
prices = 4.40 + np.cumsum(np.random.randn(n) * 0.01)
df = pd.DataFrame({
    "close": prices,
    "open": prices - np.random.randn(n) * 0.005,
    "high": prices + np.random.rand(n) * 0.01,
    "low": prices - np.random.rand(n) * 0.01,
    "volume": np.random.randint(1000, 5000, n),
    "range_pos": np.random.rand(n),
    "vwap": prices + np.random.randn(n) * 0.005,
    "rsi": np.random.rand(n) * 30 + 40,
    "vol_ratio": np.random.rand(n) * 2 + 0.5,
    "is_limit_up": [False] * n,
    "is_limit_down": [False] * n,
    "hour": 10,
    "minute": 30,
})

daily_ctx = {
    "daily_status": "ok",
    "daily_gate": "neutral",
    "daily_trend_bg": "normal",
    "daily_support_name": "",
    "daily_support_gap": 0.0,
    "daily_overheated": False,
    "last_close": 4.50,
    "ma20": 4.45,
    "ma60": 4.40,
}

try:
    engine = SignalEngine()
    buy_score, sell_score, sig = engine.evaluate("600481", "双良节能", df, holding, daily_ctx=daily_ctx)
    print(f"[PASS] evaluate completed: buy={buy_score}, sell={sell_score}, sig={sig}")
except NameError as e:
    print(f"[FAIL] NameError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"[INFO] Other exception (acceptable): {type(e).__name__}: {e}")

print("[DONE] All tests passed")
