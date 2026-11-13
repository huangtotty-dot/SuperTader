# -*- coding: utf-8 -*-
"""GUI 看板 + 两市成交额浮窗的子进程生命周期管理（2026-11-13 自 main.py 抽出）。

这两个挂件都是**独立子进程**：pywebview 必须跑主线程，而盯盘主循环已占用主线程；
浮窗（tkinter）同理。它们只读盘上 JSON/JSONL 数据，与盯盘进程天然解耦，失败不影响盯盘。

依赖面刻意收敛为：`os` / `sys` / `subprocess` / `time` / `threading` / `datetime` + `config.log`
+ 仓库根路径 `_ROOT`。不 import 任何交易状态（HOLDINGS/engine/PARAMS），
因此抽出后不会引入与 main.py 的循环依赖。

子进程输出**不丢 DEVNULL**（2026-10-21 教训）：挂件起不来时若输出进黑洞，主日志干净得像没事发生。
分别落 `t_io/logs/gui_subprocess.log` / `turnover_widget.log`，并在 2~3s 后巡一次：秒退就把日志尾巴打进主日志。
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


# ── 两市成交额·桌面浮窗（2026-10-27 owner：独立于 GUI、可自由拖动/置顶）──────────
# 为什么要独立进程：DOM 浮层出不了 WebView 视口，拖不到别的显示器、也压不到别的软件上面。
# 浮窗只显示数字，跟 GUI 窗口没有任何关系（GUI 关了它照样在）。
_WIDGET_PROC = None
_WIDGET_LOG_FP = None


def launch_turnover_widget():
    """拉起成交额浮窗。失败**静默降级**：绝不能因为一个挂件拖住交易主循环。

    ⚠️ 子进程输出**不丢 DEVNULL**（与 `launch_gui` 同一教训，2026-10-21）：
    挂件起不来时（tkinter 缺失/脚本语法错）如果输出进了黑洞，主日志会干净得像没事发生。
    这里落 `t_io/logs/turnover_widget.log`，并在 2s 后巡一次：秒退就把日志尾巴打进主日志。
    """
    global _WIDGET_PROC, _WIDGET_LOG_FP
    if "--no-widget" in sys.argv:
        log.info("🪟 成交额浮窗: 已通过 --no-widget 禁用")
        return
    try:
        path = os.path.join(_ROOT, "scripts", "turnover_widget.py")
        if not os.path.exists(path):
            log.warning(f"🪟 成交额浮窗: 脚本不存在 {path}")
            return
        try:
            _logdir = os.path.join(_ROOT, "t_io", "logs")
            os.makedirs(_logdir, exist_ok=True)
            _WIDGET_LOG_FP = open(os.path.join(_logdir, "turnover_widget.log"), "a",
                                  encoding="utf-8", errors="replace")
            _WIDGET_LOG_FP.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
            _WIDGET_LOG_FP.flush()
        except Exception:
            _WIDGET_LOG_FP = None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 别弹出黑框
        _WIDGET_PROC = subprocess.Popen(
            # 带父 pid：main.py 被强杀（atexit 不跑）时，浮窗自己发现父进程没了就退出，
            # 不会变成"占着单实例互斥量的孤儿"（2026-10-28 实报：浮窗打不开就是这么来的）
            [sys.executable, path, "--parent-pid", str(os.getpid())],
            cwd=_ROOT,
            creationflags=flags,
            stdout=(_WIDGET_LOG_FP or subprocess.DEVNULL),
            stderr=(subprocess.STDOUT if _WIDGET_LOG_FP else subprocess.DEVNULL),
        )
        log.info(f"🪟 成交额浮窗: 已启动 (pid={_WIDGET_PROC.pid})；--no-widget 可禁用")

        def _check_alive():
            time.sleep(2)
            if _WIDGET_PROC is None or _WIDGET_PROC.poll() is None:
                return
            rc = _WIDGET_PROC.returncode
            if rc == 7:
                # 浮窗自己用 7 表示"已有实例在跑，我退出" —— 不是崩溃，别报成"秒退"
                log.info("🪟 成交额浮窗: 已有实例在跑，本次跳过"
                         "（若屏幕上看不到，请在任务管理器结束旧的 python 进程后重开）")
                return
            tail = ""
            try:
                with open(os.path.join(_ROOT, "t_io", "logs", "turnover_widget.log"),
                          encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-8:]).strip()
            except Exception:
                pass
            log.warning(f"🪟 成交额浮窗: 启动后秒退 (rc={rc})"
                        + (f"｜日志尾部:\n{tail}" if tail else ""))

        threading.Thread(target=_check_alive, daemon=True, name="widget-check").start()
    except Exception as e:
        log.warning(f"🪟 成交额浮窗: 拉起失败（不影响主循环）: {e}")


def close_turnover_widget():
    global _WIDGET_PROC
    if _WIDGET_PROC is not None and _WIDGET_PROC.poll() is None:
        try:
            _WIDGET_PROC.terminate()
            log.info("🪟 成交额浮窗: 已随盯盘结束关闭")
        except Exception:
            pass
    _WIDGET_PROC = None
