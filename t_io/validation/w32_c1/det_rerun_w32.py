# -*- coding: utf-8 -*-
"""
det_rerun_w32.py — W32 §4 确定性双跑校验：重跑 4 块（2 ctl + 2 C1）与既有 parts 逐字节比对
"""
import hashlib
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/w32_c1"
DET = ROOT / "det2"
HOLDINGS_SNAP = ROOT / "holdings_snapshot_3d80810.json"
PY = sys.executable
BLOCKS = [("ctl", "000988", "2026-04-13", "2026-04-17"),
          ("ctl", "600481", "2026-06-22", "2026-06-26"),
          ("C1", "588170", "2026-05-11", "2026-05-15"),
          ("C1", "603667", "2026-07-13", "2026-07-17")]
BASE_ENV = {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36",
            "T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
            "T_SNAPSHOT_DIR": str(BASE / "t_io/minute_snapshots_ts")}

def md5(fp):
    return hashlib.md5(open(fp, "rb").read()).hexdigest()

def run_block(g, code, s, e):
    od = DET / f"{g}_{code}_{s.replace('-', '')}"
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(BASE_ENV)
    if g == "C1":
        env["T_BUYBACK_BYPASS_GATES"] = "1"
    r = subprocess.run([PY, str(BASE / "harness_backtest.py"), "--codes", code,
                        "--start", s, "--end", e, "--ab", "v102", "--out", str(od)],
                       capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)
    return od, r.returncode

def main():
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda b: run_block(*b), BLOCKS))
    all_ok = True
    for (g, code, s, e), (od, rc) in zip(BLOCKS, results):
        ref = ROOT / "parts" / f"{g}_{code}_{s.replace('-', '')}"
        ok_block = True
        for p in sorted(ref.glob("*.jsonl")):
            q = od / p.name
            same = q.exists() and md5(p) == md5(q)
            ok_block &= same
            if not same:
                print(f"  ✗ {g} {code} {s}: {p.name} 不一致")
        print(("✓" if ok_block else "✗"), f"{g} {code} {s}~{e} 逐字节一致" if ok_block else f"{g} {code} {s} 存在不一致")
        all_ok &= ok_block
    print("DETERMINISM:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
