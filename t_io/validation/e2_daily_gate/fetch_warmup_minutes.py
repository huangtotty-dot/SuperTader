# -*- coding: utf-8 -*-
"""fetch_warmup_minutes.py — E2: 拉取样本前预热期(2025-11-10~2026-03-13)1分钟K -> minute_snapshots_pre/
格式与 minute_snapshots_ts 完全一致; 幂等(已存在跳过); 需用户 python(tushare)。仅用于 MA 预热聚合日线。
"""
import json, sys, time
from datetime import datetime, timedelta
from pathlib import Path
import tushare as ts

BASE = Path(r"E:\06_T")
OUT = BASE / "t_io/validation/e2_daily_gate/minute_snapshots_pre"
CODES = {"000988": "000988.SZ", "588170": "588170.SH", "600176": "600176.SH",
         "600481": "600481.SH", "603667": "603667.SH"}
D0, D1 = "2025-11-10", "2026-03-13"
ts.set_token("9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def")
pro = ts.pro_api()

def target(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return OUT / str(dt.year) / f"{dt.month:02d}"

chunks = []
cur = datetime.strptime(D0, "%Y-%m-%d")
end_dt = datetime.strptime(D1, "%Y-%m-%d")
while cur <= end_dt:
    ce = min(cur + timedelta(days=24), end_dt)
    chunks.append((cur.strftime("%Y-%m-%d"), ce.strftime("%Y-%m-%d")))
    cur = ce + timedelta(days=1)

for code, ts_code in CODES.items():
    nw = 0
    for cs, ce in chunks:
        try:
            df = pro.stk_mins(ts_code=ts_code, freq="1min",
                              start_date=cs + " 09:00:00", end_date=ce + " 19:00:00")
        except Exception as e:
            print(f"{code} {cs}~{ce}: FETCH_ERROR {repr(e)[:100]}", flush=True); time.sleep(5); continue
        if df is None or df.empty:
            print(f"{code} {cs}~{ce}: EMPTY", flush=True); time.sleep(2); continue
        df = df.sort_values("trade_time")
        df["d"] = df["trade_time"].str[:10]
        for d, g in df.groupby("d"):
            tf = target(d) / f"{code}_{d}.json"
            if tf.exists():
                continue
            bars = [{"time": r["trade_time"], "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(r["close"]),
                     "volume": float(r["vol"]), "amount": float(r["amount"])} for _, r in g.iterrows()]
            tf.parent.mkdir(parents=True, exist_ok=True)
            with open(tf, "w", encoding="utf-8") as f:
                json.dump({"code": code, "date": d, "source": "tushare_stk_mins", "bars": bars},
                          f, ensure_ascii=False)
            nw += 1
        print(f"{code} {cs}~{ce}: rows={len(df)} written_so_far={nw}", flush=True)
        time.sleep(2)
    print(f"{code}: warmup written={nw}", flush=True)
print("DONE")
