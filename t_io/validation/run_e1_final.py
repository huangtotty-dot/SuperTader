# -*- coding: utf-8 -*-
"""
run_e1_final.py — E1 决赛档全管线实验驱动(仿 run_variant_a.py, 幂等可续)
两组同参数 A/B(均 --ab v102 生产代码 + 活簿记 + 统一口径 + T_DAILY_CTX=1 真实日上下文):
  T36b: 引擎买阈36(T_BUY_BONUS_MIN_SCORE=36) + 买侧通知阈对齐36(T_NOTIFY_BUY=36)
  T30b: 引擎买阈30(T_BUY_BONUS_MIN_SCORE=30) + 买侧通知阈对齐30(T_NOTIFY_BUY=30)
对照基线: 复用 e2_variant_a/control 合并结果(买126 wr0.5044 / 452总 wr0.4770 / 41对 +221.31)。
锁定闸门: ①买wr>=0.49 ②买闭环>=41对且PnL>=+221.31 ③卖侧wr退化<=1pp ④阴跌日买wr>=0.30 ⑤买密度∈[0.30,0.60]/股日
冒烟已过: 默认值下4块 signals+decision_trace 与 control 同块逐字节一致(MD5/逐条)。
用法: python run_e1_final.py  (断点重入同命令; BUDGET=150s 派发截止)
"""
import json, os, subprocess, sys, time
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/e1_final"
LOG = ROOT / "progress.log"
CODES = ["000988", "588170", "600176", "600481", "603667"]
SEGS = [("2026-03-16", "2026-03-20"), ("2026-03-23", "2026-03-27"), ("2026-03-30", "2026-03-31"),
        ("2026-04-01", "2026-04-03"), ("2026-04-06", "2026-04-10"), ("2026-04-13", "2026-04-17"),
        ("2026-04-20", "2026-04-24"), ("2026-04-27", "2026-04-30"), ("2026-05-01", "2026-05-08"),
        ("2026-05-11", "2026-05-15"), ("2026-05-18", "2026-05-22"), ("2026-05-25", "2026-05-29"),
        ("2026-06-01", "2026-06-05"), ("2026-06-08", "2026-06-12"), ("2026-06-15", "2026-06-19"),
        ("2026-06-22", "2026-06-26"), ("2026-06-29", "2026-06-30"), ("2026-07-01", "2026-07-03"),
        ("2026-07-06", "2026-07-10"), ("2026-07-13", "2026-07-17"), ("2026-07-20", "2026-07-24")]
GROUPS = {"T36b": {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36"},
          "T30b": {"T_BUY_BONUS_MIN_SCORE": "30", "T_NOTIFY_BUY": "30"}}
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
    log(f"START E1_FINAL remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
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
