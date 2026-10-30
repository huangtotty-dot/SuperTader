# -*- coding: utf-8 -*-
"""进程内**线程级** CPU 归因（2026-10-21）。

## 为什么需要它
今日 CPU 采样（`scripts/experiment/cpu_sampler.py`）显示 GUI 进程：

  · 180 个样本里 **43% 的单点 >90% 单核**，中位只有 44% ⇒ 双峰，是**周期性重活**；
  · 冲顶时刻精确落在 **:00 / :30**（30 分钟周期）；15:00 那一刻 main 已降到 1%，
    GUI 仍是 95% ⇒ 这口锅在 **GUI 进程内部**，不在 main。

但外部采样只能看到"进程"这一层。想在进程外拿到线程名不行：实测 Windows 上
`GetThreadDescription` 对本机 Python 线程**全部返回 None**（Python 3.11 不把
`threading.Thread(name=…)` 写进 OS 线程描述）。

⇒ 只能进进程内部：`threading.enumerate()` 的 `ident` 与 `psutil.Process().threads()`
的 `id` **能对上**（本机实测对上 2/2），于是可以拿到「线程名 → CPU 增量」的精确归因。

## 成本
daemon 线程，每 `interval` 秒读一次各线程 `cpu_times()`（纯本地 syscall），
每 `flush_sec` 秒把该窗口的热点写一行 JSONL。35 个线程 × 5s ≈ 可忽略。

## 产物
`t_io/logs/thread_cpu.jsonl`，一行一个窗口：
    {"ts": "…", "window_s": 60.1, "total_pct": 95.2,
     "top": [{"name": "chart-live-*（K线实时尾）", "pct": 62.1, "n": 1, "threads": 1}, …]}
`pct` = 该窗口内**占一个核的百分比**（同名线程已合并）。
"""
import json
import os
import threading
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_FP = os.path.join(_BASE, "t_io", "logs", "thread_cpu.jsonl")

_INTERVAL = 5.0
_FLUSH_SEC = 60.0
_MAX_TOP = 12
_lock = threading.Lock()
_started = False

# 线程名 → 可读分组（去掉每只票的后缀，避免 9 个 mw-* 各占一行）
_GROUP_RULES = (
    ("mw-", "mw-*（多窗口）"),
    ("chart-live-", "chart-live-*（K线实时尾）"),
    ("company-profile", "company-profile（公司资料）"),
    ("ThreadPoolExecutor", "ThreadPoolExecutor"),
    ("hunter", "hunter*（猎手）"),
    ("prewarm", "prewarm*（预热）"),
)


def group_of(name: str) -> str:
    """线程名 → 分组名（同组的线程 CPU 相加）。认不出的原样返回。"""
    n = str(name or "")
    for pre, label in _GROUP_RULES:
        if n.startswith(pre) or pre in n:
            return label
    return n or "?"


def snapshot(prev: dict, dt: float) -> dict:
    """读一次各线程 CPU，返回本窗口聚合。

    `prev` = {tid: (name, cpu_seconds)}，**原地更新**（调用方跨窗口持有）。
    返回 {"by_group": {组: pct}, "raw": [{name,pct,threads}]}；失败返回空。
    """
    try:
        import psutil
    except ImportError:
        return {}
    try:
        proc = psutil.Process()
        ths = proc.threads()
    except Exception:
        return {}
    # 进程内才能拿到名字：ident 与 psutil 的 thread id 是同一个值
    names = {}
    try:
        for t in threading.enumerate():
            if t.ident:
                names[t.ident] = t.name
    except Exception:
        pass

    cur = {}
    per_name = {}
    for t in ths:
        nm = names.get(t.id) or f"tid:{hex(t.id & 0xFFFFFFFF)}"
        tot = float(t.user_time + t.system_time)
        cur[t.id] = (nm, tot)
        old = prev.get(t.id)
        if old is not None and dt > 0:
            d = tot - old[1]
            if d > 0:
                per_name.setdefault(nm, [0.0, 0])   # [cpu_pct, 线程数]
                per_name[nm][0] += d / dt * 100.0
                per_name[nm][1] += 1
    prev.clear()
    prev.update(cur)

    by_group = {}
    rows = []
    for nm, (pct, n) in per_name.items():
        g = group_of(nm)
        by_group[g] = by_group.get(g, 0.0) + pct
        rows.append({"name": nm, "group": g, "pct": round(pct, 2), "threads": n})
    rows.sort(key=lambda x: -x["pct"])
    grp_rows = [{"name": g, "pct": round(v, 2),
                 "threads": sum(r["threads"] for r in rows if r["group"] == g)}
                for g, v in by_group.items()]
    grp_rows.sort(key=lambda x: -x["pct"])
    return {"raw": rows, "by_group": grp_rows,
            "total_pct": round(sum(by_group.values()), 2)}


def make_record(top: list, window_s: float, label: str = "", ts: str = None) -> dict:
    """组装一条窗口记录（2026-10-30：抽出来便于单测 `proc` 字段与形状）。

    `proc` = 进程标签（"gui" / "main"）。GUI 与 main 同时采样追加同一份 JSONL，
    没有这个字段两边的线程名会混在一起（此前只有 GUI 在采，所以没暴露）。
    """
    return {"ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "proc": str(label or ""),
            "window_s": round(float(window_s), 1),
            "total_pct": round(sum(x["pct"] for x in (top or [])), 2),
            "top": top or []}


def _loop(interval: float, flush_sec: float, stop_event, label: str = ""):
    prev = {}
    win = []          # 本窗口内的各次快照（按组求和后再平均）
    t_last_flush = time.time()
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        time.sleep(interval)
        try:
            snap = snapshot(prev, interval)
            if snap:
                win.append(snap)
        except Exception:
            continue
        now = time.time()
        if now - t_last_flush < flush_sec or not win:
            continue
        # 写一行：窗口内**按组平均**（不是累加），避免窗口越长数字越大
        acc = {}
        for s in win:
            for r in s["by_group"]:
                e = acc.setdefault(r["name"], [0.0, 0, 0])
                e[0] += r["pct"]
                e[1] += 1
                e[2] += r["threads"]
        n = len(win)
        top = sorted(({"name": k, "pct": round(v[0] / n, 2),
                       "threads": max(1, v[2] // max(1, v[1]))}
                      for k, v in acc.items()), key=lambda x: -x["pct"])[:_MAX_TOP]
        if not top:              # 这一段没有任何线程在用 CPU（空闲窗口）⇒ 不写，别灌水
            win = []
            t_last_flush = now
            continue
        rec = make_record(top, now - t_last_flush, label)
        try:
            os.makedirs(os.path.dirname(_LOG_FP), exist_ok=True)
            with open(_LOG_FP, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        win = []
        t_last_flush = now


def start(interval: float = _INTERVAL, flush_sec: float = _FLUSH_SEC,
          stop_event=None, label: str = "") -> bool:
    """启动进程内线程采样（幂等）。返回是否真的启动了。

    `label`（2026-10-30）：进程标签（"gui" / "main"）。GUI 与 main 同时采样会追加到
    **同一个** JSONL ⇒ 必须能区分来源，否则两边的线程名混在一起。
    """
    global _started
    with _lock:
        if _started:
            return False
        _started = True
    try:
        threading.Thread(target=_loop,
                         args=(float(interval), float(flush_sec), stop_event, str(label or "")),
                         daemon=True, name="thread-watch").start()
        return True
    except Exception:
        with _lock:
            _started = False
        return False
