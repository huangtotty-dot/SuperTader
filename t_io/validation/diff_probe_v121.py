# -*- coding: utf-8 -*-
"""diff_probe_v121.py — 定位 V1.2.1 冒烟 trace 首个分歧点"""
import json
from pathlib import Path

ref = Path(r"E:\06_T\t_io\validation\w32_c1p\parts\C1p_600481_20260622")
out = Path(r"E:\06_T\t_io\validation\v121_smoke\unfreeze_600481")
for d in ("2026-06-22", "2026-06-23", "2026-06-24"):
    a = (ref / f"decision_trace_{d}.jsonl").read_text(encoding="utf-8").splitlines()
    b = (out / f"decision_trace_{d}.jsonl").read_text(encoding="utf-8").splitlines()
    print(d, "ref", len(a), "new", len(b))
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            rx, ry = json.loads(x), json.loads(y)
            keys = [k for k in rx if rx.get(k) != ry.get(k)]
            print(f"  首个分歧 L{i+1} {rx['scan_time']} 差异键={keys}")
            for k in keys[:6]:
                print(f"    ref.{k} = {json.dumps(rx.get(k), ensure_ascii=False)[:140]}")
                print(f"    new.{k} = {json.dumps(ry.get(k), ensure_ascii=False)[:140]}")
            break
    else:
        if len(a) != len(b):
            print("  前缀一致，行数差", len(a), len(b))
print("--- ref signals:")
for l in (ref / "signals_v102.jsonl").read_text(encoding="utf-8").splitlines():
    print(l[:220])
print("--- new signals:")
for l in (out / "signals_v102.jsonl").read_text(encoding="utf-8").splitlines():
    print(l[:220])
