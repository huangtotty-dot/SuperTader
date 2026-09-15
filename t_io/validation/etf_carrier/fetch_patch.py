# -*- coding: utf-8 -*-
"""补齐 2026-03-19 ~ 2026-05-20 段（gm 基金 180 自然日权限内的最早段）。
同时探测基金净值(NAV)字段可得性。用户 Python 运行。"""
import sys, os, json
sys.path.insert(0, r'E:\superTrader')
import pandas as pd

OUT_DIR = r'E:\superTrader\t_io\validation\etf_carrier\data'
import gm.api as gma
token = os.environ['GM_TOKEN']
gma.set_token(token)

SYMBOLS = {'SHSE.513130': '513130.SH', 'SHSE.513180': '513180.SH'}
# 从现有 CSV 的最早时间继续往前
for gm_sym, ts_code in SYMBOLS.items():
    path = os.path.join(OUT_DIR, ts_code.split('.')[0] + '_1year_1min.csv')
    old = pd.read_csv(path)
    end_time = old['time'].min()
    frames = []
    for i in range(10):
        try:
            df = gma.history_n(symbol=gm_sym, frequency='60s', count=9000,
                               end_time=end_time,
                               fields='eob,open,high,low,close,volume,amount',
                               adjust=gma.ADJUST_PREV, df=True)
        except Exception as e:
            print(ts_code, 'patch stop:', str(e)[:120])
            break
        if df is None or df.empty:
            break
        t = pd.to_datetime(df['eob']).dt.tz_localize(None)
        frames.append(df.assign(t=t))
        earliest = t.min()
        print(ts_code, f'patch chunk{i}: {len(df)} bars, earliest={earliest}')
        if str(earliest)[:10] <= '2026-03-20':
            break
        end_time = earliest.strftime('%Y-%m-%d %H:%M:%S')
    if frames:
        add = pd.concat(frames).drop_duplicates(subset=['t'])
        add_out = pd.DataFrame({
            'ts_code': ts_code,
            'time': add['t'].dt.strftime('%Y-%m-%d %H:%M:%S'),
            'close': add['close'].astype(float), 'open': add['open'].astype(float),
            'high': add['high'].astype(float), 'low': add['low'].astype(float),
            'volume': add['volume'].astype(float), 'amount': add['amount'].astype(float),
        })
        merged = pd.concat([old, add_out]).drop_duplicates(subset=['time']).sort_values('time')
        merged.to_csv(path, index=False, encoding='utf-8')
        print(ts_code, 'merged:', len(merged), 'bars,', merged['time'].str[:10].nunique(), 'days,',
              merged['time'].min(), '~', merged['time'].max())

# NAV 探测：基金基本面表
nav_probe = {}
try:
    df = gma.get_fundamentals(table='fund', symbols='SHSE.513130', df=True)
    nav_probe['fund_table'] = {'ok': True, 'columns': list(df.columns),
                               'sample': df.astype(str).to_dict('records')[:2]}
except Exception as e:
    nav_probe['fund_table'] = {'ok': False, 'error': repr(e)[:300]}
try:
    df = gma.get_fundamentals_n(table='fund', symbols='SHSE.513130', count=5,
                                end_date='2026-09-15', df=True)
    nav_probe['fund_table_n'] = {'ok': True, 'columns': list(df.columns),
                                 'sample': df.astype(str).to_dict('records')[:3]}
except Exception as e:
    nav_probe['fund_table_n'] = {'ok': False, 'error': repr(e)[:300]}
json.dump(nav_probe, open(os.path.join(OUT_DIR, 'nav_probe.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
print('nav probe saved')
