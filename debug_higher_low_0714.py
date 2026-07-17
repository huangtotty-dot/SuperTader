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
for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine']:
    mod_path = os.path.join(BASE_DIR, f"{mod_name}.py")
    if not os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

# 加载07-14数据
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

# 添加指标
df = shared['add_indicators'](df_raw) if 'add_indicators' in shared else df_raw

print(f"总数据条数: {len(df)}")
print(f"列名: {list(df.columns)}")
print(f"vwap列存在: {'vwap' in df.columns}")
if 'vwap' in df.columns:
    print(f"vwap前5行: {df['vwap'].head().tolist()}")

# 检查11:15附近的数据
print("\n=== 11:10-11:30 数据 ===")
sub = df[(df['time'].dt.hour == 11) & (df['time'].dt.minute >= 10) & (df['time'].dt.minute <= 30)]
print(sub[['time', 'open', 'high', 'low', 'close', 'vwap']].to_string(index=False))

# 检查日内高点
day_high = df['high'].max()
print(f"\n日内高点: {day_high} 时间: {df.loc[df['high'].idxmax(), 'time']}")

# 检查低点抬高信号的条件
print("\n=== 手动检查低点抬高条件 ===")
for i in [110, 115, 120, 125, 130, 135, 140, 145]:
    if i >= len(df):
        continue
    sub_df = df.iloc[:i+1]
    price = float(sub_df.iloc[-1]['close'])
    vwap = float(sub_df.iloc[-1]['vwap']) if 'vwap' in sub_df.columns else 0
    last_time = sub_df.iloc[-1]['time']
    t_val = last_time.hour * 100 + last_time.minute
    
    day_high_so_far = float(sub_df['high'].max())
    drop = (day_high_so_far - price) / day_high_so_far if day_high_so_far > 0 else 0
    
    recent_lows = sub_df.iloc[-10:]['low'].astype(float).values
    lows_found = []
    for j in range(1, len(recent_lows) - 1):
        if recent_lows[j] <= recent_lows[j-1] and recent_lows[j] <= recent_lows[j+1]:
            lows_found.append((j, recent_lows[j]))
    
    hl_ok = False
    if len(lows_found) >= 2:
        last_two = lows_found[-2:]
        hl_ok = last_two[1][1] > last_two[0][1]
    elif len(recent_lows) >= 4:
        prev_low = min(recent_lows[-4:-2])
        curr_low = min(recent_lows[-2:])
        hl_ok = curr_low > prev_low
    
    vwap_ok = vwap > 0 and price < vwap * 0.995
    time_ok = 1000 <= t_val <= 1400
    drop_ok = drop >= 0.04
    
    print(f"  i={i} {last_time.strftime('%H:%M')} 价格={price:.2f} vwap={vwap:.2f} | 跌幅={drop*100:.1f}% HL={hl_ok} 价<VWAP={vwap_ok} 时间={time_ok} | 全部={drop_ok and hl_ok and vwap_ok and time_ok}")
