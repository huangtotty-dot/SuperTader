# -*- coding: utf-8 -*-
"""
smoke_w32_c1p.py — C1' 口径B 冒烟（跑全管线前必过）
块：000988_20260323（含破闸日 2026-03-25，离线预期 cap 第 8 条 14:37 VOID 42.1）
    600481_20260622（无破闸块 → 与 W32 C1 同块应逐字节一致，验证 cap 未命中时零干扰）
校验：① C1p 双跑逐字节一致（确定性）
      ② 000988 2026-03-25 已记录买信号 ≤7 且 capped_buys 含当日 capped_rank=8 的尾盘条目
      ③ 无破闸块与 W32 C1 parts 逐字节一致
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
W32 = BASE / "t_io/validation/w32_c1"
ROOT = BASE / "t_io/validation/w32_c1p"
SMOKE = ROOT / "smoke"
HOLDINGS_SNAP = W32 / "holdings_snapshot_3d80810.json"
PY = sys.executable
BLOCKS = [("000988", "2026-03-23", "2026-03-27"),
          ("600481", "2026-06-22", "2026-06-26")]
C1P_ENV = {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36",
           "T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
           "T_SNAPSHOT_DIR": str(BASE / "t_io/minute_snapshots_ts"),
           "T_BUYBACK_BYPASS_GATES": "1", "T_BUY_DAILY_CAP": "7"}

def md5(fp):
    return hashlib.md5(open(fp, "rb").read()).hexdigest()

def run_block(code, s, e, tag):
    od = SMOKE / f"{tag}_{code}_{s.replace('-', '')}"
    if od.exists():
        shutil.rmtree(od)
    od.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(C1P_ENV)
    subprocess.run([PY, str(BASE / "harness_backtest.py"), "--codes", code,
                    "--start", s, "--end", e, "--ab", "v102", "--out", str(od)],
                   capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)
    return od

def load_jsonl(fp):
    return [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()] if fp.exists() else []

def main():
    SMOKE.mkdir(parents=True, exist_ok=True)
    # 双跑
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = []
        for tag in ("r1", "r2"):
            for b in BLOCKS:
                futs.append((tag, b, ex.submit(run_block, *b, tag)))
        dirs = {(tag, b[0]): f.result() for tag, b, f in futs}

    ok = True
    # ① 确定性：双跑逐字节
    for code, s, e in BLOCKS:
        d1, d2 = dirs[("r1", code)], dirs[("r2", code)]
        f1 = sorted(p.name for p in d1.glob("*"))
        f2 = sorted(p.name for p in d2.glob("*"))
        same = f1 == f2 and all(md5(d1 / n) == md5(d2 / n) for n in f1)
        ok &= same
        print(("✓" if same else "✗"), f"C1p {code} {s} 双跑逐字节一致")
        if not same:
            for n in set(f1) | set(f2):
                p, q = d1 / n, d2 / n
                if not (p.exists() and q.exists() and md5(p) == md5(q)):
                    print(f"    差异: {n}")

    # ② cap 生效：000988 2026-03-25
    d = dirs[("r1", "000988")]
    sigs = load_jsonl(d / "signals_v102.jsonl")
    capped = load_jsonl(d / "capped_buys_v102.jsonl")
    buys_325 = [x for x in sigs if x["action"] == "BUY_LOW" and x["ts"].startswith("2026-03-25")]
    capped_325 = [x for x in capped if x["ts"].startswith("2026-03-25")]
    cond1 = len(buys_325) <= 7
    cond2 = len(capped_325) >= 1 and all(x["capped_rank"] == 8 for x in capped_325)
    ok &= cond1 and cond2
    print(("✓" if cond1 else "✗"), f"000988 2026-03-25 记录买信号 {len(buys_325)} 条（≤7）")
    print(("✓" if cond2 else "✗"), f"000988 2026-03-25 被 cap {len(capped_325)} 条 capped_rank=8")
    for x in capped_325:
        print(f"    capped: {x['ts']} score={x['buy_score']} settle={x['settle_result']} rank={x['capped_rank']}")
    # 被 cap 的是尾盘条目（在已记录 7 条之后）
    if buys_325 and capped_325:
        tail_ok = all(x["ts"] > max(b["ts"] for b in buys_325) for x in capped_325)
        ok &= tail_ok
        print(("✓" if tail_ok else "✗"), "被 cap 信号时间序晚于当日已记录 7 条（尾盘第 8 条）")

    # ③ 无破闸块与 W32 C1 同块逐字节一致
    ref = W32 / "parts" / "C1_600481_20260622"
    d_new = dirs[("r1", "600481")]
    ref_files = sorted(ref.glob("*.jsonl")) + sorted(ref.glob("*.json"))
    same3 = all((d_new / p.name).exists() and md5(p) == md5(d_new / p.name) for p in ref_files)
    extra = [q.name for q in d_new.glob("*") if not (ref / q.name).exists()]
    ok &= same3 and not extra
    print(("✓" if same3 and not extra else "✗"),
          f"600481 2026-06-22 与 W32 C1 同块逐字节一致（cap 未命中零干扰） extra={extra}")

    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
