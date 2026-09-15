# -*- coding: utf-8 -*-
"""588170 载体验证实验（实验员_E4，2026-09-15）· IOPV 可得性探测。

对 SHSE.588170 复测 A10 在 513130 上的探测口径：
  1) current() 实时快照是否带 iopv 字段（588170 为场内 ETF，IOPV 每 15s 刷新）；
  2) history_n 1d 请求 iopv 字段是否被静默丢弃；
  3) get_instrumentinfos 是否有 iopv/净值相关字段。
只读探测，结果落盘 data/iopv_probe_588170.json。

运行：C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\python.exe iopv_probe_588170.py
"""
import sys, os, json, datetime
sys.path.insert(0, r'E:\superTrader')

OUT = r'E:\superTrader\t_io\validation\etf_carrier\data\iopv_probe_588170.json'

import gm.api as gma
from core.market_data.gm_token import load_token

token = os.environ.get('GM_TOKEN') or load_token()
assert token, 'gm token 不可得（掘金终端未运行？）'
gma.set_token(token)

SYM = 'SHSE.588170'
probe = {'symbol': SYM, 'probe_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         'probes': {}}

# 1) current 实时快照（重点：iopv 字段）
try:
    cur = gma.current(symbols=SYM)
    probe['probes']['current'] = {
        'ok': True,
        'keys': [list(c.keys()) for c in cur] if cur else [],
        'sample': [{k: str(v)[:80] for k, v in c.items()} for c in (cur or [])[:1]],
    }
except Exception as e:
    probe['probes']['current'] = {'ok': False, 'error': repr(e)[:300]}

# 2) history 日线请求 iopv 字段（A10 实测被静默丢弃，本票复测）
try:
    df = gma.history_n(symbol=SYM, frequency='1d', count=5,
                       end_time='2026-09-15 15:00:00', fields='eob,close,iopv',
                       adjust=gma.ADJUST_PREV, df=True)
    probe['probes']['history_1d_iopv'] = {
        'ok': True, 'columns': list(df.columns),
        'sample': df.astype(str).to_dict('records')[:3],
    }
except Exception as e:
    probe['probes']['history_1d_iopv'] = {'ok': False, 'error': repr(e)[:300]}

# 3) get_instrumentinfos 字段清单
try:
    info = gma.get_instrumentinfos(symbols=SYM, df=True)
    probe['probes']['get_instrumentinfos'] = {
        'ok': True, 'columns': list(info.columns),
        'row': {k: str(v)[:100] for k, v in info.iloc[0].to_dict().items()} if len(info) else {},
    }
except Exception as e:
    probe['probes']['get_instrumentinfos'] = {'ok': False, 'error': repr(e)[:300]}

json.dump(probe, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
print('saved:', OUT)
cur = probe['probes'].get('current', {})
if cur.get('ok') and cur.get('sample'):
    print('current sample:', json.dumps(cur['sample'][0], ensure_ascii=False))
