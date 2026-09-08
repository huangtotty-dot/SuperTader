# -*- coding: utf-8 -*-
"""core/v8guard.py — py_mini_racer(V8) 主线程预热守卫（2026-09-08）。

背景：akshare 部分接口底层用 py_mini_racer(V8) 执行 JS。V8 的 configurable pool
（IsConfigurablePoolInitialized）**必须由主线程首次初始化**——若后台/工作线程成为首个触发者，
V8 直接 abort 整进程（FATAL:partition_address_space）。项目已在「热度补算」处用子进程隔离，
但其它经 worker 线程调 akshare 的路径（如扫描线程内 stock_zh_a_hist/新浪日线兜底）同样会踩。

对策：进程启动后**第一时间在主线程**初始化一次 py_mini_racer，令 V8 pool 落在主线程，
此后任何线程内的懒初始化都安全。调用点：main.py __main__ / t_gui __main__ 等长驻进程入口。
"""
import sys


def prewarm_akshare_v8() -> bool:
    """主线程预热 akshare 的 py_mini_racer(V8) 引擎。返回是否成功（缺 py_mini_racer → False）。"""
    if sys.platform == "win32":
        # Windows 上同 main.py 其它模块的 UTF-8 修复：无副作用，保留幂等
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        from py_mini_racer import MiniRacer
        _mr = MiniRacer()
        _mr.eval("1 + 1")
        del _mr
        return True
    except Exception:
        return False
