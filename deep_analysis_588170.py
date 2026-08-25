import json
import pandas as pd
import numpy as np

print("=" * 70)
print("🔬 588170 分钟级数据深度分析 — 为什么今天无信号？")
print("=" * 70)

# 读取 588170 分钟快照
try:
    with open('t_io/minute_snapshots/2026/08/588170_2026-08-25.json', 'r', encoding='utf-8') as f:
        snap = json.load(f)
    
    if 'data' in snap and snap['data']:
        df = pd.DataFrame(snap['data'])
        print(f"\n数据点数: {len(df)}")
        print(f"列: {list(df.columns)}")
        
        # 转换为时间序列
        df['datetime'] = pd.to_datetime(df['time'])
        df.set_index('datetime', inplace=True)
        
        # 计算5分钟K线
        df_5m = df.resample('5min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        print(f"\n5分钟K线数: {len(df_5m)}")
        
        # 计算布林带 (20期, 2倍标准差)
        df_5m['bb_mid'] = df_5m['close'].rolling(window=20).mean()
        df_5m['bb_std'] = df_5m['close'].rolling(window=20).std()
        df_5m['bb_upper'] = df_5m['bb_mid'] + 2 * df_5m['bb_std']
        df_5m['bb_lower'] = df_5m['bb_mid'] - 2 * df_5m['bb_std']
        df_5m['bb_pct'] = (df_5m['close'] - df_5m['bb_lower']) / (df_5m['bb_upper'] - df_5m['bb_lower'])
        df_5m['bb_pct'] = df_5m['bb_pct'].clip(0, 1)
        
        # 计算RSI(6)
        delta = df_5m['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=6).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=6).mean()
        rs = gain / loss
        df_5m['rsi_6'] = 100 - (100 / (1 + rs))
        
        print(f"\n5分钟K线 + 指标 (后10条):")
        print(df_5m[['close', 'bb_pct', 'rsi_6']].tail(10).to_string())
        
        # 检查是否有 bb_pct <= 0.15 且 rsi_6 < 35 的记录
        trigger = df_5m[(df_5m['bb_pct'] <= 0.15) & (df_5m['rsi_6'] < 35)]
        print(f"\n触发低吸条件 (bb_pct<=0.15 & rsi_6<35) 的记录: {len(trigger)} 条")
        if len(trigger) > 0:
            print(trigger[['close', 'bb_pct', 'rsi_6']].to_string())
        
        # 计算15分钟MACD
        df_15m = df.resample('15min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        ema12 = df_15m['close'].ewm(span=12, adjust=False).mean()
        ema26 = df_15m['close'].ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_hist = (dif - dea) * 2
        
        df_15m['dif'] = dif
        df_15m['dea'] = dea
        df_15m['macd_hist'] = macd_hist
        
        print(f"\n15分钟MACD:")
        print(df_15m[['close', 'macd_hist']].tail(10).to_string())
        
        # 检查MACD金叉时段
        golden = df_15m[df_15m['macd_hist'] > 0]
        print(f"\nMACD金叉 (hist>0) 的15分钟K线: {len(golden)} 条")
        if len(golden) > 0:
            print(golden[['close', 'macd_hist']].to_string())
        
        # 综合检查：bb_pct<=0.15 & rsi_6<35 & 15分MACD金叉
        # 需要把15分MACD对齐到5分钟
        df_5m['time_15m'] = df_5m.index.floor('15min')
        macd_map = df_15m['macd_hist'].to_dict()
        df_5m['macd_hist_15m'] = df_5m['time_15m'].map(macd_map)
        
        full_trigger = df_5m[
            (df_5m['bb_pct'] <= 0.15) & 
            (df_5m['rsi_6'] < 35) & 
            (df_5m['macd_hist_15m'] > 0)
        ]
        print(f"\n=== 综合触发条件 (bb_pct<=0.15 & rsi_6<35 & 15分MACD>0) ===")
        print(f"满足条件的记录: {len(full_trigger)} 条")
        if len(full_trigger) > 0:
            print(full_trigger[['close', 'bb_pct', 'rsi_6', 'macd_hist_15m']].to_string())
        else:
            print("无记录满足全部三个条件")
            
            # 分别检查每个条件
            c1 = df_5m[df_5m['bb_pct'] <= 0.15]
            c2 = df_5m[df_5m['rsi_6'] < 35]
            c3 = df_5m[df_5m['macd_hist_15m'] > 0]
            print(f"\n  仅 bb_pct<=0.15: {len(c1)} 条")
            print(f"  仅 rsi_6<35: {len(c2)} 条")
            print(f"  仅 15分MACD>0: {len(c3)} 条")
            
            if len(c1) > 0:
                print(f"\n  bb_pct<=0.15 的时段:")
                print(c1[['close', 'bb_pct', 'rsi_6', 'macd_hist_15m']].to_string())
    else:
        print("快照无 data 字段")
except Exception as e:
    print(f"分析失败: {e}")
    import traceback
    traceback.print_exc()

# 同时分析昨天（08-24）的数据作为对比
print("\n" + "=" * 70)
print("🔬 对比：08-24 588170 分钟级数据（昨天有信号）")
print("=" * 70)

try:
    with open('t_io/minute_snapshots/2026/08/588170_2026-08-24.json', 'r', encoding='utf-8') as f:
        snap = json.load(f)
    
    if 'data' in snap and snap['data']:
        df = pd.DataFrame(snap['data'])
        df['datetime'] = pd.to_datetime(df['time'])
        df.set_index('datetime', inplace=True)
        
        df_5m = df.resample('5min').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
        
        df_5m['bb_mid'] = df_5m['close'].rolling(window=20).mean()
        df_5m['bb_std'] = df_5m['close'].rolling(window=20).std()
        df_5m['bb_upper'] = df_5m['bb_mid'] + 2 * df_5m['bb_std']
        df_5m['bb_lower'] = df_5m['bb_mid'] - 2 * df_5m['bb_std']
        df_5m['bb_pct'] = (df_5m['close'] - df_5m['bb_lower']) / (df_5m['bb_upper'] - df_5m['bb_lower'])
        df_5m['bb_pct'] = df_5m['bb_pct'].clip(0, 1)
        
        delta = df_5m['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=6).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=6).mean()
        rs = gain / loss
        df_5m['rsi_6'] = 100 - (100 / (1 + rs))
        
        print(f"\n08-24 5分钟K线 + 指标 (11:00-11:20):")
        print(df_5m[['close', 'bb_pct', 'rsi_6']].loc['2026-08-24 11:00:00':'2026-08-24 11:20:00'].to_string())
        
        trigger = df_5m[(df_5m['bb_pct'] <= 0.15) & (df_5m['rsi_6'] < 35)]
        print(f"\n08-24 触发低吸条件 (bb_pct<=0.15 & rsi_6<35): {len(trigger)} 条")
        if len(trigger) > 0:
            print(trigger[['close', 'bb_pct', 'rsi_6']].to_string())
except Exception as e:
    print(f"08-24 分析失败: {e}")

print("\n" + "=" * 70)
print("分析完成")
print("=" * 70)
