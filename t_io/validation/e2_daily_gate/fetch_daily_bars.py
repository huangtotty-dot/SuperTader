# -*- coding: utf-8 -*-
"""fetch_daily_bars.py — E2: 拉取 5 股 Tushare 日线(含样本前预热期) -> e2_daily_gate/daily_bars_ts/
用用户 python(有 tushare): C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe
"""
import sys, time
from pathlib import Path
import tushare as ts

TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
OUT = Path(r"E:\06_T\t_io\validation\e2_daily_gate\daily_bars_ts")
OUT.mkdir(parents=True, exist_ok=True)
CODES = {"000988.SZ": "daily", "600176.SH": "daily", "600481.SH": "daily",
         "603667.SH": "daily", "588170.SH": "fund_daily"}  # 588170=ETF
START, END = "20250901", "20260724"   # 预热 ~140 交易日(MA60+slope 足够) + 样本期
ts.set_token(TOKEN)
pro = ts.pro_api()
for code, api in CODES.items():
    fp = OUT / f"{code.replace('.', '_')}.csv"
    if fp.exists():
        print("SKIP", code); continue
    fn = getattr(pro, api)
    df = fn(ts_code=code, start_date=START, end_date=END)
    df = df.sort_values("trade_date")
    df.to_csv(fp, index=False)
    print("OK", code, len(df), df.iloc[0]["trade_date"], "->", df.iloc[-1]["trade_date"])
    time.sleep(0.5)
print("DONE")
