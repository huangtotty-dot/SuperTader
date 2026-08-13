# -*- coding: utf-8 -*-
"""
run_ab_unified.py — X7 统一口径复测: 仅重跑"原快照段覆盖的周段"(5股各自首个快照日起)
数据源: T_SNAPSHOT_DIR=t_io/minute_snapshots_ts (全 Tushare 口径)
未受影响的周段(Tushare 段)已在 det 校验中证明与 v107_ab_expanded 结果字节级一致, 合并时直接复用。
幂等可续, BUDGET 250s, WORKERS 5。
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/v108_unified"
PARTS = ROOT / "parts"
PROGRESS = ROOT / "progress.log"
CODES = ["000988", "588170", "600176", "600481", "603667"]
# 各股原腾讯快照段首日(由 ts_fetch_snapshot_seg classify 实测):
FIRST_SNAP = {"000988": "2026-06-04", "588170": "2026-06-22", "600176": "2026-06-12",
              "600481": "2026-05-27", "603667": "2026-05-22"}
SEGS = [("2026-03-16", "2026-03-20"), ("2026-03-23", "2026-03-27"), ("2026-03-30", "2026-03-31"),
        ("2026-04-01", "2026-04-03"), ("2026-04-06", "2026-04-10"), ("2026-04-13", "2026-04-17"),
        ("2026-04-20", "2026-04-24"), ("2026-04-27", "2026-04-30"),
        ("2026-05-01", "2026-05-08"), ("2026-05-11", "2026-05-15"), ("2026-05-18", "2026-05-22"),
        ("2026-05-25", "2026-05-29"),
        ("2026-06-01", "2026-06-05"), ("2026-06-08", "2026-06-12"), ("2026-06-15", "2026-06-19"),
        ("2026-06-22", "2026-06-26"), ("2026-06-29", "2026-06-30"),
        ("2026-07-01", "2026-07-03"), ("2026-07-06", "2026-07-10"), ("2026-07-13", "2026-07-17"),
        ("2026-07-20", "2026-07-24")]
MODES = ["baseline", "v102"]
BUDGET = 100  # 派发截止: 一批(5并发)~130-200s, 保证总时长<290s 不被外层超时杀
WORKERS = 5
PY = sys.executable
CHILD_ENV = dict(os.environ)
CHILD_ENV["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")

def tasks():
    for mode in MODES:
        for code in CODES:
            for s, e in SEGS:
                if e >= FIRST_SNAP[code]:  # 仅受快照段影响的周段
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
    try:
        r = subprocess.run(
            [PY, str(BASE / "harness_backtest.py"), "--codes", code,
             "--start", s, "--end", e, "--ab", mode, "--out", str(od)],
            capture_output=True, text=True, timeout=900, cwd=str(BASE), env=CHILD_ENV)
    except subprocess.TimeoutExpired:
        return t, "timeout", time.time() - t0
    dt = time.time() - t0
    if sig.exists():
        try:
            n = sum(1 for l in open(sig, encoding="utf-8") if l.strip())
        except Exception:
            n = -1
        if n == 0:
            # 空信号块重试一次(偶发进程早夭防护)
            for junk in od.glob("*"):
                junk.unlink()
            try:
                r = subprocess.run(
                    [PY, str(BASE / "harness_backtest.py"), "--codes", code,
                     "--start", s, "--end", e, "--ab", mode, "--out", str(od)],
                    capture_output=True, text=True, timeout=900, cwd=str(BASE), env=CHILD_ENV)
            except subprocess.TimeoutExpired:
                return t, "timeout", time.time() - t0
            dt = time.time() - t0
            if sig.exists():
                return t, "ok_retry_empty", dt
            return t, "fail_no_signals", dt
        return t, "ok", dt
    # 无 signals 文件: 检查 decision_trace 是否写全(判断是否早夭), 通用重试一次
    tr = list(od.glob("decision_trace_*.jsonl"))
    for junk in od.glob("*"):
        junk.unlink()
    try:
        r = subprocess.run(
            [PY, str(BASE / "harness_backtest.py"), "--codes", code,
             "--start", s, "--end", e, "--ab", mode, "--out", str(od)],
            capture_output=True, text=True, timeout=900, cwd=str(BASE), env=CHILD_ENV)
    except subprocess.TimeoutExpired:
        return t, "timeout", time.time() - t0
    dt = time.time() - t0
    return (t, "ok_retry", dt) if sig.exists() else (t, "fail_no_signals", dt)

def main():
    todo = [t for t in tasks() if not (part_dir(t[0], t[1], t[2]) / f"signals_{t[0]}.jsonl").exists()]
    log(f"START remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
    if not todo:
        log("NOTHING_TODO")
        return 0
    t_start = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {}
        it = iter(todo)
        pending = list(todo)
        # 简单方案: 全部提交, 靠预算截断不现实(submit 后无法取消运行中) — 分批派发
        batch_size = WORKERS
        idx = 0
        while idx < len(pending):
            if time.time() - t_start > BUDGET:
                log(f"BUDGET_OUT dispatched={idx}/{len(pending)}")
                break
            batch = pending[idx:idx + batch_size]
            idx += batch_size
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
