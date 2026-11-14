# -*- coding: utf-8 -*-
"""做T盯盘脚本主入口；所有依赖均通过正常 import 获取唯一模块对象。"""
import sys, os, io
# 2026-10-26（D 轮「消灭 exec 共享命名空间」回归）：正文里大量出现**裸名** `json.` / `time.` /
# `traceback.`，而顶层只 import 了 `_json` / `_time` / `_traceback` 这几个别名 ——
# 以前靠 exec 的共享 namespace 顺手带进来，改成正常 import 后全成了 NameError。
# 最严重的一处是主循环末尾的 `time.sleep(...)`：跑完第一轮就抛 NameError，
# 而 except 分支里的 `traceback.format_exc()` 同样 NameError ⇒ 进程**静默退出**
# （2026-10-26 15:07:03 实测：最后一轮日志后再无输出、进程消失）。
import time, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 确保运行路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import importlib.util as _importlib_util

# 代理彻底禁用（含 requests 全局 monkeypatch）由 config.py 在 import 时统一完成——
# main.py 此前在 import config 之前又重复了一遍，且漏 pop no_proxy（更不完整）。
# 2026-11-13 删掉这份冗余，直接复用 config 的（见 config.py 顶部「彻底禁用所有代理」块）。

from config import (
    FEISHU_KEYWORD, FEISHU_WEBHOOK, HOLDINGS,
    PARAMS,
    SIGNAL_OUTCOME_TRACKER, SYS_ALERT_AVAILABLE,
    log,
)
from core.utils import AI_REVIEW_STATS, DAILY_DECISION_STATS
from core.data_fetcher import (
    DAILY_CONTEXT_CACHE, MINUTE_FETCH_DETAIL, MINUTE_FETCH_STATUS,
    _now, cleanup_expired_minute_cache, load_holdings,
)
from core.gui_subprocess import (
    close_gui, close_turnover_widget, launch_gui, launch_turnover_widget,
)
from core.scan_timing import _log_scan_round
from core.eod_tasks import _maybe_backfill_sentiment
from core.price_alert_watch import _start_price_alert_watch
from core.hunter_watch import (
    _start_hunter_ma5_watch, _start_ma5_pullback_watch, _start_ma60_pullback_watch,
    _start_summary_regen,
)
from analysis.indicators import (
    add_indicators, add_5min_indicators, add_15min_indicators,
)
# 2026-11-13：scan_once 迁到 core/scan.py（#3 参数注入）。它原先独占的
# `_minute_issue_bucket` / `_minute_status_label` / `write_shadow_signal`（2026-10-26 D 轮
# 从 exec 共享命名空间改正常 import）以及 EOD/MA/猎手/指数/spot_vwap 那一簇 `_maybe_*`
# 调用，全部随函数搬走，main.py 不再 import。以下 SignalEngine/EngineContext 与
# add_*_indicators / MarketRegime / calc_buy_qty 仍是 test_main_import_identity 钉住的
# 「组装点」重导出，务必保留。
from core.signal_engine import EngineContext, SignalEngine
from core.market_regime import MarketRegime, detect_regime
from core.position_sizer import calc_buy_qty, calc_sell_qty

from core.scan import scan_once
from core.engine_ctx import make_engine_ctx

def run_watch():
    global HOLDINGS
    # 原地更新，保持 config.HOLDINGS、EngineContext 与 daily_sentiment 的对象同一性。
    HOLDINGS.clear()
    HOLDINGS.update(load_holdings())
    # 引擎初始化（2026-10-09 修复：此前的 `engine = SignalEngine(make_engine_ctx())`
    # 随「移除竞价模块」误删，导致 scan_once 里 engine.evaluate 报 NameError → 每只持仓"扫描异常"）
    engine = SignalEngine(make_engine_ctx())

    # manual 做T 下线（2026-09-14）：VIRTUAL_TRADES 持久化恢复与 T_MODE（正/反T）自动决策
    # 均已随 manual 做T 一并删除；run_watch 只保留持仓加载 + 热度补算 + 引擎启动。

    # 2026-10-30（owner 批准）：进程内**线程级 CPU 归因**。此前只有 GUI 挂了
    # `core.thread_watch`，而外部采样显示 main 在 :00/:30 冲到 **314%（3.1 核）**
    # （与猎手 30 分钟轮询全池重跑重合）却**没有任何线程级证据**。落同一份 JSONL，
    # 用 `proc` 字段区分（`--threads-report` 会按 proc/线程 归并）。
    try:
        from core import thread_watch as _tw
        _tw_sec = float(PARAMS.get("thread_watch_sec", 5) or 5)
        if _tw.start(interval=_tw_sec, flush_sec=60.0, label="main"):
            log.info(f"🧵 线程级 CPU 采样已启动（proc=main，{_tw_sec:g}s 采样 / 60s 落盘）")
    except Exception:
        pass

    # V3.1fix: 启动时补算昨日热度（如果缺失）
    _maybe_backfill_sentiment()
    # 2026-10-20：猎手「刚站上5日线」实时 watcher（独立线程，20s 节拍，不再等主循环）
    _start_hunter_ma5_watch()
    # 2026-10-20：回踩5日线不破 · 30 分钟轮询（体检持仓 + 建仓池 + **异常监控池**）
    _start_ma5_pullback_watch()
    # 2026-11-04：回踩60日线不破 · 同一套轮询（独立线程/独立去重/独立开关）
    _start_ma60_pullback_watch()
    # 2026-11-14：市场风向标排名 15 分钟重跑（独立线程，写回 daily_summary.json；不推飞书）
    _start_summary_regen()


    log.info("========= 做T终极护城河防御版 启动 =========")
    log.info(f"飞书推送: {'✓ 已启用' if FEISHU_WEBHOOK else '✗ 未配置'}")
    log.info(f"飞书关键词: {FEISHU_KEYWORD}")
    if FEISHU_WEBHOOK:
        log.info(f"飞书Webhook: {FEISHU_WEBHOOK[:55]}...")

    cleanup_expired_minute_cache()

    # 报警音已由 GUI 看板统一接管（Web Audio，方向区分），此处仅信息提示
    if not SYS_ALERT_AVAILABLE:
        log.info("🖥 报警音由 GUI 看板接管（Web Audio），无需 system_alert 模块")
    log.info(f"⏱ 采用顺序轮询模式：每轮扫描结束后再等待 {PARAMS['poll_interval']} 秒（V1.8 确认型收敛）")

    try:
        while True:
            cycle_start = _now()
            scan_once(engine)
            elapsed = (_now() - cycle_start).total_seconds()
            sleep_seconds = max(0, PARAMS["poll_interval"])
            log.debug(f"⏳ 本轮耗时 {elapsed:.1f}s，等待 {sleep_seconds}s 后进入下一轮")
            _log_scan_round(elapsed)      # 2026-10-16: 超阈值才 INFO 落盘（debug 那行本仓从未落过盘）
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        log.info("已停止盯盘")
    except Exception as e:
        log.error(f"❌ 盯盘主循环异常: {e}\n{traceback.format_exc()}")


def _validate_pool_split_or_exit():
    """P3-2 池分管启动校验：manual 池与 auto 池交集冲突则拒绝启动并提示（fail-closed）。"""
    try:
        _cfg = os.path.join(BASE_DIR, "config", "auto_pool.py")
        _spec = _importlib_util.spec_from_file_location("auto_pool", _cfg)
        _m = _importlib_util.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
        _conflicts = _m.validate_pool_split(
            os.path.join(BASE_DIR, "t_io", "state", "watchlist_buy.json"))
    except Exception as _e:
        log.error(f"❌ P3-2 池分管校验异常（无法确认池归属，拒绝启动）: {_e}")
        sys.exit(1)
    if _conflicts:
        log.error(f"❌ P3-2 池分管冲突：以下标的同属 manual 池与 auto 池 → {_conflicts}。"
                  f"请修正 watchlist_buy.json 的 pool 字段或 config/auto_pool.py 后重启。")
        sys.exit(1)


if __name__ == "__main__":
    import atexit
    atexit.register(close_gui)
    atexit.register(close_turnover_widget)
    # V8 预热(2026-09-08): akshare 的 py_mini_racer(V8) 首次初始化必须在主线程，否则后台扫描线程内
    # 首用（如 stock_zh_a_hist 兜底）会 FATAL 拖垮整进程——启动最先预热，见 core/v8guard.py
    try:
        from core.v8guard import prewarm_akshare_v8
        if prewarm_akshare_v8():
            print("[v8guard] py_mini_racer(V8) 已主线程预热（akshare 线程内首用不再 FATAL）")
    except Exception as _v8e:
        print(f"[v8guard] 预热失败（akshare 未用 V8 或不可用）: {_v8e}")
    # H3(2026-09-09): 启动 log 记录解释器路径——15:08/15:16 无 gm 解释器重启事故靠它二分定位
    print(f"[startup] python={sys.executable}")
    try:
        import logging as _lg
        _lg.getLogger("main").info("[startup] python=%s", sys.executable)
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "--replay-today":
        from core.replay import replay_today
        replay_today()
    elif len(sys.argv) > 1 and sys.argv[1] == "--tushare-replay":
        from core.replay import tushare_replay
        tushare_replay(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        _validate_pool_split_or_exit()
        launch_gui()
        launch_turnover_widget()
        _start_price_alert_watch()      # 价格突破提醒（K线画线 → 飞书，10s 一拍）
        run_watch()
