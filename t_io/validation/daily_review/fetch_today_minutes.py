# -*- coding: utf-8 -*-
"""fetch_today_minutes.py — 拉取当日 5 股 1 分钟 K(供日复盘日型判定, 口径同 minute_snapshots_ts)
用法(用户python, 带tushare): python fetch_today_minutes.py [--date 2026-08-03]
产物: t_io/validation/daily_review/minutes_DATE.json {code: {date, bars:[{time,open,high,low,close,volume}]}}
"""
import argparse, json, sys
from pathlib import Path

BASE = Path(r"E:\06_T")
CODES = {"000988": "000988.SZ", "588170": "588170.SH", "600176": "600176.SH",
         "600481": "600481.SH", "603667": "603667.SH"}
TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"

p = argparse.ArgumentParser()
p.add_argument("--date", default="2026-08-03")
DATE = p.parse_args().date

try:
    import tushare as ts
except ImportError:
    print("NO_TUSHARE")
    sys.exit(1)
ts.set_token(TOKEN)
pro = ts.pro_api()

out = {}
for code, tsc in CODES.items():
    try:
        df = pro.stk_mins(ts_code=tsc, freq="1min",
                          start_date=f"{DATE} 09:00:00", end_date=f"{DATE} 15:30:00")
        if df is None or df.empty:
            print(f"{code}: EMPTY")
            continue
        df = df.sort_values("trade_time")
        bars = [{"time": str(r["trade_time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r.get("vol", 0) or 0)} for _, r in df.iterrows()]
        out[code] = {"date": DATE, "bars": bars}
        print(f"{code}: {len(bars)} bars {bars[0]['time'][11:16]}~{bars[-1]['time'][11:16]}")
    except Exception as e:
        print(f"{code}: ERROR {repr(e)[:120]}")

fp = BASE / f"t_io/validation/daily_review/minutes_{DATE}.json"
json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
print("written:", fp)
