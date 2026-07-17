# -*- coding: utf-8 -*-
import sys, os, time

os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''

BASE_DIR = 'E:/06_T'
sys.path.insert(0, BASE_DIR)

import os as _os, pandas as pd
from datetime import datetime

shared = {'__name__': '__main__', '__file__': 'test.py', 'os': _os, 'sys': sys, 'pd': pd, 'datetime': datetime}

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()

sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()
shared['akshare'] = MockAkshare()
shared['ak'] = MockAkshare()

import logging
log = logging.getLogger('test')
log.setLevel(logging.WARNING)
shared['log'] = log

print('[0] Loading modules...')
t0 = time.time()
for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']:
    mod_path = _os.path.join(BASE_DIR, f'{mod_name}.py')
    if not _os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)
print(f'[0] Modules loaded in {time.time()-t0:.2f}s')

globals().update(shared)

import tushare as ts
ts.set_token('9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def')
pro = ts.pro_api()

print('[1] Fetching hist minute...')
t0 = time.time()
df = pro.stk_mins(ts_code='000988.SZ', freq='1min', start_date='2026-05-01 09:00:00', end_date='2026-06-22 19:00:00')
print(f'[1] Fetched {len(df)} rows in {time.time()-t0:.2f}s')

if df is not None and not df.empty:
    df = df.sort_values('trade_time').reset_index(drop=True)
    for col in ['open', 'close', 'high', 'low', 'vol']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df['time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['time'].dt.strftime('%Y-%m-%d')
    df['volume'] = df['vol']
    df = df[['time', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount']]
    
    print('[2] Aggregating daily...')
    t0 = time.time()
    daily = df.groupby('date').agg({'open':'first','high':'max','low':'min','close':'last'}).reset_index()
    daily = daily.sort_values('date').reset_index(drop=True)
    print(f'[2] Daily aggregated in {time.time()-t0:.2f}s, {len(daily)} days')
    
    # Slice 2026-06-02
    day_df = df[df['date'] == '2026-06-02'].copy()
    print(f'[3] 2026-06-02 minute rows: {len(day_df)}')
    
    if len(day_df) > 15:
        print('[4] Adding indicators...')
        t0 = time.time()
        day_df = add_indicators(day_df)
        print(f'[4] Indicators added in {time.time()-t0:.2f}s')
        
        daily_ctx = {
            'daily_status': 'ok',
            'daily_ma5': 158.24, 'daily_ma10': 159.90, 'daily_ma20': 148.84,
            'daily_ma30': 148.84, 'daily_ma60': 0.0, 'daily_ma150': 0.0,
            'daily_high_10d': 175.80, 'daily_low_10d': 142.58, 'pre2_close': 166.00,
        }
        state = {'name': '华工科技', 't_qty': 200, 'qty': 200, 'type': 'stock', 'cost': 207.205}
        
        engine = SignalEngine()
        engine.state_reset_date = '20260602'
        engine.buy_count_per_stock['000988'] = 0
        engine.sell_count_per_stock['000988'] = 0
        engine.post_sell_block_until['000988'] = None
        
        MINUTE_FETCH_STATUS['000988'] = 'ok'
        
        # Test a few evaluation points
        test_indices = [30, 60, 100, 135, 180, 240]
        for i in test_indices:
            if i >= len(day_df):
                continue
            sub = day_df.iloc[:i+1].copy()
            SIM_NOW = sub.iloc[-1]['time']
            globals()['SIM_NOW'] = SIM_NOW
            
            print(f'[5] Evaluating at i={i} ({SIM_NOW.strftime("%H:%M")})...')
            t0 = time.time()
            buy_score, sell_score, sig = engine.evaluate('000988', '华工科技', sub, state, daily_ctx=daily_ctx)
            elapsed = time.time() - t0
            print(f'    -> {elapsed:.2f}s: buy={buy_score}, sell={sell_score}, sig={sig.action if sig else None}')
        
        print('[6] DONE')
    else:
        print('[3] Not enough rows for 2026-06-02')
else:
    print('[1] No data fetched')
