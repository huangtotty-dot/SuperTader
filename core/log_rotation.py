# -*- coding: utf-8 -*-
"""诊断日志的**大小上限轮转**（2026-11-13）。

背景：GUI 卡死排查类日志（`gui_freeze.log` / `chart_zoom.log` /
`gui_client_diag.log` / `gui_slow_calls.log` / `gui_thread_dumps.log` /
`cpu_samples.jsonl` / `thread_cpu.jsonl`）都是手工 `open(fp, "a").write(...)`，
没有 Python `logging` 的 `RotatingFileHandler`，一个卡死复现日就能灌出几十 MB
（实测 gui_freeze.log 单日 49MB）。老日志只有 ~18MB、删不掉，真正修法是**上限轮转**。

这里只提供一个 6 行的小函数，在每次追加**前**调用：超过 `max_mb` 就把当前文件
改名成 `fp.1`（先删掉旧 `.1`），下一次 `open(fp, "a")` 自然新起一个文件。
失败静默（Windows 上文件若正被另一进程以追加模式打开，`os.rename` 会抛
`PermissionError` —— 此时本次跳过轮转、下个刷新周期再试，可接受的降级）。

**故意不轮转**：`execution/auto/gm_main.py` 的 `auto_backtrace.jsonl` 是交易回补链
审计数据，不是诊断日志，绝不能丢。
"""
from __future__ import annotations

import os


def cap_log_file(fp, max_mb: float = 10.0) -> None:
    """若日志文件超过 `max_mb` MB，则原地改名成 `<fp>.1`（覆盖旧 `.1`）。

    `fp` 接受 str 或 pathlib.Path。失败静默（文件被占用/不存在等）。
    调用方随后照常 `open(fp, "a")`，会自动新建小文件。
    """
    try:
        p = os.fspath(fp)
        if os.path.getsize(p) <= max_mb * 1024 * 1024:
            return
        bak = p + ".1"
        if os.path.exists(bak):
            os.remove(bak)
        os.rename(p, bak)
    except OSError:
        pass
