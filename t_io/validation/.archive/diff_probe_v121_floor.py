# -*- coding: utf-8 -*-
"""diff_probe_v121_floor.py — ON 块 trace 分歧定性：NaN→null 序列化差 or 行为差"""
import json
from pathlib import Path

REF = Path(r"E:\06_T\t_io\validation\w32_c1p\parts\C1p_588170_20260511")
ON = Path(r"E:\06_T\t_io\validation\v121_smoke\engine_floor_on_588170")


def norm(o):
    if isinstance(o, dict):
        return {k: norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [norm(v) for v in o]
    if isinstance(o, float) and o != o:
        return None
    return o


all_sem = True
for d in ("2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"):
    a = (REF / f"decision_trace_{d}.jsonl").read_text(encoding="utf-8").splitlines()
    b = (ON / f"decision_trace_{d}.jsonl").read_text(encoding="utf-8").splitlines()
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    sem = all(norm(json.loads(a[i])) == norm(json.loads(b[i])) for i in diffs)
    all_sem &= sem and len(a) == len(b)
    print(f"{d}: 行数 {len(a)}/{len(b)} 字节分歧 {len(diffs)} 行，语义相等={sem}")
    if diffs and sem:
        x, y = a[diffs[0]], b[diffs[0]]
        for i, (cx, cy) in enumerate(zip(x, y)):
            if cx != cy:
                print(f"   样本 L{diffs[0]+1}@{i}: ref=...{x[max(0,i-25):i+25]}...  new=...{y[max(0,i-25):i+25]}...")
                break
print("ON_TRACE_SEMANTIC:", "PASS" if all_sem else "FAIL")
