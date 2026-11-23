# -*- coding: utf-8 -*-
"""诊断指标计数与落盘（2026-11-23 P1-6 首片）
================================================================================
背景（架构评估 doc/solutions/2026-11-23_架构优化机会评估.md P1-6）：
全仓 339+ 处 `except: pass`（gui.py 203 / core/ 136 / execution/ 182）——多数是守护线程的
**刻意静默**（UI 鲁棒性策略，不该清零），但故障现场没有任何计数，"静默死亡"的病根文化
在错误处理层延续。

本模块的做法是**计数化，不是清零**：`swallow(tag, exc)` 保持静默行为不变，同时累计次数、
记最近一次异常类型；`flush()` 把快照 append 到 `t_io/logs/metrics.jsonl`（一行一进程一快照），
让"吞了什么、吞了多少、最后一次是什么异常"从此可见。

设计纪律：
- **自身绝不抛异常**——诊断链不能影响业务，所有对外函数内部兜底。
- 零依赖、线程安全；进程内计数，进程重启清零（jsonl 里留历史）。
- 不强制接入调度：各进程在自有周期循环里调 `flush()` 即可（首片接 core/scan.py EOD 链，
  每日复盘段落一次；gui.py 203 处的铺开留待后续批次，避免与并行施工冲突）。

用法：
    from core.diag_metrics import swallow
    try:
        ...
    except Exception as e:
        swallow("chart_cache.acquire_lock", e)   # 原来是裸 pass，行为不变但有了计数
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter

_LOCK = threading.Lock()
_COUNTERS: Counter = Counter()
_LAST_EXC: dict = {}
_FIRST_TS: dict = {}
_LAST_TS: dict = {}
_PROCESS_TAG = [""]          # 用单元素列表当可变单元，免 global 声明


def set_process_tag(tag: str) -> None:
    """标记进程身份（如 "gui" / "gm_main" / "hunter_worker"），写进每行快照。"""
    try:
        _PROCESS_TAG[0] = str(tag or "")
    except Exception:
        pass


def swallow(tag: str, exc: BaseException | None = None) -> None:
    """在 except 块里替代裸 pass：计数 + 记最近一次异常类型。**行为与 pass 完全等价**。"""
    try:
        now = time.time()
        with _LOCK:
            _COUNTERS[tag] += 1
            _FIRST_TS.setdefault(tag, now)
            _LAST_TS[tag] = now
            if exc is not None:
                _LAST_EXC[tag] = f"{type(exc).__name__}: {str(exc)[:120]}"
    except Exception:
        pass


def snapshot() -> dict:
    """当前计数快照（可 JSON 序列化）；无吞点时 swallows 为空 dict。"""
    try:
        now = time.time()
        with _LOCK:
            tags = {
                t: {
                    "count": c,
                    "first": _FIRST_TS.get(t),
                    "last": _LAST_TS.get(t),
                    "last_exc": _LAST_EXC.get(t),
                }
                for t, c in _COUNTERS.items()
            }
        return {
            "process": _PROCESS_TAG[0],
            "ts": now,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "swallows": tags,
        }
    except Exception:
        return {"process": "", "ts": 0.0, "time": "", "swallows": {}}


def flush(path: str | None = None) -> str | None:
    """把快照 append 到 metrics.jsonl（一行一快照）。无吞点时不写（防空行刷屏）。
    返回写入路径；失败返回 None（诊断链自身静默）。"""
    try:
        snap = snapshot()
        if not snap["swallows"]:
            return None
        if path is None:
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "t_io", "logs", "metrics.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return None


def _reset_for_test() -> None:
    """仅供单测清理进程内状态（生产代码不应调用）。"""
    with _LOCK:
        _COUNTERS.clear()
        _LAST_EXC.clear()
        _FIRST_TS.clear()
        _LAST_TS.clear()
    _PROCESS_TAG[0] = ""
