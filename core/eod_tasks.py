# -*- coding: utf-8 -*-
"""收盘后（EOD）任务簇（2026-11-13 从 main.py 抽出）：情绪子进程 / 通达信日线校准 / regime EOD 落库。

这几个任务都是「15:30/15:40 后交易日每日一次」的收盘后任务，与盯盘热路径（scan_once
持仓扫描）无共享引擎状态 —— 唯一耦合是 `log`/`_now`/`BASE_DIR`/daily_sentiment 的
导入函数。故整簇搬到本模块，`main.py` 只留调用点。

子进程隔离背景：补算链会触发 akshare 新浪/东财接口，首次使用 py_mini_racer(V8) 初始化时
若在后台线程执行会 FATAL 崩溃拖垮盯盘主进程（2026-08-24 事故），故强制独立进程。
"""
from __future__ import annotations

import json
import os
import sys
import threading as _threading
from datetime import datetime, timedelta, time as dtime

from config import log
from core.data_fetcher import _now
from execution.daily_sentiment import (
    compute_daily_sentiment, load_sentiment_history, save_sentiment_record,
    sentiment_log_dir,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2026-10-19：收盘后 EOD 热度/regime 当日落库（修「跨日 S 图停在上一交易日」）
_sentiment_eod_state = {"date": "", "attempts": 0, "next_try": 0.0}
# 2026-10-30：通达信日线校准的「当日已跑」标记（进程内；跨进程标记在 core.tdx_calibrate）
_tdx_cal_state = {"date": ""}


def _launch_sentiment_subprocess(date_str: str, mode: str = "eod", tag: str = "sentiment_backfill"):
    """子进程跑 daily_sentiment.py 独立 CLI（默认 --mode eod --no-push）。

    子进程隔离：补算链会触发 akshare 新浪/东财接口，首次使用 py_mini_racer(V8) 初始化时
    若在后台线程执行会 FATAL 崩溃拖垮盯盘主进程（2026-08-24 事故），故强制独立进程。

    ⚠️ 2026-10-19 修：脚本按**绝对路径**启动时 `sys.path[0]=execution/`，`import core`
    必然 `ModuleNotFoundError`（10-12~10-16 的 sentiment_backfill 日志全在报）⇒ 显式
    `PYTHONPATH=_ROOT`。同日新增 mode 入参：收盘后 EOD 落库复用同一条链路。
    """
    import subprocess
    script = os.path.join(_ROOT, "execution", "daily_sentiment.py")
    log_dir = os.path.join(_ROOT, "t_io", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_fp = open(os.path.join(log_dir, f"{tag}_{date_str}.log"), "a", encoding="utf-8")
    _env = os.environ.copy()
    _env["PYTHONPATH"] = _ROOT + (
        os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [sys.executable, script, "--date", date_str, "--mode", mode, "--no-push"],
            cwd=_ROOT, env=_env, creationflags=flags,
            stdout=out_fp, stderr=subprocess.STDOUT,
        )
    finally:
        out_fp.close()   # 子进程已持有句柄副本，父进程立即关闭
    log.info(f"📡 热度子进程已启动({mode}): {date_str} (pid={proc.pid}) → t_io/logs/{tag}_{date_str}.log")
    return proc


def _launch_sentiment_backfill(date_str: str) -> None:
    _launch_sentiment_subprocess(date_str, "eod", "sentiment_backfill")


def _maybe_backfill_sentiment():
    """启动时检查最近一个交易日的 sentiment 数据是否存在，缺失则后台补算。

    目标日 = 昨天起向前跳过周末（08-24 周一不会对周日 08-23 做无意义补算）。
    补算在独立子进程执行（_launch_sentiment_backfill），避免主进程后台线程
    首次初始化 akshare→py_mini_racer(V8) 崩溃连累盯盘主循环。"""
    try:
        if 'load_sentiment_history' in globals():
            hist = load_sentiment_history() or []
        else:
            hist = []
        target = _now() - timedelta(days=1)
        while target.weekday() >= 5:   # 跳过周末（简单交易日近似；节假日不影响进程安全）
            target -= timedelta(days=1)
        target_str = target.strftime("%Y-%m-%d")
        has_target = any(str(r.get("date")) == target_str for r in hist if isinstance(r, dict))
        if has_target:
            return
        log.info(f"📡 缺失{sentiment_log_dir()}: {target_str}热度记录，启动后台补算...")
        if 'compute_daily_sentiment' in globals() and 'save_sentiment_record' in globals():
            _launch_sentiment_backfill(target_str)
    except Exception as e:
        log.warning(f"⚠️ backfill_sentiment 异常: {str(e)[:100]}")


def _regime_state_last_date() -> str:
    """读 index_regime/state.json 的最后一交易日（失败返回空串）。"""
    try:
        fp = os.path.join(_ROOT, "t_io", "index_regime", "state.json")
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        h = d.get("history") or []
        return str((h[-1] or {}).get("date") or "") if h else ""
    except Exception:
        return ""


def _maybe_run_tdx_calibrate(now: datetime) -> None:
    """15:40 后交易日每日一次：用**通达信本地 `.day`** 校准日线缓存（2026-10-30 owner）。

    背景：301079 邵阳液压缓存末行停在 10-26 而通达信/同花顺已到 10-30 —— 中间四根被画成
    "一根从 26 跳到 36.79 的巨柱"；另有 000006 这类**盘中快照根**（量只有真实的 10%）
    一直没被真实收盘覆盖（腾讯 fqkline 被 WAF 挡、GM 不可达时尤其明显）。
    `core.tdx_calibrate` 会按"锚点（最后一天量对得上的重叠日）之前只读、之后按通达信重写"修复；
    跨进程标记保证一天只跑一次。找不到通达信目录时静默跳过。
    """
    try:
        from core import tdx_calibrate as _tc
        _st = _tdx_cal_state
        today = now.strftime("%Y-%m-%d")
        if _st.get("date") == today:
            return
        if now.weekday() >= 5 or now.strftime("%H:%M") < "15:40":
            return
        _st["date"] = today

        def _work():
            try:
                rep = _tc.run_daily_once(fix=True, now=now)
                if rep is None:
                    return
                if rep.get("missing_home"):
                    log.info("通达信日线校准：未找到 vipdoc，跳过")
                    return
                log.info("通达信日线校准：%s（落后 %d 只 / 比率异常 %d 只 / 历史偏短 %d 只）",
                         rep.get("summary"), len(rep.get("behind") or []),
                         len(rep.get("ratio_bad") or []), len(rep.get("short") or []))
                # 2026-10-30：历史偏短的票用长窗口补（每天限量，约 13 天补齐 5147 只）。
                # 数据源不可用（腾讯 fqkline 被 WAF、GM 不可达）时 `deepen_daily` 会先探针后直接跳过。
                short = sorted((rep.get("short") or []),
                               key=lambda r: -(int(r.get("tdx_rows") or 0)
                                               - int(r.get("cache_rows") or 0)))
                if short:
                    from core import chart_cache as _cc
                    st = _cc.deepen_daily([r["code"] for r in short], limit=600, days=800, now=now)
                    log.info("日线补长历史：%s", st)
            except Exception as e:
                log.warning("通达信日线校准失败：%s", e)
            # 2026-11-03（owner：「11-02 的 K 线明显不对」）：通达信 `.day` 没更新到当天时，
            # 当天早盘的**快照根**会一直挂着 forming，次日被当成收盘。这里用腾讯日线兜底：
            # 找出「过去某天仍标 forming」的缓存，逐只重拉真数据覆盖（最多 3000 只/天）。
            try:
                from core import daily_repair as _dr
                _log_fp = os.path.join(_ROOT, "t_io", "logs",
                                       f"fix_forming_{now.strftime('%Y%m%d')}.jsonl")
                _st = _dr.fix_forming_tails(workers=4, limit=3000, log_fp=_log_fp)
                if _st.get("total"):
                    log.info("日线 forming 根修复：%s", _st)
            except Exception as e:
                log.warning("日线 forming 根修复失败：%s", e)

        _threading.Thread(target=_work, daemon=True, name="tdx-calibrate").start()
    except Exception:
        pass


def _maybe_run_sentiment_eod(now: datetime) -> None:
    """15:30 后交易日每日一次：当日 EOD 热度/regime 落库。

    为什么必须有：14:30 尾盘钩子用 mode="tail"，模块**明确不写** state/trace
    （保持 EOD 状态机纯净），而 state.json 是「大盘跨日 S」图的唯一数据源 ⇒
    没有这条，当日 S 只能等**次日启动**的 morning 推送补"上一完成日"
    （2026-10-19 实测：图停在 10-16）。子进程隔离 + 失败 10 分钟重试（≤3 次/日）。
    """
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(15, 30) <= t <= dtime(17, 0)):
            return
        today = now.strftime("%Y-%m-%d")
        if _regime_state_last_date() >= today:     # 已落库（ISO 日期可直接比较）
            return
        st = _sentiment_eod_state
        if st.get("date") != today:
            st.update({"date": today, "attempts": 0, "next_try": 0.0})
        if int(st.get("attempts", 0)) >= 3:
            return
        if _now().timestamp() < float(st.get("next_try", 0.0)):
            return
        st["attempts"] = int(st.get("attempts", 0)) + 1
        st["next_try"] = _now().timestamp() + 600
        _launch_sentiment_subprocess(today, "eod", "sentiment_eod")
    except Exception as e:
        log.warning(f"⚠️ 收盘 EOD 热度/regime 落库失败（10 分钟重试）: {str(e)[:120]}")
