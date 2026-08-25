import json
import pandas as pd
import numpy as np

# 尝试读取600481的分钟数据并计算15分MACD
try:
    with open('t_io/minute_snapshots/2026/08/600481_2026-08-24.json', 'r', encoding='utf-8') as f:
        snap = json.load(f)
    
    if 'data' in snap and snap['data']:
        df = pd.DataFrame(snap['data'])
        print("600481 分钟数据列:", list(df.columns))
        print(f"数据点数: {len(df)}")
        print(f"\n前3条:\n{df.head(3)}")
        print(f"\n后3条:\n{df.tail(3)}")
        
        # 计算15分钟K线
        df['datetime'] = pd.to_datetime(df['time'])
        df.set_index('datetime', inplace=True)
        df_15m = df.resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        print(f"\n15分钟K线数: {len(df_15m)}")
        if len(df_15m) >= 2:
            # 计算MACD
            ema12 = df_15m['close'].ewm(span=12, adjust=False).mean()
            ema26 = df_15m['close'].ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd_hist = (dif - dea) * 2
            
            df_15m['dif'] = dif
            df_15m['dea'] = dea
            df_15m['macd_hist'] = macd_hist
            
            print(f"\n15分钟MACD:\n{df_15m[['close', 'dif', 'dea', 'macd_hist']].tail(5)}")
            
            last_hist = df_15m['macd_hist'].iloc[-1]
            print(f"\n最新15分MACD柱状图: {last_hist:.4f}")
            print(f"  >0 (金叉/多头): {'是' if last_hist > 0 else '否'}")
    else:
        print("600481 快照无 data 字段")
except Exception as e:
    print(f"600481 分析失败: {e}")

print("\n" + "=" * 70)

# 同样分析588170
try:
    with open('t_io/minute_snapshots/2026/08/588170_2026-08-24.json', 'r', encoding='utf-8') as f:
        snap = json.load(f)
    
    if 'data' in snap and snap['data']:
        df = pd.DataFrame(snap['data'])
        print("588170 分钟数据列:", list(df.columns))
        print(f"数据点数: {len(df)}")
        
        df['datetime'] = pd.to_datetime(df['time'])
        df.set_index('datetime', inplace=True)
        df_15m = df.resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        if len(df_15m) >= 2:
            ema12 = df_15m['close'].ewm(span=12, adjust=False).mean()
            ema26 = df_15m['close'].ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd_hist = (dif - dea) * 2
            
            df_15m['dif'] = dif
            df_15m['dea'] = dea
            df_15m['macd_hist'] = macd_hist
            
            print(f"\n15分钟MACD:\n{df_15m[['close', 'dif', 'dea', 'macd_hist']].tail(5)}")
            
            # 找11:02附近的15分MACD
            near_1102 = df_15m.between_time('11:00', '11:15')
            if not near_1102.empty:
                print(f"\n11:00-11:15 的15分MACD:\n{near_1102[['close', 'macd_hist']]}")
    else:
        print("588170 快照无 data 字段")
except Exception as e:
    print(f"588170 分析失败: {e}")
