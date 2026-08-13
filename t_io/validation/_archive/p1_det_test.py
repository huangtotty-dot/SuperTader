# -*- coding: utf-8 -*-
"""p1_det_test.py — 用 harness 实际 compute_p1_metrics 测平局势能稳定性（跨进程 hash seed 不同）"""
import json, sys
from pathlib import Path
BASE = Path(r"E:\06_T")
sys.path.insert(0, str(BASE))
import harness_backtest as hb

tls = {}
for line in open(BASE / "t_io/validation/v106/trend_timeline_v102.jsonl", encoding="utf-8"):
    r = json.loads(line)
    tls[r["key"]] = r["timeline"]

day_bars_cache = {}
dates = sorted({k.split(":")[0] for k in tls})
for d in dates:
    day_bars = {}
    for k in tls:
        dd, code = k.split(":")
        if dd == d:
            df = hb.load_snapshots(code, d)
            if not df.empty:
                day_bars[code] = df
    day_bars_cache[d] = day_bars

m = hb.compute_p1_metrics(tls, day_bars_cache)
print(f"overall_consistency={m['overall_consistency']} bull={m['bull_consistency']} bear={m['bear_consistency']} neutral={m['neutral_ratio']} bias={m['bias_ratio']}")
