# -*- coding: utf-8 -*-
"""
ts_fetch_minutes.py — 用 Tushare stk_mins 补齐 2026-03-16~07-24 缺口
只写快照缺失的 stock-day，绝不覆盖已有文件。ASCII 输出。
格式: t_io/minute_snapshots/YYYY/MM/{code}_{date}.json {"code","date","source","bars":[asc]}
"""
import json, os, sys, time
from datetime import datetime
from pathlib import Path

BASE = Path(r"E:\06_T")
SNAP = BASE / "t_io/minute_snapshots"
LOG = BASE / "t_io/validation/ts_fetch_log.txt"

CODES = {"000988": "000988.SZ", "588170": "588170.SH", "600176": "600176.SH",
         "600481": "600481.SH", "603667": "603667.SH"}
CHUNKS = [("2026-03-16", "2026-04-15"), ("2026-04-16", "2026-05-15"),
          ("2026-05-16", "2026-06-19"), ("2026-06-20", "2026-07-24")]

lines = []
def out(s):
    lines.append(s); print(s)

def existing_days(code):
    days = set()
    ydir = SNAP / "2026"
    if not ydir.exists():
        return days
    for m in os.listdir(ydir):
        mdir = ydir / m
        if not mdir.is_dir():
            continue
        for f in os.listdir(mdir):
            if f.startswith(code + "_") and f.endswith(".json"):
                days.add(f[len(code) + 1:-5])
    return days

def main():
    try:
        import tushare as ts
    except ImportError:
        out("FATAL: no tushare"); return 1
    ts.set_token("9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def")
    pro = ts.pro_api()

    total_new = 0
    for code, ts_code in CODES.items():
        have = existing_days(code)
        new_days = 0
        for cs, ce in CHUNKS:
            try:
                df = pro.stk_mins(ts_code=ts_code, freq="1min",
                                  start_date=cs + " 09:00:00", end_date=ce + " 19:00:00")
            except Exception as e:
                out(f"{code} {cs}~{ce}: FETCH_ERROR {repr(e)[:120]}")
                time.sleep(5)
                continue
            if df is None or df.empty:
                out(f"{code} {cs}~{ce}: empty")
                time.sleep(1.5)
                continue
            df = df.sort_values("trade_time")
            df["d"] = df["trade_time"].str[:10]
            for d, g in df.groupby("d"):
                if d in have:
                    continue  # 已有快照，不覆盖
                bars = []
                for _, r in g.iterrows():
                    bars.append({"time": r["trade_time"],
                                 "open": float(r["open"]), "high": float(r["high"]),
                                 "low": float(r["low"]), "close": float(r["close"]),
                                 "volume": float(r["vol"]), "amount": float(r["amount"])})
                dt = datetime.strptime(d, "%Y-%m-%d")
                od = SNAP / "2026" / f"{dt.month:02d}"
                od.mkdir(parents=True, exist_ok=True)
                fp = od / f"{code}_{d}.json"
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump({"code": code, "date": d, "source": "tushare_stk_mins",
                               "bars": bars}, f, ensure_ascii=False)
                new_days += 1
            out(f"{code} {cs}~{ce}: rows={len(df)} new_days_so_far={new_days}")
            time.sleep(1.5)
        total_new += new_days
        out(f"{code}: +{new_days} days written")
    out(f"TOTAL new stock-days: {total_new}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return 0

if __name__ == "__main__":
    sys.exit(main())
