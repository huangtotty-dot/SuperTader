# -*- coding: utf-8 -*-
"""A10 ETF 载体验证实验 · 数据拉取（实验员_E2，2026-09-15）。

用用户 Python（gm SDK 只装在那里）拉取 513130 / 513180 近 12 个月 1min K 线，
落盘到 t_io/validation/etf_carrier/data/，格式与 t_io/backtest_1year_data 一致：
    ts_code,time,close,open,high,low,volume,amount
（volume=股，amount=元，time 为 gm eob 原生标签，前复权 ADJUST_PREV）

同时探测 IOPV/溢价率数据在 gm SDK 的可得性，结果写入 data/iopv_probe.json。

运行：C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\python.exe fetch_data.py
"""
import sys, os, json
sys.path.insert(0, r'E:\superTrader')
import pandas as pd

ROOT = r'E:\superTrader'
OUT_DIR = os.path.join(ROOT, 't_io', 'validation', 'etf_carrier', 'data')
LOG = os.path.join(OUT_DIR, 'fetch_log.txt')
os.makedirs(OUT_DIR, exist_ok=True)

log_lines = []
def log(msg):
    print(msg)
    log_lines.append(str(msg))

import gm.api as gma
from core.market_data.gm_token import load_token

token = os.environ.get('GM_TOKEN') or load_token()
assert token, 'gm token 不可得（掘金终端未运行？）'
gma.set_token(token)

SYMBOLS = {'SHSE.513130': '513130.SH', 'SHSE.513180': '513180.SH'}
END = '2026-09-15 15:00:00'
# gm 基金品种权限：只能下载最近 180 个自然日（>=2026-03-19）——权限实测结论
START_LIMIT = '2026-03-19'
CHUNK = 10000

for gm_sym, ts_code in SYMBOLS.items():
    frames, end_time = [], END
    for i in range(20):
        try:
            df = gma.history_n(symbol=gm_sym, frequency='60s', count=CHUNK,
                               end_time=end_time,
                               fields='eob,open,high,low,close,volume,amount',
                               adjust=gma.ADJUST_PREV, df=True)
        except Exception as e:
            log(f'{ts_code} chunk{i}: GmError(权限/其他), stop: {e}')
            break
        if df is None or df.empty:
            log(f'{ts_code} chunk{i}: empty, stop')
            break
        t = pd.to_datetime(df['eob']).dt.tz_localize(None)
        df = df.assign(t=t)
        frames.append(df)
        earliest = t.min()
        log(f'{ts_code} chunk{i}: {len(df)} bars, {earliest} ~ {t.max()}')
        if str(earliest)[:10] <= START_LIMIT:
            break
        end_time = earliest.strftime('%Y-%m-%d %H:%M:%S')
        if len(df) < CHUNK:
            break
    if not frames:
        log(f'{ts_code}: 无数据！')
        continue
    alldf = pd.concat(frames).drop_duplicates(subset=['t']).sort_values('t')
    out = pd.DataFrame({
        'ts_code': ts_code,
        'time': alldf['t'].dt.strftime('%Y-%m-%d %H:%M:%S'),
        'close': alldf['close'].astype(float),
        'open': alldf['open'].astype(float),
        'high': alldf['high'].astype(float),
        'low': alldf['low'].astype(float),
        'volume': alldf['volume'].astype(float),
        'amount': alldf['amount'].astype(float),
    })
    path = os.path.join(OUT_DIR, ts_code.split('.')[0] + '_1year_1min.csv')
    out.to_csv(path, index=False, encoding='utf-8')
    days = out['time'].str[:10].nunique()
    log(f'{ts_code}: saved {len(out)} bars, {days} days -> {path}')

# ---------- IOPV / 溢价率数据可得性探测 ----------
probe = {'symbol': 'SHSE.513130', 'probes': {}}
# 1) get_instrumentinfos 看有没有 iopv 相关字段
try:
    info = gma.get_instrumentinfos(symbols='SHSE.513130', df=True)
    probe['probes']['get_instrumentinfos'] = {
        'ok': True,
        'columns': list(info.columns),
        'row': {k: (str(v)[:100]) for k, v in info.iloc[0].to_dict().items()} if len(info) else {},
    }
except Exception as e:
    probe['probes']['get_instrumentinfos'] = {'ok': False, 'error': repr(e)}
# 2) history 日线尝试 iopv 字段
try:
    df = gma.history_n(symbol='SHSE.513130', frequency='1d', count=5,
                       end_time=END, fields='eob,close,iopv',
                       adjust=gma.ADJUST_PREV, df=True)
    probe['probes']['history_1d_iopv'] = {
        'ok': True, 'columns': list(df.columns),
        'sample': df.astype(str).to_dict('records')[:3],
    }
except Exception as e:
    probe['probes']['history_1d_iopv'] = {'ok': False, 'error': repr(e)}
# 3) current 实时快照字段
try:
    cur = gma.current(symbols='SHSE.513130')
    probe['probes']['current'] = {
        'ok': True,
        'keys': [list(c.keys()) for c in cur] if cur else [],
        'sample': [{k: str(v)[:60] for k, v in c.items()} for c in (cur or [])[:1]],
    }
except Exception as e:
    probe['probes']['current'] = {'ok': False, 'error': repr(e)}

json.dump(probe, open(os.path.join(OUT_DIR, 'iopv_probe.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1, default=str)
log('iopv probe saved')
open(LOG, 'w', encoding='utf-8').write('\n'.join(log_lines))
log('DONE')
