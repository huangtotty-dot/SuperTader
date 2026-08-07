# -*- coding: utf-8 -*-
"""smoke_recheck_603667.py — 注入 T36b 同款 env 后复验 603667 块逐字节一致"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
od = BASE / "t_io/validation/w32_c1/smoke/ctl_603667_20260406"
env = dict(os.environ)
env["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")
env["T_HOLDINGS_FILE"] = str(BASE / "t_io/validation/w32_c1/holdings_snapshot_3d80810.json")
env["T_BUY_BONUS_MIN_SCORE"] = "36"
env["T_NOTIFY_BUY"] = "36"
for junk in od.glob("*") if od.exists() else []:
    junk.unlink()
od.mkdir(parents=True, exist_ok=True)
r = subprocess.run([sys.executable, str(BASE / "harness_backtest.py"), "--codes", "603667",
                    "--start", "2026-04-06", "--end", "2026-04-10", "--ab", "v102",
                    "--out", str(od)], capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)
print("rc:", r.returncode)
def md5(f):
    return hashlib.md5(open(f, "rb").read()).hexdigest()
ref = BASE / "t_io/validation/e1_final/parts/T36b_603667_20260406"
ok = True
for p in sorted(ref.glob("*.jsonl")):
    same = md5(p) == md5(od / p.name)
    ok &= same
    print("✓" if same else "✗", p.name)
print("cores:", os.cpu_count())
print("RECHECK:", "PASS" if ok else "FAIL")
