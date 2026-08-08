# -*- coding: utf-8 -*-
"""compare_v120.py — V1.2.0 冒烟三块与决赛参照逐字节比对（独立比较器，免重跑 harness）"""
import hashlib
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
CASES = [
    ("A_prod_000988 vs C1p_000988_20260323", BASE / "t_io/validation/v120_smoke/A_prod_000988",
     BASE / "t_io/validation/w32_c1p/parts/C1p_000988_20260323"),
    ("A_prod_588170 vs C1p_588170_20260511", BASE / "t_io/validation/v120_smoke/A_prod_588170",
     BASE / "t_io/validation/w32_c1p/parts/C1p_588170_20260511"),
    ("B_compat_600481 vs ctl_600481_20260622", BASE / "t_io/validation/v120_smoke/B_compat_600481",
     BASE / "t_io/validation/w32_c1/parts/ctl_600481_20260622"),
]

def md5(fp):
    return hashlib.md5(fp.read_bytes()).hexdigest()

def main():
    all_ok = True
    for name, od, ref in CASES:
        ok = True
        for p in sorted(ref.glob("*.jsonl")) + sorted(ref.glob("*.json")):
            q = od / p.name
            same = q.exists() and md5(p) == md5(q)
            ok &= same
            if not same:
                print("  DIFF", name, p.name)
        extra = [q.name for q in od.glob("*") if not (ref / q.name).exists()]
        missing = [p.name for p in ref.glob("*") if not (od / p.name).exists()]
        if extra:
            ok = False
            print("  EXTRA", name, extra)
        if missing:
            ok = False
            print("  MISSING", name, missing)
        print(("PASS" if ok else "FAIL"), name)
        all_ok &= ok
    print("V120_SMOKE_COMPARE:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
