# -*- coding: utf-8 -*-
"""
run_w32_c1.py — W32 §4 C1-final(接回解耦)全管线决赛驱动(仿 run_e1_final.py, 幂等可续)
两组同参数 A/B（--ab v102 生产代码 + 活簿记 + 统一口径 + T_DAILY_CTX=1 + 历史holdings@3d80810）:
  ctl: T36b 同款 env(T_BUY_BONUS_MIN_SCORE=36 + T_NOTIFY_BUY=36) — 新鲜对照（隔离 V1.1.2 漂移）
  C1 : ctl + T_BUYBACK_BYPASS_GATES=1（接回信号绕过 daily_overheated/index_uni_down_clearance）
冒烟已过: 默认关+同款env+历史holdings → 4块与 T36b 同块逐字节一致(600176两块trace层V1.1.2解盲
          漂移已定性: rsi NaN→50, signals 零变化; 603667 env float/int 注入后全一致)。
锁定六闸门(对照 ctl 新鲜基线, 预判参考 T36b): ①总买wr>=0.4788 ②闭环>=47对且PnL>=+252.98
  ③卖侧退化<=1pp ④阴跌日买wr>=0.30 ⑤买密度∈[0.30,0.60]且单股<=7/日
  ⑥绕过白名单校验+bear日接回wr>=0.35
用法: python run_w32_c1.py  (断点重入同命令; BUDGET=100s 派发截止)
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/w32_c1"
LOG = ROOT / "progress.log"
HOLDINGS_SNAP = ROOT / "holdings_snapshot_3d80810.json"
sys.path.insert(0, str(BASE / "t_io/validation"))
from run_ab_expanded import SEGS  # noqa: E402

CODES = ["000988", "588170", "600176", "600481", "603667"]
BASE_ENV = {"T_BUY_BONUS_MIN_SCORE": "36", "T_NOTIFY_BUY": "36",
            "T_HOLDINGS_FILE": str(HOLDINGS_SNAP)}
GROUPS = {"ctl": dict(BASE_ENV),
          "C1": {**BASE_ENV, "T_BUYBACK_BYPASS_GATES": "1"}}
BUDGET = 100
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
    log(f"START W32_C1 remaining={len(todo)} at {time.strftime('%H:%M:%S')}")
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
