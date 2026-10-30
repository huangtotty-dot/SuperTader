# -*- coding: utf-8 -*-
"""CPU 采样器 —— superTrader 全栈 CPU 归因（只读，零风险）。

为什么需要它：系统里跑着多个互不相干的进程（main.py / t_gui.py / gm_main.py /
guardian / iopv / watcher），此前所有"哪里慢"的判断都靠推断或单次日志，**没有
按时间序列、按进程、按线程的 CPU 证据**。看不到就无法系统优化。

本机约束（2026-10-11 实测）：**4 物理核 / 8 逻辑核、可用内存 4.1GB**
⇒ 任何 >4 的线程池都是超额订阅，这一条本身就是 CPU 效率的首要约束。

输出：
  · 控制台：排行报告（按进程 + 按线程）
  · JSONL 时间序列（默认 t_io/logs/cpu_samples.jsonl）——供**每日**对比、抓回归

用法：
    # 采样 60 秒，每 2 秒一次，打印排行
    python scripts/experiment/cpu_sampler.py --duration 60 --interval 2

    # 持续采样（给每日后台跑用），只落盘不刷屏
    python scripts/experiment/cpu_sampler.py --watch --quiet

    # 只看某个 pid 的每线程归因
    python scripts/experiment/cpu_sampler.py --pid 12345 --duration 30

    # 只出最近一次采样的历史汇总（读 JSONL，不起采样）
    python scripts/experiment/cpu_sampler.py --report

只读保证：不 import 任何业务模块、不发网络请求、不写业务目录（只追加自己的 JSONL）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    print("需要 psutil：pip install psutil")
    sys.exit(1)

sys.stdout.reconfigure(encoding="utf-8")

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_JSONL = os.path.join(_BASE, "t_io", "logs", "cpu_samples.jsonl")
THREAD_JSONL = os.path.join(_BASE, "t_io", "logs", "thread_cpu.jsonl")

# 进程角色识别（按命令行关键字 → 角色名），顺序即优先级
ROLE_RULES = [
    ("gm_main.py", "gm_main(交易)"),
    ("auto_process_guardian", "guardian(守护)"),
    ("iopv_snapshot", "iopv"),
    ("watcher", "watcher(桥告警)"),
    ("t_gui.py", "GUI(看板)"),
    ("main.py", "main(盯盘)"),
    ("daily_sentiment", "sentiment(热度)"),
    ("daily_review", "daily_review"),
]
OTHER_PROCS = ("msedgewebview2", "msedge", "python", "pythonw")


def role_of(proc: "psutil.Process") -> str:
    try:
        parts = " ".join(proc.cmdline() or [])
    except Exception:
        parts = ""
    low = parts.lower()
    for key, role in ROLE_RULES:
        if key in low:
            return role
    try:
        nm = (proc.name() or "").lower()
    except Exception:
        nm = ""
    if "msedgewebview2" in nm:
        return "WebView2(渲染)"
    if "python" in nm:
        return "python(未识别)"
    return nm or "?"


def iter_targets(all_procs: bool):
    """产出 (pid, proc, role)。默认只看相关进程，--all 看全部。"""
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.pid == me:
                continue
            nm = (p.info.get("name") or "").lower()
            if not all_procs and not any(k in nm for k in OTHER_PROCS):
                continue
            yield p.pid, p, role_of(p)
        except Exception:
            continue


def snapshot(procs, prev_cpu: dict, dt: float) -> list[dict]:
    """采一次。CPU% = Δcpu_time / Δwall / 核数归一（100% = 占满全部逻辑核）。"""
    rows = []
    for pid, p, role in procs:
        try:
            ct = p.cpu_times()
            tot = float(ct.user + ct.system)
            mem = p.memory_info()
            nthreads = p.num_threads()
            pct = 0.0
            if pid in prev_cpu and dt > 0:
                pct = max(0.0, (tot - prev_cpu[pid]) / dt) * 100.0
            prev_cpu[pid] = tot
            rows.append({
                "pid": pid, "role": role,
                "cpu_pct": round(pct, 2),
                "rss_mb": round(mem.rss / 2 ** 20, 1),
                "threads": nthreads,
                "cpu_total_s": round(tot, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def thread_breakdown(pid: int, prev: dict, dt: float) -> list[dict]:
    """某进程的每线程 CPU 归因（Windows 下 psutil 给每线程 user/system time）。"""
    try:
        p = psutil.Process(pid)
        ths = p.threads()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    out = []
    for t in ths:
        tot = float(t.user_time + t.system_time)
        pct = 0.0
        if t.id in prev and dt > 0:
            pct = max(0.0, (tot - prev[t.id]) / dt) * 100.0
        prev[t.id] = tot
        out.append({"tid": hex(t.id & 0xFFFFFFFF), "raw_tid": t.id,
                    "cpu_pct": round(pct, 2), "cpu_total_s": round(tot, 2)})
    out.sort(key=lambda x: -x["cpu_pct"])
    return out


def fmt_report(rows: list[dict], threads: dict[int, list[dict]], dt: float) -> str:
    lines = []
    total = sum(r["cpu_pct"] for r in rows)
    ncpu = psutil.cpu_count()
    lines.append(f"── 采样窗口 {dt:.1f}s ｜ 逻辑核 {ncpu} ｜ "
                 f"相关进程合计 {total:.1f}% (={total/100:.2f} 核 / {ncpu} 逻辑核) ──")
    lines.append(f"{'角色':<18}{'pid':>7}{'CPU%':>9}{'核':>7}{'RSS(MB)':>10}{'线程':>6}")
    for r in sorted(rows, key=lambda x: -x["cpu_pct"]):
        if r["cpu_pct"] <= 0 and r["rss_mb"] < 1:
            continue
        lines.append(f"{r['role']:<18}{r['pid']:>7}{r['cpu_pct']:>9.2f}"
                     f"{r['cpu_pct']/100:>7.2f}{r['rss_mb']:>10.1f}{r['threads']:>6}")
    for pid, ths in threads.items():
        hot = [t for t in ths if t["cpu_pct"] > 0.5]
        if not hot:
            continue
        lines.append(f"  └ pid {pid} 热点线程 Top8：")
        for t in hot[:8]:
            lines.append(f"      {t['tid']:>12}  {t['cpu_pct']:>7.2f}%  "
                         f"累计 {t['cpu_total_s']:>8.2f}s")
    return "\n".join(lines)


def cmd_report(path: str) -> int:
    """读 JSONL 出「按天 × 按角色」的 CPU 汇总，供每日对比。"""
    if not os.path.exists(path):
        print(f"⛔ 无历史：{path}")
        return 0
    by_day: dict[str, dict[str, list[float]]] = {}
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            n += 1
            day = rec.get("ts", "")[:10]
            for r in rec.get("procs", []):
                by_day.setdefault(day, {}).setdefault(r["role"], []).append(r["cpu_pct"])
    if not by_day:
        print("⛔ JSONL 里没有可用样本")
        return 0
    print(f"样本 {n} 条，覆盖 {len(by_day)} 天：{path}\n")
    print(f"{'日期':<12}{'角色':<18}{'样本':>6}{'均值%':>9}{'峰值%':>9}{'核·均值':>10}")
    for day in sorted(by_day):
        for role, vals in sorted(by_day[day].items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            avg = sum(vals) / len(vals)
            print(f"{day:<12}{role:<18}{len(vals):>6}{avg:>9.2f}{max(vals):>9.2f}"
                  f"{avg/100:>10.2f}")
        print()
    return 0


def cmd_report_threads(path: str) -> int:
    """读**线程级**采样（`core/thread_watch.py` 落盘）→ 按天 × 按线程组汇总。

    为什么单独一个入口：进程级采样只能告诉你"GUI 很忙"，而 2026-10-21 的复盘发现
    CPU 是**双峰**（43% 的采样点 >90% 单核）且带 **30 分钟周期** —— 到底是
    `chart-live-*`（K线实时尾）、`mw-*`（多窗口）、还是别的线程在吃核，
    只有线程级数据能回答。
    """
    if not os.path.exists(path):
        print(f"⛔ 无线程级数据：{path}")
        print("   （需重启 t_gui.py —— 采样在进程内跑，见 core/thread_watch.py）")
        return 0
    by_day: dict[str, dict[str, list[float]]] = {}
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            n += 1
            day = str(rec.get("ts", ""))[:10]
            # 2026-10-30：GUI 与 main 同时采样、追加同一份 JSONL ⇒ 按 `proc` 区分。
            # 旧记录没有 proc 字段（只有 GUI 在采），保持原样显示。
            _proc = str(rec.get("proc") or "")
            for r in rec.get("top", []) or []:
                _nm = r.get("name") or "?"
                _key = f"{_proc}/{_nm}" if _proc else _nm
                by_day.setdefault(day, {}).setdefault(_key, []).append(
                    float(r.get("pct") or 0))
    if not by_day:
        print("⛔ 线程级 JSONL 里没有可用样本")
        return 0
    print(f"线程级样本 {n} 条，覆盖 {len(by_day)} 天：{path}")
    print()
    print(f"{'日期':<12}{'线程组':<26}{'窗口数':>7}{'均值%':>9}{'峰值%':>9}")
    for day in sorted(by_day):
        for name, vals in sorted(by_day[day].items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            avg = sum(vals) / len(vals)
            if avg < 1.0:
                continue
            print(f"{day:<12}{name:<26}{len(vals):>7}{avg:>9.2f}{max(vals):>9.2f}")
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=60.0, help="采样时长秒（0=无限）")
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔秒")
    ap.add_argument("--pid", type=int, default=None, help="额外做该 pid 的每线程归因")
    ap.add_argument("--watch", action="store_true", help="持续采样（duration=0）")
    ap.add_argument("--all", action="store_true", help="不筛进程，看全部")
    ap.add_argument("--jsonl", default=DEFAULT_JSONL)
    ap.add_argument("--quiet", action="store_true", help="不刷屏，只落盘")
    ap.add_argument("--report", action="store_true", help="只读历史出按天汇总，不采样")
    ap.add_argument("--threads-report", action="store_true",
                    help="读线程级采样（t_io/logs/thread_cpu.jsonl）出按天×线程组汇总")
    ap.add_argument("--threads-jsonl", default=THREAD_JSONL,
                    help="线程级采样文件路径（默认 t_io/logs/thread_cpu.jsonl）")
    args = ap.parse_args()

    if args.report:
        return cmd_report(args.jsonl)
    if args.threads_report:
        return cmd_report_threads(args.threads_jsonl)

    duration = 0.0 if args.watch else args.duration
    os.makedirs(os.path.dirname(args.jsonl), exist_ok=True)

    procs = list(iter_targets(args.all))
    if not procs:
        print("⚠️ 未发现相关进程（main.py / t_gui.py / gm_main.py 都没在跑？）")
    print(f"目标进程 {len(procs)} 个 ｜ 间隔 {args.interval}s ｜ "
          f"时长 {'无限' if duration == 0 else f'{duration:.0f}s'} ｜ 落盘 {args.jsonl}")

    prev: dict[int, float] = {}
    prev_th: dict[int, dict] = {}
    t0 = time.time()
    tprev = t0
    n_samples = 0
    try:
        while True:
            time.sleep(args.interval)
            now = time.time()
            dt = now - tprev
            tprev = now
            rows = snapshot(procs, prev, dt)
            threads = {}
            if args.pid:
                threads[args.pid] = thread_breakdown(args.pid, prev_th.setdefault(args.pid, {}), dt)
            n_samples += 1
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "dt": round(dt, 2), "procs": rows, "threads": threads}
            with open(args.jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if not args.quiet:
                print(fmt_report(rows, threads, dt))
            if duration and (now - t0) >= duration:
                break
    except KeyboardInterrupt:
        print("\n已停止采样")
    print(f"\n共 {n_samples} 个样本 → {args.jsonl}")
    print("按天汇总：python scripts/experiment/cpu_sampler.py --report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
