# -*- coding: utf-8 -*-
"""diff_probe_v120.py — V1.2.0 冒烟差异定位：行数对比 + 首处差异打印"""
from pathlib import Path

BASE = Path(r"E:\06_T")
PAIRS = [
    ("sig_000988", BASE / "t_io/validation/v120_smoke/A_prod_000988/signals_v102.jsonl",
     BASE / "t_io/validation/w32_c1p/parts/C1p_000988_20260323/signals_v102.jsonl"),
    ("capped_000988", BASE / "t_io/validation/v120_smoke/A_prod_000988/capped_buys_v102.jsonl",
     BASE / "t_io/validation/w32_c1p/parts/C1p_000988_20260323/capped_buys_v102.jsonl"),
    ("trace_0325", BASE / "t_io/validation/v120_smoke/A_prod_000988/decision_trace_2026-03-25.jsonl",
     BASE / "t_io/validation/w32_c1p/parts/C1p_000988_20260323/decision_trace_2026-03-25.jsonl"),
    ("trace_600481_0622", BASE / "t_io/validation/v120_smoke/B_compat_600481/decision_trace_2026-06-22.jsonl",
     BASE / "t_io/validation/w32_c1/parts/ctl_600481_20260622/decision_trace_2026-06-22.jsonl"),
]

for label, a, b in PAIRS:
    la = a.read_text(encoding="utf-8").splitlines()
    lb = b.read_text(encoding="utf-8").splitlines()
    print(label, "lines", len(la), "vs", len(lb))
    for i, (x, y) in enumerate(zip(la, lb)):
        if x != y:
            print("  first diff at line", i)
            print("   A:", x[:260])
            print("   B:", y[:260])
            break
    else:
        if len(la) != len(lb):
            print("  prefix identical, length differs")
