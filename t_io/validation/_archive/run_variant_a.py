# -*- coding: utf-8 -*-
"""
run_variant_a.py — v1.1.1 变体A全管线实验驱动(仿 run_degraded.py, 幂等可续)
两组同参数 A/B(均 --ab v102 生产代码 + 活簿记 + 统一口径 + T_DAILY_CTX=1 真实日上下文):
  control : 仅键修复(门控现状语义: near_ma5_chop/above_ma5_trend 放行)
  variantA: 键修复+变体A(T_GATE_VARIANT_A=1, below_ma5_weak且slope>=0 放行)
预注册闸门: 买入闭环>=10对 + 买入胜率>=50% + 卖侧不退化超1pp + 阴跌日胜率>=0.35
用法: python run_variant_a.py  (断点重入同命令; BUDGET=100s 派发截止)
"""
import json, os, subprocess, sys, time
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/e2_variant_a"
LOG = ROOT / "progress.log"
CODES = ["000988", "588170", "600176", "600481", "603667"]
SEGS = [("2026-03-16", "2026-03-20"), ("2026-03-23", "2026-03-27"), ("2026-03-30", "2026-03-31"),
        ("2026-04-01", "2026-04-03"), ("2026-04-06", "2026-04-10"), ("2026-04-13", "2026-04-17"),
        ("2026-04-20", "2026-04-24"), ("2026-04-27", "2026-04-30"), ("2026-05-01", "2026-05-08"),
        ("2026-05-11", "2026-05-15"), ("2026-05-18", "2026-05-22"), ("2026-05-25", "2026-05-29"),
        ("2026-06-01", "2026-06-05"), ("2026-06-08", "2026-06-12"), ("2026-06-15", "2026-06-19"),
        ("2026-06-22", "2026-06-26"), ("2026-06-29", "2026-06-30"), ("2026-07-01", "2026-07-03"),
        ("2026-07-06", "2026-07-10"), ("2026-07-13", "2026-07-17"), ("2026-07-20", "2026-07-24")]
GROUPS = {"control": {}, "variantA": {"T_GATE_VARIANT_A": "1"}}
BUDGET = 150
WORKERS = 5
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
    log(f"START VARIANT_A remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
    if not todo:
        log("NOTHING_TODO")
        return 0
    t_start = time.time()
    from concurrent.futures import ThreadPoolExecutor
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
