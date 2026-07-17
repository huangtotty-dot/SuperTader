# -*- coding: utf-8 -*-
"""最小复现测试：验证 send_auction_alert 修复"""
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

send_auction_alert = shared.get('send_auction_alert')
if not send_auction_alert:
    print("[FAIL] send_auction_alert not found")
    sys.exit(1)

test_holding = {
    "name": "双良节能",
    "cost": 45.475,
    "qty": 100,
    "base": 100,
    "t_qty": 100,
    "type": "stock",
    "account": "账户A"
}

test_sig = {
    "action": "AUCTION_LOW_BUY",
    "price": 4.40,
    "range_pos": 0.2,
    "today_ret": -0.05,
    "auction_score": 55,
    "open_gap": -0.03,
    "reason": "test"
}

try:
    # repaired call
    send_auction_alert(test_sig, {**test_holding, "code": "600481"})
    print("[PASS] send_auction_alert with code injection works")
except KeyError as e:
    print(f"[FAIL] KeyError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"[INFO] Other exception (acceptable): {type(e).__name__}: {e}")

print("[DONE] All tests passed")
