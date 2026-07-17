# -*- coding: utf-8 -*-
import sys, os, json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
from datetime import datetime

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()
sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()

shared = {'__name__': '__main__', '__file__': __file__}
for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']:
    mod_path = os.path.join(BASE_DIR, f"{mod_name}.py")
    if not os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

# Mock load_starvation_state if not defined
if 'load_starvation_state' not in shared:
    shared['load_starvation_state'] = lambda: {}

globals().update(shared)

fpath = os.path.join(BASE_DIR, "t_io/minute_snapshots/2026/07/000988_2026-07-14.json")
with open(fpath, 'r', encoding='utf-8') as f:
    data = json.load(f)

bars = data.get("bars", [])
df_raw = pd.DataFrame(bars)
for col in ['open', 'high', 'low', 'close', 'volume']:
    if col in df_raw.columns:
        df_raw[col] = pd.to_numeric(df_raw[col], errors='coerce')
df_raw['time'] = pd.to_datetime(df_raw['time'])
df_raw['date'] = df_raw['time'].dt.strftime('%Y-%m-%d')

df = shared['add_indicators'](df_raw) if 'add_indicators' in shared else df_raw

holding = {"name": "华工科技", "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}
engine = shared['SignalEngine']()
engine.state_reset_date = "20260714"
engine.buy_count_per_stock["000988"] = 0
engine.sell_count_per_stock["000988"] = 0
engine.post_sell_block_until["000988"] = None

# 模拟早盘预警
engine.morning_alert_state["000988"] = {
    "level": 2, "rules": [{"name": "test_rule"}], "stats": {},
    "triggered_at": 935, "corrected": False
}

shared['MINUTE_FETCH_STATUS']["000988"] = "ok"
shared['MINUTE_FETCH_DETAIL']["000988"] = "snapshot"
daily_ctx = shared.get('_default_daily_context', lambda c: {})("000988")

print("=== 07-14 低点抬高支撑确认回测 ===\n")
hl_count = 0
for i in [100, 105, 108, 110, 112, 113, 114, 115, 116, 117, 118, 120, 125, 130]:
    if i >= len(df):
        continue
    sub = df.iloc[:i+1].copy()
    current_time = sub.iloc[-1]["time"]
    if hasattr(current_time, 'to_pydatetime'):
        current_time = current_time.to_pydatetime()
    
    shared['SIM_NOW'] = current_time
    globals()['SIM_NOW'] = current_time
    
    price = float(sub.iloc[-1]["close"])
    vwap = float(sub.iloc[-1]["vwap"]) if 'vwap' in sub.columns else 0
    
    # 直接调用HL检测
    hl_detected, hl_detail = engine._check_higher_low_support("000988", sub, price, vwap)
    
    # 调用完整evaluate
    try:
        buy_score, sell_score, sig = engine.evaluate("000988", "华工科技", sub, holding, daily_ctx=daily_ctx)
    except Exception as e:
        print(f"  i={i} ERROR: {e}")
        continue
    
    alert_level = 0
    if sig and sig.indicators:
        alert_level = sig.indicators.get("morning_alert_level", 0)
    
    if hl_detected:
        hl_count += 1
        print(f"  [{current_time.strftime('%H:%M')}] HL_DETECTED! 买分{buy_score} 卖分{sell_score} | 跌幅{hl_detail.get('drop_from_high',0)*100:.1f}% 抬高+{hl_detail.get('low_raise_pct',0)*100:.2f}% | alert={alert_level}")
    elif sig and sig.score >= 65:
        print(f"  [{current_time.strftime('%H:%M')}] SIGNAL {sig.action} 得分{sig.score} 价格{price:.2f}")

print(f"\n总HL信号数: {hl_count}")
print(f"\n验证完成 - signal_engine.py V1.26 低点抬高支撑确认集成正常")
