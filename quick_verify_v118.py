# -*- coding: utf-8 -*-
import sys, os, json
sys.path.insert(0, 'E:/06_T')

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: __import__('pandas').DataFrame()
import pandas as pd
sys.modules['akshare'] = MockAkshare()

import os as _os, sys as _sys, json as _json, time as _time, logging as _logging
_logging.basicConfig(level=_logging.WARNING)

from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

_os.environ['http_proxy'] = ''
_os.environ['https_proxy'] = ''

shared = {'akshare': MockAkshare(), 'ak': MockAkshare(), 'log': _logging.getLogger('test')}
shared.update({
    '__name__': '__main__', '__file__': 'E:/06_T/main.py',
    'os': _os, 'sys': _sys, 'json': _json, 'time': _time, 'logging': _logging, 'traceback': __import__('traceback'),
    'dataclass': dataclass, 'field': field, 'datetime': datetime, 'timedelta': timedelta, 'dtime': dtime,
    'np': np, 'pd': pd, 'requests': requests, 'urllib': urllib,
})

for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen']:
    mod_path = _os.path.join('E:/06_T', f'{mod_name}.py')
    with open(mod_path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), mod_path, 'exec'), shared)

import tushare as ts
pro = ts.pro_api('9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def')

def get_data(code):
    if code.startswith(("5", "6", "9")):
        ts_code = f"{code}.SH"
    else:
        ts_code = f"{code}.SZ"
    df = pro.stk_mins(ts_code=ts_code, freq='1min', start_date='2026-07-07 09:00:00', end_date='2026-07-07 19:00:00')
    df = df.sort_values('trade_time').reset_index(drop=True)
    df['time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['time'].dt.strftime('%Y-%m-%d')
    df['volume'] = pd.to_numeric(df['vol'])
    for col in ['open', 'close', 'high', 'low', 'amount']:
        df[col] = pd.to_numeric(df[col])
    return df[['time', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount']]

add_indicators = shared.get('add_indicators')

def evaluate_at(df, code, holding, target_time_str):
    target_time = pd.to_datetime(f"2026-07-07 {target_time_str}")
    idx = df[df['time'] <= target_time].index[-1] if any(df['time'] <= target_time) else len(df)-1
    sub = df.iloc[:idx+1].copy()
    
    shared['SIM_NOW'] = sub.iloc[-1]['time']
    if hasattr(shared['SIM_NOW'], 'to_pydatetime'):
        shared['SIM_NOW'] = shared['SIM_NOW'].to_pydatetime()
    shared['MINUTE_FETCH_STATUS'][code] = 'ok'
    
    engine = shared['SignalEngine']()
    buy_score, sell_score, sig = engine.evaluate(code, holding['name'], sub, holding)
    
    diag = engine.diagnostics.get(code, {}) if hasattr(engine, 'diagnostics') else {}
    
    price = float(sub.iloc[-1]['close'])
    t = shared['SIM_NOW'].time()
    if sig:
        if sig.action in ["BUY_LOW", "ADD_POS"]:
            notify_threshold = 68
        else:
            if t >= dtime(10, 0):
                notify_threshold = 65
            else:
                notify_threshold = 75
        is_feishu = sig.score >= notify_threshold
    else:
        is_feishu = False
    
    return {
        'time': target_time_str,
        'price': price,
        'buy_score': buy_score,
        'sell_score': sell_score,
        'signal': sig.action if sig else None,
        'signal_score': sig.score if sig else 0,
        'is_feishu': is_feishu,
        'reasons': sig.reasons if sig else [],
        'factors': list(sig.factors.keys()) if sig and sig.factors else [],
        'indicators': {k: v for k, v in (sig.indicators.items() if sig and sig.indicators else {}) if k.startswith('v1_18') or k in ['market_state', 'vwap', 'today_ret']},
        'diag': {k: v for k, v in diag.items() if k.startswith('v1_18') or k in ['buy_block_reasons', 'sell_block_reasons', 'priority_path', 'buy_candidate', 'sell_candidate']},
    }

HOLDINGS = {
    "600089": {"name": "特变电工", "cost": 26.216, "qty": 1200, "base": 1200, "t_qty": 1200, "type": "stock", "pre_close": 21.350},
    "002261": {"name": "拓维信息", "cost": 47.325, "qty": 300, "base": 300, "t_qty": 300, "type": "stock", "pre_close": 30.100},
    "000988": {"name": "华工科技", "cost": 207.205, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "pre_close": 149.700},
    "300666": {"name": "江丰电子", "cost": 403.078, "qty": 100, "base": 100, "t_qty": 100, "type": "stock", "pre_close": 326.860},
    "588170": {"name": "科创半导体ETF华夏", "cost": 1.320, "qty": 30000, "base": 30000, "t_qty": 30000, "type": "etf", "pre_close": 1.192},
}

test_cases = [
    ("600089", "09:35", "特变电工早盘"),
    ("600089", "10:00", "特变电工10点"),
    ("600089", "11:30", "特变电工午盘"),
    ("600089", "13:30", "特变电工下午"),
    ("600089", "14:30", "特变电工尾盘前"),
    ("002261", "09:35", "拓维信息早盘"),
    ("002261", "10:00", "拓维信息10点"),
    ("002261", "11:30", "拓维信息午盘"),
    ("002261", "13:30", "拓维信息下午"),
    ("002261", "14:30", "拓维信息尾盘前"),
    ("000988", "13:09", "华工科技预期低吸点"),
    ("000988", "10:17", "华工科技下降开始"),
    ("000988", "11:30", "华工科技午盘"),
    ("300666", "13:17", "江丰电子预期低吸点"),
    ("588170", "11:30", "ETF午盘"),
    ("588170", "14:30", "ETF尾盘前"),
]

print("="*70)
print("V1.18 关键时间点验证报告 - 2026-07-07")
print("="*70)

feishu_commands = []
for code, t_str, desc in test_cases:
    print(f"\n[{desc}] {code}")
    try:
        df_raw = get_data(code)
        df = add_indicators(df_raw)
        holding = HOLDINGS[code]
        result = evaluate_at(df, code, holding, t_str)
        
        print(f"  价格: {result['price']:.2f} | 买分: {result['buy_score']:.0f} | 卖分: {result['sell_score']:.0f}")
        
        v1_18 = result.get('indicators', {})
        diag = result.get('diag', {})
        print(f"  V1.18: weak={v1_18.get('v1_18_weak_oscillation')}, steep={v1_18.get('v1_18_steep_decline')}, crossing={v1_18.get('v1_18_vwap_crossing')}, below_ratio={v1_18.get('v1_18_below_vwap_ratio')}, slope={v1_18.get('v1_18_slope_pct')}")
        
        if result['signal']:
            action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(result['signal'], result['signal'])
            print(f"  信号: {action_cn} @ {result['price']:.2f} 得分:{result['signal_score']:.0f}")
            if result['is_feishu']:
                print(f"  >>> 飞书通知: {action_cn} {holding['name']} @ {result['price']:.2f} 得分:{result['signal_score']:.0f}")
                feishu_commands.append({
                    'time': t_str, 'code': code, 'name': holding['name'],
                    'action': result['signal'], 'price': result['price'],
                    'score': result['signal_score'], 'desc': desc
                })
            else:
                print(f"  (未达飞书通知阈值)")
        else:
            print(f"  信号: 无")
            if diag.get('buy_block_reasons'):
                print(f"  买入阻塞原因: {diag['buy_block_reasons']}")
    except Exception as e:
        print(f"  [ERROR] {e}")

print(f"\n{'='*70}")
print("[飞书通知命令汇总]")
print("="*70)
for cmd in feishu_commands:
    action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(cmd['action'], cmd['action'])
    print(f"  {cmd['time']} {cmd['name']}({cmd['code']}) {action_cn} @ {cmd['price']:.2f} 得分:{cmd['score']}")

print(f"\n总计飞书通知: {len(feishu_commands)} 条")

report = {
    'date': '2026-07-07',
    'version': 'V1.18',
    'feishu_commands': feishu_commands,
}
with open('E:/06_T/backtest_0707_v118_quick_report.json', 'w', encoding='utf-8') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"\n报告已保存: E:/06_T/backtest_0707_v118_quick_report.json")
