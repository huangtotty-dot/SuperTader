# -*- coding: utf-8 -*-
"""E0 任务B Stage1：39票分钟线 -> 日级统计 shard（可续跑，2026-09-22）。

每票输出 shard parquet：t_io/validation/t0_bull/daystats/<code>.parquet
日级字段：
  date, n_bars, open, high, low, close, amount, volume,
  prev_close, amplitude = (high-low)/prev_close,
  vwap = amount/volume, dev_up = high/vwap-1, dev_dn = 1-low/vwap,
  open30_range = (H-L of 09:30-09:59)/prev_close,
  tail30_range = (H-L of 14:30-15:00)/prev_close,
  open30_amt, tail30_amt（金额占比分子）
因果自审：prev_close 用前一交易日收盘（T-1 信息），vwap/振幅为当日盘后统计，
          仅用于家底盘点，不做任何信号；无未来数据进入任何前瞻判断。
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'factor_mining'))
import minute_data as md

OUT_DIR = Path(__file__).resolve().parent / 'daystats'
OUT_DIR.mkdir(parents=True, exist_ok=True)

IS_START, IS_END = '2025-09-14', '2026-05-31'
OOS_START, OOS_END = '2026-06-01', '2026-08-26'

syms = md.pool_symbols()
print(f'池内代码数: {len(syms)}')

t0 = time.perf_counter()
for i, s in enumerate(syms):
    shard = OUT_DIR / f'{s}.parquet'
    if shard.exists():
        print(f'[{i+1}/{len(syms)}] {s} 已有 shard，跳过')
        continue
    df = md.load_minutes(s)  # 全量，不裁剪，先摸清真实覆盖
    if df.empty:
        print(f'[{i+1}/{len(syms)}] {s} 空数据！')
        continue
    df = df.sort_values('time').reset_index(drop=True)
    rows = []
    prev_close = np.nan
    for date, day in md.iter_days(df):
        hh = day['time'].dt.hour * 60 + day['time'].dt.minute
        o30 = day[(hh >= 570) & (hh < 600)]          # 09:30-09:59
        t30 = day[(hh >= 870) & (hh <= 900)]         # 14:30-15:00
        vol = day['volume'].sum()
        vwap = day['amount'].sum() / vol if vol > 0 else np.nan
        pc = prev_close if np.isfinite(prev_close) else np.nan
        amp = (day['high'].max() - day['low'].min()) / pc if np.isfinite(pc) else np.nan
        rows.append({
            'date': date,
            'n_bars': len(day),
            'open': day['open'].iloc[0], 'high': day['high'].max(),
            'low': day['low'].min(), 'close': day['close'].iloc[-1],
            'amount': day['amount'].sum(), 'volume': vol,
            'prev_close': pc, 'amplitude': amp, 'vwap': vwap,
            'dev_up': day['high'].max() / vwap - 1 if np.isfinite(vwap) else np.nan,
            'dev_dn': 1 - day['low'].min() / vwap if np.isfinite(vwap) else np.nan,
            'open30_range': (o30['high'].max() - o30['low'].min()) / pc
                            if len(o30) and np.isfinite(pc) else np.nan,
            'tail30_range': (t30['high'].max() - t30['low'].min()) / pc
                            if len(t30) and np.isfinite(pc) else np.nan,
            'open30_amt': o30['amount'].sum(),
            'tail30_amt': t30['amount'].sum(),
        })
        prev_close = day['close'].iloc[-1]
    ds = pd.DataFrame(rows)
    ds['code'] = s
    ds['segment'] = np.where(ds['date'] < '2026-06-01', 'IS', 'OOS')
    ds.to_parquet(shard, index=False)
    print(f'[{i+1}/{len(syms)}] {s}: {len(ds)} 日 '
          f'{ds["date"].iloc[0]}~{ds["date"].iloc[-1]} '
          f'({time.perf_counter()-t0:.0f}s 累计)')

print(f'\nStage1 完成，耗时 {time.perf_counter()-t0:.0f}s')
