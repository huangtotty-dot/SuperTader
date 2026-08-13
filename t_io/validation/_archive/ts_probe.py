# -*- coding: utf-8 -*-
"""ts_probe.py — 探测 tushare stk_mins 可用性与可回溯窗口（ASCII 输出）"""
import sys
try:
    import tushare as ts
except ImportError:
    print("NO_TUSHARE")
    sys.exit(1)
TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
ts.set_token(TOKEN)
pro = ts.pro_api()

def probe(code, start, end, freq="1min"):
    try:
        df = pro.stk_mins(ts_code=code, freq=freq, start_date=start, end_date=end)
        if df is None or df.empty:
            return 0, None, None
        return len(df), str(df["trade_time"].min()), str(df["trade_time"].max())
    except Exception as e:
        return -1, None, repr(e)[:150]

# 探测不同月份的可达性
for label, s, e in [
    ("2026-03 中旬", "2026-03-16 09:00:00", "2026-03-20 19:00:00"),
    ("2026-04 中旬", "2026-04-13 09:00:00", "2026-04-17 19:00:00"),
    ("2026-05 中旬", "2026-05-11 09:00:00", "2026-05-15 19:00:00"),
    ("2026-06 中旬", "2026-06-15 09:00:00", "2026-06-19 19:00:00"),
]:
    n, tmin, tmax = probe("000988.SZ", s, e)
    print(f"{label}: rows={n} min={tmin} max={tmax}")

# 单次调用行数上限探测（长区间）
n, tmin, tmax = probe("000988.SZ", "2026-05-01 09:00:00", "2026-06-20 19:00:00")
print(f"长区间单次: rows={n} min={tmin} max={tmax}")
print("columns sample:", end=" ")
try:
    df = pro.stk_mins(ts_code="000988.SZ", freq="1min", start_date="2026-06-15 09:00:00", end_date="2026-06-15 19:00:00")
    print(list(df.columns))
    print(df.head(2).to_string())
except Exception as e:
    print(repr(e)[:200])
