# -*- coding: utf-8 -*-
"""
run_degraded.py — v1.1.0 降级形态 harness 复测驱动(单组 v102 通道, 90日x5股)
用法: python run_ab_threshold.py [SELL_TH=55] [EARLY_TH=SELL_TH+10]
两组同阈值全量重跑: 5股 x 21段 x 2模式 = 210 块, 数据源 minute_snapshots_ts (统一口径)
输出: t_io/validation/v109_threshold/t{SELL_TH}/parts/
幂等可续; BUDGET=100s 派发截止(一批5并发~130-200s, 保证不被外层超时杀)
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
SELL_TH = sys.argv[1] if len(sys.argv) > 1 else "55"
EARLY_TH = sys.argv[2] if len(sys.argv) > 2 else str(int(float(SELL_TH)) + 10)
ROOT = BASE / "t_io/validation/v110_degraded"
PARTS = ROOT / "parts"
PROGRESS = ROOT / "progress.log"
CODES = ["000988", "588170", "600176", "600481", "603667"]
SEGS = [("2026-03-16", "2026-03-20"), ("2026-03-23", "2026-03-27"), ("2026-03-30", "2026-03-31"),
        ("2026-04-01", "2026-04-03"), ("2026-04-06", "2026-04-10"), ("2026-04-13", "2026-04-17"),
        ("2026-04-20", "2026-04-24"), ("2026-04-27", "2026-04-30"),
        ("2026-05-01", "2026-05-08"), ("2026-05-11", "2026-05-15"), ("2026-05-18", "2026-05-22"),
        ("2026-05-25", "2026-05-29"),
        ("2026-06-01", "2026-06-05"), ("2026-06-08", "2026-06-12"), ("2026-06-15", "2026-06-19"),
        ("2026-06-22", "2026-06-26"), ("2026-06-29", "2026-06-30"),
        ("2026-07-01", "2026-07-03"), ("2026-07-06", "2026-07-10"), ("2026-07-13", "2026-07-17"),
        ("2026-07-20", "2026-07-24")]
MODES = ["v102"]  # 降级形态=新生产代码 v102 通道(TrendRegime 信息层保留, 门控已拆)
BUDGET = 100
WORKERS = 5
PY = sys.executable
CHILD_ENV = dict(os.environ)
CHILD_ENV["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")

def tasks():
    for mode in MODES:
        for code in CODES:
            for s, e in SEGS:
                yield mode, code, s, e

def part_dir(mode, code, s):
    return PARTS / f"{mode}_{code}_{s.replace('-', '')}"

def log(msg):
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def run_task(t):
    mode, code, s, e = t
    od = part_dir(mode, code, s)
    sig = od / f"signals_{mode}.jsonl"
    if sig.exists():
        return t, "skip", 0.0
    od.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    def _run():
        return subprocess.run(
            [PY, str(BASE / "harness_backtest.py"), "--codes", code,
             "--start", s, "--end", e, "--ab", mode, "--out", str(od)],
            capture_output=True, text=True, timeout=900, cwd=str(BASE), env=CHILD_ENV)
    try:
        r = _run()
    except subprocess.TimeoutExpired:
        return t, "timeout", time.time() - t0
    def _valid():
        if not sig.exists():
            return False
        try:
            n = sum(1 for l in open(sig, encoding="utf-8") if l.strip())
            # 空信号需确认 summary 日覆盖完整(真空 vs 早夭)
            sm = od / f"summary_{mode}.json"
            return n > 0 or sm.exists()
        except Exception:
            return False
    if _valid():
        n = sum(1 for l in open(sig, encoding="utf-8") if l.strip())
        if n == 0:
            for junk in od.glob("*"):
                junk.unlink()
            try:
                r = _run()
            except subprocess.TimeoutExpired:
                return t, "timeout", time.time() - t0
            if sig.exists():
                return t, "ok_retry_empty", time.time() - t0
            return t, "fail_no_signals", time.time() - t0
        return t, "ok", time.time() - t0
    for junk in od.glob("*"):
        junk.unlink()
    try:
        r = _run()
    except subprocess.TimeoutExpired:
        return t, "timeout", time.time() - t0
    return (t, "ok_retry", time.time() - t0) if sig.exists() else (t, "fail_no_signals", time.time() - t0)

def main():
    todo = [t for t in tasks() if not (part_dir(t[0], t[1], t[2]) / f"signals_{t[0]}.jsonl").exists()]
    log(f"START DEGRADED remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
    if not todo:
        log("NOTHING_TODO")
        return 0
    t_start = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        pending = list(todo)
        idx = 0
        while idx < len(pending):
            if time.time() - t_start > BUDGET:
                log(f"BUDGET_OUT dispatched={idx}/{len(pending)}")
                break
            batch = pending[idx:idx + WORKERS]
            idx += WORKERS
            fs = [ex.submit(run_task, t) for t in batch]
            for f in fs:
                t, status, dt = f.result()
                log(f"{t[0]} {t[1]} {t[2]}~{t[3]} {status} {dt:.0f}s")
                if "ok" in status or status == "skip":
                    ok += 1
                else:
                    fail += 1
    log(f"BATCH_DONE ok={ok} fail={fail} remaining={len(todo) - ok} elapsed={time.time() - t_start:.0f}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
