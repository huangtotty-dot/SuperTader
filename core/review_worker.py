# -*- coding: utf-8 -*-
"""每日大盘复盘（LLM）· 子进程入口（2026-11-23）。

由 gui.py 的 `run_daily_review` 以
    sys.executable core/review_worker.py --date YYYY-MM-DD --parent-pid <pid>
拉起。为什么拆子进程（与 P2-4 猎手同型）：
  · 原方案在 GUI 同进程后台线程跑：LLM 网络挂死（连接不超时/流式半挂）会长期
    占着 GUI 线程池，且数据收集 + 流式生成全程（1~10min）与 GUI 抢同一个 GIL；
  · 子进程有独立 GIL/网络栈，挂死至多占住自己——父进程 monitor 带硬超时
    （subprocess_job.wait_with_timeout）强杀，GUI 线程池零占用。

设计要点（骨架公共件在 core/subprocess_job.py，与 hunter_worker 同源）：
  · 复盘本体**复用** `core.market_review.run_market_review_stream`（唯一真源，
    算法零改动）。market_review 只依赖 json/urllib/pandas + t_io 路径，
    **不耦合 gui 内部状态** ⇒ 无需 import gui（hunter_worker 是要复用
    gui.Api._load_hunter_impl 才 import，那是它的耦合钩子，这里不需要）。
  · 流式文本：run_market_review_stream 的 on_text 回调（LLM token 粒度，高频）
    只累积进内存缓冲；守护线程每 ~0.5s 把缓冲落成
    `t_io/state/review_run_status.json`（**文本没变就跳过写盘**——LLM 思考
    停顿期一秒几十拍全是重复内容，省小文件写放大；hunter 版连 ts 一起比较
    实际上每拍都写，这里修正为只比文本）。GUI 的 daily_review_progress 读档
    返回，前端契约（800ms 轮询 {running, error, output}）零改动。
  · 配置：**不在命令行传 api_key**（进程列表对同机任何用户可见）——父进程先
    save_llm_config 落盘，worker 启动后 load_llm_config() 读回；时序上
    save 一定先于 Popen，无竞态。
  · 结果：整包落 `t_io/state/review_run_result.json`（含 error 分支），
    **先结果后状态**（monitor 以结果档为准，状态档只是流式文本的最后定格）。
  · stdout/stderr 由父进程重定向到 `t_io/logs/review_run_{ts}.log`（不丢
    DEVNULL，2026-10-21 教训）。
  · 父进程看门狗：父 PID 消失 ⇒ subprocess_job 看门狗自杀，不留孤儿继续烧
    LLM token。与 GUI 侧 atexit terminate 互为双保险。
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
    sys.path.insert(0, _ROOT)   # 让 `core.*` 可解析（worker 由仓库根拉起，双保险）

from core import subprocess_job as _sj

_STATUS_FP = os.path.join(_ROOT, "t_io", "state", "review_run_status.json")
_RESULT_FP = os.path.join(_ROOT, "t_io", "state", "review_run_result.json")

# 流式缓冲：on_text 回调只改这块内存（加锁，LLM 回调线程 vs status-writer 线程），
# 落盘交给 _status_writer —— 回调里直接写盘会把 token 粒度的高频写放大到磁盘。
_STREAM = {"output": ""}
_STREAM_LOCK = threading.Lock()


def _on_text(t) -> None:
    with _STREAM_LOCK:
        _STREAM["output"] = (_STREAM["output"] or "") + t


def _status_writer(stop, date, status_fp, interval=0.5) -> None:
    """把流式缓冲周期落成状态档（GUI 端 daily_review_progress 读它）。

    只比文本内容，没变就跳过写盘（见模块 docstring）。
    """
    last = None
    while not stop.is_set():
        try:
            with _STREAM_LOCK:
                out = _STREAM["output"]
            if out != last:
                _sj.atomic_write_json(status_fp,
                                      {"running": True, "date": date, "pid": os.getpid(),
                                       "output": out, "ts": time.time()})
                last = out
        except Exception:
            pass
        stop.wait(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="每日大盘复盘子进程（由 gui.py run_daily_review 拉起）")
    ap.add_argument("--date", required=True, help="复盘日期 YYYY-MM-DD")
    ap.add_argument("--parent-pid", type=int, default=0, help="GUI 父进程 PID（看门狗用）")
    ap.add_argument("--status-file", default=_STATUS_FP)
    ap.add_argument("--result-file", default=_RESULT_FP)
    args = ap.parse_args()

    _sj.utf8_console_fix()
    if args.parent_pid:
        _sj.start_parent_watchdog(args.parent_pid, tag="review_worker")

    print(f"[review_worker] 启动: date={args.date} pid={os.getpid()} "
          f"parent={args.parent_pid}", flush=True)

    # 延迟 import：--help/参数错误等秒退路径不背 pandas/market_data 的 import 成本；
    # 且 mock 友好（测试可在 sys.modules 注入假 core.market_review）。
    from core.market_review import load_llm_config, run_market_review_stream
    cfg = load_llm_config()   # 父进程 save_llm_config 已落盘；api_key 不上命令行

    # 流式缓冲复位：worker 语义上一次性，但同进程重复 main()（测试注入场景）要保证结果确定
    with _STREAM_LOCK:
        _STREAM["output"] = ""

    stop = threading.Event()
    threading.Thread(target=_status_writer, args=(stop, args.date, args.status_file),
                     daemon=True, name="status-writer").start()

    rc, err = 0, None
    try:
        run_market_review_stream(args.date, cfg, _on_text)
    except Exception as e:
        err = f"复盘子进程未捕获异常: {type(e).__name__}: {str(e)[:300]}"
        rc = 1
    stop.set()
    with _STREAM_LOCK:
        final_out = _STREAM["output"] or ""

    try:
        _sj.atomic_write_json(args.result_file, {
            "date": args.date, "ok": rc == 0,
            "result": {"output": final_out, "error": err},
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        # 结果文件**先**落、状态文件**后**落：GUI monitor 以结果文件为准，
        # 状态文件只是流式文本的最后定格（与 hunter_worker 同序防竞态）。
        _sj.atomic_write_json(args.status_file, {
            "running": False, "date": args.date, "pid": os.getpid(),
            "output": final_out, "error": err, "ts": time.time()})
    except Exception as e:
        print(f"[review_worker] 结果落盘失败: {e}", flush=True)
        rc = 2
    print(f"[review_worker] 结束: rc={rc} error={str(err or '')[:80]}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
