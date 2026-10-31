# -*- coding: utf-8 -*-
"""GUI **进程外**卡死捕获器（2026-10-30）。

## 为什么需要它（不是重复造轮子）
进程内的卡死看门狗（`t_gui._start_gui_freeze_watchdog`）与线程采样（`core.thread_watch`）
都活在 GUI 进程里。2026-10-30 15:47 的这次卡死，**GUI 的 34 个线程全部阻塞、CPU 增量 0.0s**，
连看门狗线程都没再写过一行 ⇒ 进程内工具整体失效，事后只剩"它卡了"，没有栈。

`py-spy` 走的是进程外读取（不需要目标释放 GIL），这种状态下仍能拿到全部线程栈。

## 判据（两条，任一触发即抓）
1. `psutil.Process.status()` 之外看 **CPU 增量**：连续 3 次（默认 3×10s）增量 < 0.5% ⇒ 疑似全冻结；
2. Windows 窗口 `Responding == False` 持续 30s 以上（psutil 不暴露，用 PowerShell 查）。

## 产物
`t_io/logs/gui_hang_<YYYYmmdd_HHMMSS>.txt`：py-spy dump 原文 + 触发原因 + 当时 psutil 快照。
每个"卡死回合"只抓一次（恢复后重新武装），避免刷屏。

用法：
    python scripts/experiment/gui_hang_catcher.py            # 常驻，Ctrl+C 退出
    python scripts/experiment/gui_hang_catcher.py --once     # 只检查一次（排障用）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_DIR = os.path.join(_BASE, "t_io", "logs")


def _pyspy():
    exe = os.path.join(os.path.dirname(sys.executable), "Scripts", "py-spy.exe")
    return exe if os.path.exists(exe) else "py-spy"


def find_gui() -> list[int]:
    """找出所有跑 t_gui.py 的 pid（可能有多个：单实例守卫会等待）。"""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if "t_gui.py" in cmd and "python" in (p.info.get("name") or "").lower():
                out.append(p.info["pid"])
        except Exception:
            continue
    return out


def _responding(pid: int):
    """Windows 窗口是否响应（psutil 不提供，走 PowerShell）。取不到 → None。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Responding"],
            capture_output=True, text=True, timeout=15)
        v = (r.stdout or "").strip()
        return None if v == "" else (v.lower() == "true")
    except Exception:
        return None


def dump(pid: int, reason: str, extra: dict) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = os.path.join(_LOG_DIR, f"gui_hang_{ts}.txt")
    try:
        r = subprocess.run([_pyspy(), "dump", "--pid", str(pid)],
                           capture_output=True, text=True, timeout=120)
        body = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    except Exception as e:
        body = f"[py-spy 失败] {type(e).__name__}: {e}"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(f"# GUI 卡死快照 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"# pid={pid}  触发={reason}\n")
        f.write(f"# psutil={json.dumps(extra, ensure_ascii=False)}\n\n")
        f.write(body)
    print(f"[capture] {fp}  ({reason})", flush=True)
    return fp


def watch(interval: float, zero_hits: int, once: bool = False) -> int:
    import psutil
    armed = set()           # 已抓过的 pid（恢复后移除，重新武装）
    zero = {}               # pid -> 连续"零 CPU"次数
    last = {}
    print(f"GUI 卡死捕获器启动：每 {interval:g}s 检查；零 CPU 连续 {zero_hits} 次即抓栈", flush=True)
    while True:
        for pid in find_gui():
            try:
                p = psutil.Process(pid)
                t = p.cpu_times()
                cur = float(t.user + t.system)
                prev = last.get(pid)
                last[pid] = cur
                delta = None if prev is None else (cur - prev)
                resp = _responding(pid) if delta is not None else None
                if delta is not None:
                    if delta < 0.005 * interval:      # 10s 内 <0.5% 单核
                        zero[pid] = zero.get(pid, 0) + 1
                    else:
                        zero[pid] = 0
                        armed.discard(pid)            # 有活 ⇒ 重新武装
                stuck = zero.get(pid, 0) >= zero_hits
                if (stuck or resp is False) and pid not in armed:
                    why = "cpu=0" if stuck else "responding=False"
                    dump(pid, why, {"cpu_total_s": round(cur, 2), "delta": delta,
                                    "responding": resp, "threads": p.num_threads(),
                                    "status": p.status()})
                    armed.add(pid)
            except Exception:
                continue
        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--zero-hits", type=int, default=3, help="连续多少次零 CPU 判卡死")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    watch(a.interval, a.zero_hits, a.once)
