# -*- coding: utf-8 -*-
"""
smoke_v120_production.py — V1.2.0 上线闸门：生产路径等价性冒烟
A. 生产口径（零行为 env 注入，仅数据 fixture：T_HOLDINGS_FILE/T_SNAPSHOT_DIR）跑 2 块：
   000988_20260323（含破闸日 03-25）+ 588170_20260511
   → 与 w32_c1p/parts/C1p_* 同块逐字节一致 = 生产默认 == C1' 决赛行为
B. 向后兼容：T_BUYBACK_BYPASS_GATES=0 + T_BUY_DAILY_CAP=0（+T36b env，W32 ctl 同款）跑 1 块
   600481_20260622 → 与 w32_c1/parts/ctl_600481_20260622 逐字节一致 = 开关可复现旧世界
"""
import hashlib
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
W32 = BASE / "t_io/validation/w32_c1"
C1P = BASE / "t_io/validation/w32_c1p"
SMOKE = BASE / "t_io/validation/v120_smoke"
HOLDINGS_SNAP = C1P / "holdings_snapshot_3d80810.json"
PY = sys.executable

DATA_ENV = {"T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
            "T_SNAPSHOT_DIR": str(BASE / "t_io/minute_snapshots_ts")}
T36B_ENV = {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36"}

CASES = [
    ("A_prod_000988", "000988", "2026-03-23", "2026-03-27", dict(DATA_ENV),
     C1P / "parts/C1p_000988_20260323"),
    ("A_prod_588170", "588170", "2026-05-11", "2026-05-15", dict(DATA_ENV),
     C1P / "parts/C1p_588170_20260511"),
    ("B_compat_600481", "600481", "2026-06-22", "2026-06-26",
     {**DATA_ENV, **T36B_ENV, "T_BUYBACK_BYPASS_GATES": "0", "T_BUY_DAILY_CAP": "0"},
     W32 / "parts/ctl_600481_20260622"),
]

def md5(fp):
    return hashlib.md5(open(fp, "rb").read()).hexdigest()

def run_case(name, code, s, e, extra_env, ref):
    od = SMOKE / name
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(extra_env)
    r = subprocess.run([PY, str(BASE / "harness_backtest.py"), "--codes", code,
                        "--start", s, "--end", e, "--ab", "v102", "--out", str(od)],
                       capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)
    return name, od, ref, r.returncode

def main():
    SMOKE.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda c: run_case(*c), CASES))
    all_ok = True
    for name, od, ref, rc in results:
        ok = True
        ref_files = sorted(ref.glob("*.jsonl")) + sorted(ref.glob("*.json"))
        for p in ref_files:
            q = od / p.name
            same = q.exists() and md5(p) == md5(q)
            ok &= same
            if not same:
                print(f"  ✗ {name}: {p.name} 不一致")
        extra = [q.name for q in od.glob("*") if not (ref / q.name).exists()]
        if extra:
            ok = False
            print(f"  ✗ {name}: 多出文件 {extra}")
        print(("✓" if ok else "✗"), f"{name} 与参照块逐字节一致" if ok else f"{name} 存在不一致")
        all_ok &= ok
    print("V120_SMOKE:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
