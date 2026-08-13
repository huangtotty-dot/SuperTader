# -*- coding: utf-8 -*-
"""
run_ab_expanded.py — X6 扩样本 A/B 分批并行回测（幂等可续跑，带时间预算干净退出）
任务: 5股 x 18个周段 x 2模式 = 180 子任务, 6 进程并行
预算: 240s 后不再派发新任务，等在途完成后退出（避免超时杀子进程丢进度）
进度: v107_ab_expanded/progress.log
"""
import json, os, subprocess, sys, time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/v107_ab_expanded"
PARTS = ROOT / "parts"
PROGRESS = ROOT / "progress.log"
CODES = ["000988", "588170", "600176", "600481", "603667"]
# 周段（每段<=5个交易日量级，控制单任务时长）
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
BUDGET = 250
WORKERS = 5
PY = sys.executable
CHILD_ENV = dict(os.environ)

def tasks():
    for mode in MODES:
        for code in CODES:
            for s, e in SEGS:
                yield mode, code, s, e

def part_dir(mode, code, s):
    return PARTS / f"{mode}_{code}_{s.replace('-', '')}"

def log(msg):
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
        return t, "TIMEOUT", time.time() - t0
    dt = time.time() - t0
    if sig.exists():
        return t, "ok", dt
    # X6-runner: 偶发进程早夭(空输出/无signals, 非数据问题) — 通用重试一次
    r2 = subprocess.run(
        [PY, str(BASE / "harness_backtest.py"), "--codes", code,
         "--start", s, "--end", e, "--ab", mode, "--out", str(od)],
        capture_output=True, text=True, timeout=900, cwd=str(BASE), env=CHILD_ENV)
    dt = time.time() - t0
    if sig.exists():
        return t, "ok_retry", dt
    (od / "stderr.log").write_text((r.stdout or "")[-2000:] + "\n" + (r.stderr or "")[-2000:]
                                   + "\n--- retry ---\n" + (r2.stdout or "")[-2000:] + "\n" + (r2.stderr or "")[-2000:],
                                   encoding="utf-8")
    return t, "FAIL", dt

def main():
    PARTS.mkdir(parents=True, exist_ok=True)
    todo = [t for t in tasks() if not (part_dir(t[0], t[1], t[2]) / f"signals_{t[0]}.jsonl").exists()]
    log(f"START remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
    if not todo:
        print("ALL_DONE"); return 0
    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        it = iter(todo)
        active = set()
        for _ in range(min(WORKERS, len(todo))):
            active.add(ex.submit(run_task, next(it)))
        while active:
            done_set, _ = concurrent.futures.wait(
                active, timeout=10, return_when=concurrent.futures.FIRST_COMPLETED)
            for f in done_set:
                active.discard(f)
                t, status, dt = f.result()
                if status in ("ok", "ok_retry", "skip"):
                    done += 1
                else:
                    fail += 1
                log(f"{t[0]} {t[1]} {t[2]}~{t[3]} {status} {dt:.0f}s")
            if time.time() - t0 < BUDGET:
                for _ in range(len(done_set)):
                    t = next(it, None)
                    if t is None:
                        break
                    active.add(ex.submit(run_task, t))
            elif not done_set and time.time() - t0 > BUDGET + 200:
                break
        for f in active:
            t, status, dt = f.result()
            if status in ("ok", "ok_retry", "skip"):
                done += 1
            else:
                fail += 1
            log(f"{t[0]} {t[1]} {t[2]}~{t[3]} {status} {dt:.0f}s (tail)")
    remaining = [t for t in tasks() if not (part_dir(t[0], t[1], t[2]) / f"signals_{t[0]}.jsonl").exists()]
    msg = f"BATCH_DONE ok={done} fail={fail} remaining={len(remaining)} elapsed={time.time()-t0:.0f}s"
    log(msg); print(msg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
