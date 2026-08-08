# -*- coding: utf-8 -*-
"""
run_w32_c1p.py — C1' 口径B 全管线决赛驱动（仿 run_w32_c1.py，幂等可续）
单组：C1p = ctl 同款 env + T_BUYBACK_BYPASS_GATES=1 + T_BUY_DAILY_CAP=7
      （C1 接回解耦保留 + 全部买信号单股日限 7 内置于状态机，用户 2026-08-08 拍板口径 B）
ctl 复用 W32 产物（det_reuse_w32_c1p.py 4 块逐字节 PASS 留证，含破闸段 000988_20260323）。
用法: python run_w32_c1p.py  （断点重入同命令；BUDGET=260s 派发截止，配 Bash 300s 上限）
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/w32_c1p"
LOG = ROOT / "progress.log"
HOLDINGS_SNAP = ROOT / "holdings_snapshot_3d80810.json"
sys.path.insert(0, str(BASE / "t_io/validation"))
from run_ab_expanded import SEGS  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
GROUPS = {"C1p": {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36",
                  "T_HOLDINGS_FILE": str(HOLDINGS_SNAP),
                  "T_BUYBACK_BYPASS_GATES": "1", "T_BUY_DAILY_CAP": "7"}}
BUDGET = 200
WORKERS = 7
PY = sys.executable


def tasks():
    for g in GROUPS:
        for code in CODES:
            for s, e in SEGS:
                yield (g, code, s, e)


def part_dir(g, code, s):
    return ROOT / "parts" / f"{g}_{code}_{s.replace('-', '')}"


def log(msg):
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


def run_one(t):
    g, code, s, e = t
    od = part_dir(g, code, s)
    od.mkdir(parents=True, exist_ok=True)
    sig = od / "signals_v102.jsonl"
    env = dict(os.environ)
    env["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")
    env.update(GROUPS[g])

    def _run():
        return subprocess.run(
            [PY, str(BASE / "harness_backtest.py"), "--codes", code,
             "--start", s, "--end", e, "--ab", "v102", "--out", str(od)],
            capture_output=True, text=True, timeout=900, cwd=str(BASE), env=env)

    t0 = time.time()
    try:
        _run()
    except subprocess.TimeoutExpired:
        return t, "timeout", time.time() - t0

    def _valid():
        if not sig.exists():
            return False
        n = sum(1 for l in open(sig, encoding="utf-8") if l.strip())
        return n > 0 or (od / "summary_v102.json").exists()

    if _valid():
        n = sum(1 for l in open(sig, encoding="utf-8") if l.strip())
        if n == 0:
            for junk in od.glob("*"):
                junk.unlink()
            try:
                _run()
            except subprocess.TimeoutExpired:
                return t, "timeout", time.time() - t0
            return (t, "ok_retry_empty", time.time() - t0) if sig.exists() else (t, "fail_no_signals", time.time() - t0)
        return t, "ok", time.time() - t0
    for junk in od.glob("*"):
        junk.unlink()
    try:
        _run()
    except subprocess.TimeoutExpired:
        return t, "timeout", time.time() - t0
    return (t, "ok_retry", time.time() - t0) if sig.exists() else (t, "fail_no_signals", time.time() - t0)


def main():
    todo = [t for t in tasks() if not (part_dir(t[0], t[1], t[2]) / "signals_v102.jsonl").exists()]
    log(f"START W32_C1P remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
    if not todo:
        log("NOTHING_TODO")
        return 0
    t_start = time.time()
    pending = list(todo)
    idx = 0
    ok = fail = 0
    while idx < len(pending):
        if time.time() - t_start > BUDGET:
            log(f"BUDGET_OUT dispatched={idx}/{len(pending)}")
            break
        batch = pending[idx:idx + WORKERS]
        idx += len(batch)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for t, status, el in ex.map(run_one, batch):
                log(f"{t[0]} {t[1]} {t[2]}~{t[3]} {status} {el:.0f}s")
                ok += status.startswith("ok")
                fail += not status.startswith("ok")
    remaining = len([t for t in tasks() if not (part_dir(t[0], t[1], t[2]) / "signals_v102.jsonl").exists()])
    log(f"BATCH_DONE ok={ok} fail={fail} remaining={remaining} elapsed={time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
