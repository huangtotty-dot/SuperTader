# -*- coding: utf-8 -*-
"""快照 vs gm 日线成交额抽验（2026-09-10/11/14/15），确认快照 amount=元。"""
import sys, os, json
sys.path.insert(0, r'E:\superTrader')
sys.stdout.reconfigure(encoding='utf-8')
import gm.api as gma

gma.set_token(os.environ['GM_TOKEN'])
df = gma.history_n(symbol='SHSE.588170', frequency='1d', count=5,
                   end_time='2026-09-15 15:00:00', fields='eob,close,volume,amount',
                   adjust=gma.ADJUST_NONE, df=True)
rows = [(str(r['eob'])[:10], float(r['close']), float(r['volume']), float(r['amount']))
        for _, r in df.iterrows()]
for r in rows:
    print(r)
json.dump(rows, open(r'E:\superTrader\t_io\validation\etf_carrier\data\gm_daily_xcheck.json',
                     'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved')
