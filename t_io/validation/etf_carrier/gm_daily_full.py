# -*- coding: utf-8 -*-
"""gm 日线全历史拉取（588170，不复权）：作为快照量额单位判定的逐日权威参照。
结果存 data/gm_daily_full.json。只读探测，用户 Python 运行。"""
import sys, os, json
sys.path.insert(0, r'E:\superTrader')
sys.stdout.reconfigure(encoding='utf-8')
import gm.api as gma

gma.set_token(os.environ['GM_TOKEN'])
frames = []
end_time = '2026-09-15 15:00:00'
for i in range(4):
    df = gma.history_n(symbol='SHSE.588170', frequency='1d', count=200,
                       end_time=end_time, fields='eob,close,volume,amount',
                       adjust=gma.ADJUST_NONE, df=True)
    if df is None or df.empty:
        break
    frames.append(df)
    earliest = min(str(t)[:10] for t in df['eob'])
    print(f'chunk{i}: {len(df)} rows, earliest={earliest}')
    if earliest <= '2025-04-10':
        break
    end_time = earliest + ' 00:00:00'
import pandas as pd
all_df = pd.concat(frames).drop_duplicates(subset=['eob']).sort_values('eob')
rows = [(str(r['eob'])[:10], float(r['close']), float(r['volume']), float(r['amount']))
        for _, r in all_df.iterrows()]
json.dump(rows, open(r'E:\superTrader\t_io\validation\etf_carrier\data\gm_daily_full.json',
                     'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved', len(rows), 'rows,', rows[0][0], '~', rows[-1][0])
