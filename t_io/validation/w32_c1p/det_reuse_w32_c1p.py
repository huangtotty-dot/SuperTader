# -*- coding: utf-8 -*-
"""
det_reuse_w32_c1p.py — C1' ctl 复用判定：cap 改动默认关隔离度校验
新代码（含 T_BUY_DAILY_CAP 注入点）下重跑 4 个 ctl 块（T_BUY_DAILY_CAP 不注入），
与 W32 ctl parts 逐字节比对 → 全一致则 ctl 复用（判定理由写入 C1P_FINAL.md）。
选块覆盖：破闸段(000988_20260323 含 03-25) + 跨代码/跨时段 3 块（W32 det 同款）。
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
ROOT = BASE / "t_io/validation/w32_c1p"
DET = ROOT / "det_reuse"
HOLDINGS_SNAP = W32 / "holdings_snapshot_3d80810.json"
PY = sys.executable
BLOCKS = [("000988", "2026-03-23", "2026-03-27"),   # 含破闸日 2026-03-25（cap 最相关段）
          ("600481", "2026-06-22", "2026-06-26"),
          ("588170", "2026-05-11", "2026-05-15"),
          ("603667", "2026-07-13", "2026-07-17")]
BASE_ENV = {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36",
            "T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
            "T_SNAPSHOT_DIR": str(BASE / "t_io/minute_snapshots_ts")}
# 注意：不注入 T_BUYBACK_BYPASS_GATES / T_BUY_DAILY_CAP —— ctl 口径与 W32 完全一致

def md5(fp):
    return hashlib.md5(open(fp, "rb").read()).hexdigest()

def run_block(code, s, e):
    od = DET / f"ctl_{code}_{s.replace('-', '')}"
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(BASE_ENV)
    r = subprocess.run([PY, str(BASE / "harness_backtest.py"), "--codes", code,
                        "--start", s, "--end", e, "--ab", "v102", "--out", str(od)],
                       capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)
    return od, r.returncode

def main():
    DET.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda b: run_block(*b), BLOCKS))
    all_ok = True
    for (code, s, e), (od, rc) in zip(BLOCKS, results):
        ref = W32 / "parts" / f"ctl_{code}_{s.replace('-', '')}"
        ok_block = True
        ref_files = sorted(ref.glob("*.jsonl")) + sorted(ref.glob("*.json"))
        for p in ref_files:
            q = od / p.name
            same = q.exists() and md5(p) == md5(q)
            ok_block &= same
            if not same:
                print(f"  ✗ ctl {code} {s}: {p.name} 不一致")
        # 反向：新产物不得多出文件（capped_buys 仅 cap 开启才写）
        extra = [q.name for q in od.glob("*") if not (ref / q.name).exists()]
        if extra:
            ok_block = False
            print(f"  ✗ ctl {code} {s}: 多出文件 {extra}")
        print(("✓" if ok_block else "✗"),
              f"ctl {code} {s}~{e} 逐字节一致" if ok_block else f"ctl {code} {s} 存在不一致")
        all_ok &= ok_block
    print("CTL_REUSE:", "PASS（可复用 W32 ctl 产物）" if all_ok else "FAIL（需 ctl 全量重跑）")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
