# -*- coding: utf-8 -*-
"""拆分因子核实：gm 日线 raw vs 前复权 对照（2026-07 拆分段）。只读探测。"""
import sys, os, json
sys.path.insert(0, r'E:\superTrader')
sys.stdout.reconfigure(encoding='utf-8')
import gm.api as gma

token = os.environ.get('GM_TOKEN')
gma.set_token(token)
out = {}
for adj, name in ((gma.ADJUST_NONE, 'raw'), (gma.ADJUST_PREV, 'prev_adj')):
    try:
        df = gma.history_n(symbol='SHSE.588170', frequency='1d', count=12,
                           end_time='2026-07-10 15:00:00', fields='eob,close',
                           adjust=adj, df=True)
        out[name] = {'ok': True,
                     'rows': [(str(t)[:10], float(c)) for t, c in zip(df['eob'], df['close'])]}
        print(name, out[name]['rows'])
    except Exception as e:
        out[name] = {'ok': False, 'error': repr(e)[:300]}
        print(name, 'ERR', out[name]['error'])
json.dump(out, open(r'E:\superTrader\t_io\validation\etf_carrier\data\split_factor_probe.json',
                    'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved split_factor_probe.json')
