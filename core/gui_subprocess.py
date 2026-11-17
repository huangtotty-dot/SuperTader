# -*- coding: utf-8 -*-
"""GUI 看板子进程的生命周期管理（2026-11-13 自 main.py 抽出）。

GUI 看板是**独立子进程**：pywebview 必须跑主线程，而盯盘主循环已占用主线程。
它只读盘上 JSON/JSONL 数据，与盯盘进程天然解耦，失败不影响盯盘。

（2026-11-17：原「两市成交额·桌面 tkinter 浮窗」已按 owner 要求删除 —— 该浮窗的
`launch_turnover_widget` / `close_turnover_widget` 与 `--no-widget` 开关一并移除。）

依赖面刻意收敛为：`os` / `sys` / `subprocess` / `time` / `threading` / `datetime` + `config.log`
+ 仓库根路径 `_ROOT`。不 import 任何交易状态（HOLDINGS/engine/PARAMS），
因此抽出后不会引入与 main.py 的循环依赖。

子进程输出**不丢 DEVNULL**（2026-10-21 教训）：挂件起不来时若输出进黑洞，主日志干净得像没事发生。
落 `t_io/logs/gui_subprocess.log`，并在 3s 后巡一次：秒退就把日志尾巴打进主日志。
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
import time
from datetime import datetime

from config import log

# 仓库根（core/ 为深度 1，dirname(dirname) 即根；与 main.py 的 BASE_DIR 同值）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_GUI_PROC = None
_GUI_LOG_FP = None      # 子进程输出句柄（见 launch_gui）


def launch_gui():
    """拉起桌面 GUI 看板（gui.py）。失败静默降级，不影响盯盘。"""
    global _GUI_PROC, _GUI_LOG_FP
    if "--no-gui" in sys.argv:
        log.info("🖥 GUI 看板: 已通过 --no-gui 禁用")
        return
    try:
        gui_path = os.path.join(_ROOT, "gui.py")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # **2026-10-21：子进程输出不再丢进 DEVNULL**。
        # 事故回顾：gui.py 的 `__main__` 引用了一个不存在的名字，进程在 import 阶段就退出；
        # 而这里 stdout/stderr 都是 DEVNULL ⇒ owner 只看到「运行 python main.py 之后打不开 GUI」，
        # 主日志干净得像没事发生，白找半天。现在落 `t_io/logs/gui_subprocess.log` 并在 3s 后
        # 巡一次：秒退就把日志尾巴打进主日志。
        try:
            _logdir = os.path.join(_ROOT, "t_io", "logs")
            os.makedirs(_logdir, exist_ok=True)
            _GUI_LOG_FP = open(os.path.join(_logdir, "gui_subprocess.log"), "a",
                               encoding="utf-8", errors="replace")
            _GUI_LOG_FP.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 gui.py =====\n")
            _GUI_LOG_FP.flush()
        except Exception:
            _GUI_LOG_FP = None
        _GUI_PROC = subprocess.Popen(
            [sys.executable, gui_path],
            cwd=_ROOT,
            creationflags=flags,
            stdout=(_GUI_LOG_FP or subprocess.DEVNULL),
            stderr=subprocess.STDOUT if _GUI_LOG_FP else subprocess.DEVNULL,
        )
        log.info(f"🖥 GUI 看板: 已启动 (pid={_GUI_PROC.pid})，盯盘结束将自动关闭；"
                 f"如需禁用加 --no-gui；子进程日志 t_io/logs/gui_subprocess.log")

        def _check_alive():
            time.sleep(3)
            try:
                if _GUI_PROC.poll() is None:
                    return
                if _GUI_PROC.returncode == 7:
                    # 单实例闸（2026-10-28）：已有 GUI 在跑，本次拉起的那个主动退出 —— 不是故障
                    log.info("🖥 GUI 看板: 已有实例在运行，本次未重复启动（双开会压垮界面投递层）")
                    return
                tail = ""
                if _GUI_LOG_FP:
                    try:
                        _GUI_LOG_FP.flush()
                        _p = os.path.join(_ROOT, "t_io", "logs", "gui_subprocess.log")
                        with open(_p, encoding="utf-8", errors="replace") as f:
                            tail = "".join(f.readlines()[-12:]).strip()
                    except Exception:
                        tail = ""
                log.warning(f"🖥 GUI 看板启动后立即退出（code={_GUI_PROC.returncode}）"
                            f"—— 主日志外请查 t_io/logs/gui_subprocess.log。tail:\n{tail}")
            except Exception:
                pass

        threading.Thread(target=_check_alive, daemon=True).start()
    except Exception as e:
        log.warning(f"🖥 GUI 看板启动失败（不影响盯盘）: {e}")


def close_gui():
    if _GUI_PROC and _GUI_PROC.poll() is None:
        try:
            _GUI_PROC.terminate()
            log.info("🖥 GUI 看板: 已随盯盘结束关闭")
        except Exception:
            pass
