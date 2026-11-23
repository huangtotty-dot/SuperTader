# -*- coding: utf-8 -*-
"""选股猎手管线 · 子进程入口（2026-11-22 P2-4）。

由 gui.py 的 `run_hunter` 以
    sys.executable core/hunter_worker.py --date YYYY-MM-DD --parent-pid <pid>
拉起。为什么拆子进程（诊断报告 P2-4）：
  · 原方案在 GUI 同进程后台线程跑 1~2min 全管线——取数挤 GM facade 的
    max_workers=1 串行池（与 K线弹窗/指数板的 GM 调用互排），批算是纯 GIL 活、
    只能靠 sleep 给 UI 让路；
  · 子进程有自己独立的 GIL 与 GM 连接池，GUI 展示路径零互排。

设计要点（与 core/gui_subprocess.py 的既有纪律对齐）：
  · 管线本体**复用** `gui.Api._load_hunter_impl`（唯一真源，不在此复制逻辑）；
    `import gui` 无副作用（webview/预热线程全在 `__main__` 闸内）。
    ⚠️ 不直接跑 stock_hunter/main.py：那是 Excel 报告 + 飞书报告推送 + 背离扫描的
    CLI 管线，产出与 GUI 端 `load_hunter` 的大 payload 契约（sector_stocks /
    heat_trends / cond_labels / build_* / concept_* …）完全不同。
  · 进度：stock_hunter 管线只写进程内 `modules.market_data.MARKET_PROGRESS`，
    这里起一个守护线程每 ~0.8s 把它落成 `t_io/state/hunter_run_status.json`
    （原子写），GUI 的 `hunter_progress` 端点读该文件 ⇒ 前端契约零改动。
  · 结果：整包落 `t_io/state/hunter_run_result.json`（含 error 分支），GUI 的
    monitor 线程回收进 `HUNTER_RUN_STATE`。
  · stdout/stderr 由父进程重定向到 `t_io/logs/hunter_run_{ts}.log`（不丢 DEVNULL，
    2026-10-21 教训：秒退时输出进黑洞，主日志干净得像没事发生）。
  · 父进程看门狗：父 PID 消失（GUI 被杀/崩溃/强退，atexit 来不及跑）⇒ 自行退出，
    不留孤儿写状态文件。与 GUI 侧 atexit terminate 互为双保险。

（2026-11-23：上述公共件已抽至 core/subprocess_job.py 供 review_worker 复用；
本文件保持原样以守住 P2-4 测试基线，后续可迁移到 subprocess_job——纯删重复，不改行为。）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)   # 让 `import gui` 可解析（worker 由仓库根拉起，双保险）

# Windows 终端 UTF-8 修复（与 stock_hunter/main.py 同款）：阶段文案含中文/emoji
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_STATUS_FP = os.path.join(_ROOT, "t_io", "state", "hunter_run_status.json")
_RESULT_FP = os.path.join(_ROOT, "t_io", "state", "hunter_run_result.json")


def _atomic_write_json(fp, obj) -> None:
    """tmp + os.replace 原子落盘；目录缺失自动补。失败抛给调用方决定。"""
    os.makedirs(os.path.dirname(str(fp)), exist_ok=True)
    tmp = str(fp) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, str(fp))


def _pid_alive(pid) -> bool:
    """父进程是否活着。探测失败一律当「活着」——误杀（猎手白跑）比孤儿更糟。"""
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


def _parent_watch(pid, interval=2.0):
    """父进程消失 ⇒ 自杀。GUI 被 terminate/kill 时 atexit 不会跑，只能靠这条。"""
    while True:
        time.sleep(interval)
        if not _pid_alive(pid):
            try:
                print("[hunter_worker] 父进程已退出，子进程随之退出", flush=True)
            except Exception:
                pass
            os._exit(3)


def _status_writer(stop, date, status_fp, interval=0.8):
    """把进程内 MARKET_PROGRESS 周期落成状态文件（GUI 端 hunter_progress 读它）。

    内容没变就跳过写盘（90% 的 tick 是重复内容，省小文件写放大）。
    """
    last = ""
    while not stop.is_set():
        try:
            from modules.market_data import MARKET_PROGRESS as _MP
            payload = {"running": True, "date": date, "pid": os.getpid(),
                       "phase": _MP.get("phase", ""), "done": int(_MP.get("done") or 0),
                       "total": int(_MP.get("total") or 0), "msg": _MP.get("msg", ""),
                       "ts": time.time()}
            s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if s != last:
                _atomic_write_json(status_fp, payload)
                last = s
        except Exception:
            pass
        stop.wait(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="选股猎手管线子进程（由 gui.py run_hunter 拉起）")
    ap.add_argument("--date", required=True, help="扫描日期 YYYY-MM-DD")
    ap.add_argument("--parent-pid", type=int, default=0, help="GUI 父进程 PID（看门狗用）")
    ap.add_argument("--status-file", default=_STATUS_FP)
    ap.add_argument("--result-file", default=_RESULT_FP)
    args = ap.parse_args()

    if args.parent_pid:
        threading.Thread(target=_parent_watch, args=(args.parent_pid,), daemon=True,
                         name="parent-watch").start()

    print(f"[hunter_worker] 启动: date={args.date} pid={os.getpid()} "
          f"parent={args.parent_pid}", flush=True)

    # import gui 无副作用（webview/预热线程全在 __main__ 闸内）；它顺带把
    # stock_hunter/ 插进 sys.path，`modules.*` 由此可解析。
    import gui

    stop = threading.Event()
    threading.Thread(target=_status_writer, args=(stop, args.date, args.status_file),
                     daemon=True, name="status-writer").start()

    rc = 0
    try:
        res = gui.Api()._load_hunter_impl(args.date)
        if not isinstance(res, dict):
            res = {"available": False, "date": args.date,
                   "error": f"管线返回类型异常: {type(res).__name__}"}
            rc = 1
    except Exception as e:
        res = {"available": False, "date": args.date,
               "error": f"猎手子进程未捕获异常: {type(e).__name__}: {str(e)[:200]}"}
        rc = 1
    stop.set()

    try:
        _atomic_write_json(args.result_file, {
            "date": args.date, "ok": rc == 0, "result": res,
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        # 结果文件**先**落、状态文件**后**落：GUI monitor 以结果文件为准，
        # 状态文件只是进度条的最后定格。
        _atomic_write_json(args.status_file, {
            "running": False, "date": args.date, "pid": os.getpid(),
            "phase": "完成" if rc == 0 else "失败", "done": 1, "total": 1,
            "msg": ("选股猎手运行完成" if rc == 0
                    else str(res.get("error", ""))[:120]),
            "ts": time.time()})
    except Exception as e:
        print(f"[hunter_worker] 结果落盘失败: {e}", flush=True)
        rc = 2
    print(f"[hunter_worker] 结束: rc={rc} available={res.get('available')} "
          f"error={str(res.get('error') or '')[:80]}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
