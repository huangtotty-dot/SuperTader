# -*- coding: utf-8 -*-
"""长任务子进程骨架的公共件（2026-11-23 自 core/hunter_worker.py 抽出）。

背景：P2-4（2026-11-22）hunter_worker 落地了一套教科书级子进程纪律；复盘
（review_worker，同日）是第二个用例。两个 worker 共用的「为什么」集中放这里，
各自文件只留业务差异：

  · **父进程看门狗**：GUI 被 terminate/kill/强退时 atexit 来不及跑 ⇒ 子进程周期
    探活父 PID，消失即 `os._exit(3)` 自杀，不留孤儿继续写状态档/烧 LLM token。
    与 GUI 侧 atexit terminate 互为双保险（atexit 管优雅退出，看门狗管横死）。
  · **状态/结果原子双文件**：tmp + os.replace 原子落盘，读者（GUI 轮询端点）
    任何时刻读到的都是完整 JSON；**结果先落、状态后落**——monitor 以结果档为准，
    状态档只是进度的最后定格，顺序反了会出现「状态说完成但结果还没到」的竞态。
  · **超时**：权威在**父侧 monitor**（`wait_with_timeout`），子进程不自定时——
    双侧计时口径漂移会互相抢杀；父侧强杀后子进程写的部分状态档仍可读（原子写
    不撕裂），正好留给前端/排障看「死在哪一步」。
  · **日志重定向**：stdout/stderr 由父进程落 t_io/logs/*.log（不丢 DEVNULL，
    2026-10-21 教训：秒退时输出进黑洞，主日志干净得像没事发生）。本模块不管
    重定向本身（父侧 Popen 两行的事），只管 worker 侧的 UTF-8 修复。

hunter_worker.py 暂保持原样（其私有副本 _atomic_write_json/_pid_alive/
_parent_watch 与本模块同源同义），守住 P2-4 测试基线；后续迁移只删不增。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time


def utf8_console_fix() -> None:
    """Windows 终端 UTF-8 修复（与 stock_hunter/main.py 同款）：阶段文案含中文/emoji，
    子进程 stdout 被父进程重定向到日志文件后，cp936 解码错误会污染日志。"""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def atomic_write_json(fp, obj) -> None:
    """tmp + os.replace 原子落盘；目录缺失自动补。失败抛给调用方决定。

    读者是另一个进程的轮询端点（800ms~1s 一拍），非原子写会被读到半个 JSON
    ⇒ json.loads 炸一脸。os.replace 同盘原子，tmp 必须同目录（不跨盘）。
    """
    os.makedirs(os.path.dirname(str(fp)), exist_ok=True)
    tmp = str(fp) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, str(fp))


def pid_alive(pid) -> bool:
    """父进程是否活着。探测失败一律当「活着」——误杀（任务白跑/LLM token 白烧）
    比孤儿更糟，孤儿至多泄漏到下次 GUI 启动时被收尸。"""
    if not pid:
        return True                     # 没传父 PID ⇒ 不看门
    if sys.platform == "win32":
        try:
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            try:
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)            # POSIX 探活
        return True
    except OSError:
        return False
    except Exception:
        return True


def parent_watch(pid, interval=2.0, tag="worker") -> None:
    """父进程消失 ⇒ 自杀。GUI 被 terminate/kill 时 atexit 不会跑，只能靠这条。

    用 os._exit 而非 sys.exit：看门狗线程里抛 SystemExit 只杀线程不杀进程，
    而此刻的语义就是「整个 worker 立刻死」（主线程可能正卡在 LLM socket 上，
    等它自己退永远等不到）。不经 finally/atexit 是刻意的——父都死了，落盘
    给谁看。
    """
    while True:
        time.sleep(interval)
        if not pid_alive(pid):
            try:
                print(f"[{tag}] 父进程已退出，子进程随之退出", flush=True)
            except Exception:
                pass
            os._exit(3)


def start_parent_watchdog(pid, interval=2.0, tag="worker") -> None:
    """以守护线程启动父进程看门狗。pid 为空/0 ⇒ 不启动（独立调试模式）。"""
    if not pid:
        return
    threading.Thread(target=parent_watch, args=(pid, interval, tag),
                     daemon=True, name="parent-watch").start()


def wait_with_timeout(proc, timeout, kill_after=5.0):
    """父侧 monitor 的硬超时 wait：超时 ⇒ terminate → `kill_after` 内仍不退 ⇒ kill。

    返回 (returncode, timed_out)。timeout 为 None/≤0 ⇒ 纯 wait 不限时。
    为什么先 terminate 再 kill：terminate（WM_CLOSE/SIGTERM）给子进程一个
    「把结果档写完再走」的机会；kill 是最后手段，代价是状态档可能停在半途
    （原子写保证不撕裂，只是旧）。
    """
    if not timeout or timeout <= 0:
        return proc.wait(), False
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        return proc.wait(timeout=kill_after), True
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        return proc.wait(timeout=kill_after), True
    except Exception:
        return None, True
