# -*- coding: utf-8 -*-
"""main.py 主循环**逐阶段计时**（2026-11-13 自 main.py 抽出）。

**为什么加**（2026-10-16）：owner 报「建仓信号扫描没有实时更新，刷新频率要相对高」。
查明**不是扫描慢** —— 同一轮 52 只单独测得 66.5s；真凶是 **main.py 主循环一整轮要跑 ~15 分钟**：
同一循环里所有"每 5 分钟"任务的实测间隔全掉到 15 分钟（猎手站上5日线 15~18 分钟、
猎手轮询心跳 34~42 分钟、大盘分时预警 32 分钟）⇒ 建仓扫描只能每 ~15 分钟被调到一次。

但**看不到 15 分钟切在哪一步**（原有 `log.debug("⏳ 本轮耗时 …")` 是 debug 级，日志级别 INFO 下
从未落盘）。故加本组计时；`tests/phase3/test_scan_stage_timing.py` 把它钉住，重点钉
**"只观测、不改行为"**：

⚠️ **本段只计时、只打日志，不改任何行为**：阶段判据/调用顺序/节流一律不动，
`_ScanStage.__exit__` 返回 `False` ⇒ **异常照常抛出**（计时不许改变控制流）。

logger 用 `config.log`（与 main 主进程同一 logger），保证「⏳ 本轮耗时」INFO 行仍落主日志
—— 这是本组计时存在的全部意义，抽离后不能改到别的 logger 导致它又"看不见"。
"""
from __future__ import annotations

import time as _time

from config import log

_SCAN_STAGES: dict = {}          # 阶段名 -> [累计秒, 次数, 最慢一次秒]
_SCAN_ROUND_INFO_SEC = 60.0      # 单轮超过它 → INFO 落一行归因（正常轮次不该到）
_SCAN_ROUND_SUMMARY_EVERY = 20   # 另每 N 轮无条件落一行累计均值（拿基线，别只看异常轮）
_SCAN_ROUNDS = [0, 0.0, 0.0]     # [轮数, 累计秒, 最慢一轮秒]（进程内累计，不清零）


def _scan_stage_add(name: str, dt: float) -> None:
    row = _SCAN_STAGES.setdefault(name, [0.0, 0, 0.0])
    row[0] += dt
    row[1] += 1
    if dt > row[2]:
        row[2] = dt


class _ScanStage:
    """`with _ScanStage("名字"):` —— 给 scan_once 的一个阶段计时。

    ⚠️ `__exit__` 返回 `False`：**不吞异常**（计时是观测，不许改变控制流）。
    """

    __slots__ = ("_name", "_t0")

    def __init__(self, name: str):
        self._name = name
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = _time.perf_counter()
        return self

    def __exit__(self, *exc):
        try:
            _scan_stage_add(self._name, _time.perf_counter() - self._t0)
        except Exception:
            pass
        return False


def _scan_stage_report() -> str:
    """各阶段**均值/最慢**（只列均 ≥0.5s 的，降序，最多 8 条）—— 一行能读完。"""
    items = [(n, v[0] / max(1, v[1]), v[2]) for n, v in _SCAN_STAGES.items()]
    items.sort(key=lambda x: -x[1])
    parts = ["%s 均%.1f/最慢%.1fs" % (n, a, m) for n, a, m in items if a >= 0.5][:8]
    return " | ".join(parts) if parts else "（无 ≥0.5s 的阶段）"


def _log_scan_round(elapsed: float) -> None:
    """主循环每轮收尾调用（2026-10-16）。**只读累计值，不改行为**。

    超过 `_SCAN_ROUND_INFO_SEC` 就无条件落 INFO（要归因）；
    否则每 `_SCAN_ROUND_SUMMARY_EVERY` 轮落一行基线（否则只看到异常轮、没有对照）。
    """
    try:
        _SCAN_ROUNDS[0] += 1
        _SCAN_ROUNDS[1] += elapsed
        if elapsed > _SCAN_ROUNDS[2]:
            _SCAN_ROUNDS[2] = elapsed
        if elapsed >= _SCAN_ROUND_INFO_SEC:
            log.info("⏳ 本轮耗时 %.1fs ⇒ 慢阶段归因: %s", elapsed, _scan_stage_report())
        elif _SCAN_ROUNDS[0] % _SCAN_ROUND_SUMMARY_EVERY == 0:
            log.info("⏳ 累计 %d 轮 · 均值 %.1fs · 最慢一轮 %.1fs · 阶段: %s",
                     _SCAN_ROUNDS[0], _SCAN_ROUNDS[1] / _SCAN_ROUNDS[0], _SCAN_ROUNDS[2],
                     _scan_stage_report())
    except Exception:
        pass
