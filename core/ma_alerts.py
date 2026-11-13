# -*- coding: utf-8 -*-
"""均线报警调度器（2026-11-13 从 main.py 抽出）：破5/10日线 / 站上5/10日线 / 30min 趋势翻转。

三者共用同一套「交易时段 + 每 5 分钟节流 + 单飞线程」调度模式，破线/站上还共用
`_ma_alert_lock`（2026-10-09 从建仓扫描锁独立出来，因扫描会持锁数分钟 ⇒ 报警每次都
120s 超时从未推送过）。与盯盘热路径零引擎状态耦合，只依赖 config(log/load_runtime_config)
+ core.position_builder 的 run_ma_break_alert/run_ma_reclaim_alert + core.trend30_alert。
"""
from __future__ import annotations

import threading as _threading
from datetime import datetime, time as dtime

from config import load_runtime_config, log

# 建仓扫描可用性守卫（与 main.py 顶部同源：position_builder 加载失败时这三路报警也不可用）
try:
    from core.position_builder import run_position_scan as _run_position_scan
except Exception:
    _run_position_scan = None

# 2026-10-09：破线/站上日均线报警**独立的锁**。原先两者与建仓扫描共用 `_position_scan_lock`，
# 而盘中建仓扫描会持锁数分钟 ⇒ 报警每次都等 120s 超时、**从未推送过**（实测日志
# 「⚠️ 站上均线报警等待建仓扫描释放锁超时」）。两者互斥即可（读写行情，不再抢扫描的锁）。
_ma_alert_lock = _threading.Lock()
_ma_break_last = None  # 破5/10日线报警节流（datetime，仿盘中建仓扫描）
_ma_break_thread = None  # 破5/10日线报警后台线程
_ma_reclaim_last = None  # 站上5/10日线（破线回站）报警节流（2026-10-08）
_ma_reclaim_thread = None  # 站上5/10日线报警后台线程
_trend30_alert_last = None  # 30min 趋势翻转告警节流（2026-10-04 方案 §4.6）
_trend30_alert_thread = None  # 30min 趋势翻转告警后台线程


def _ma_break_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled + notify_on_ma_break 开关。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get("notify_on_ma_break", True))
    except Exception:
        return True


def _maybe_run_ma_break_alert(now: datetime) -> None:
    """盘中破5/10日线报警（每5分钟，刚跌破事件即飞书提醒建仓，每只每天只推一次）。

    与盘中建仓扫描同节奏/同互斥锁；run_ma_break_alert 内部按 (code,date) 状态文件去重。
    """
    global _ma_break_last, _ma_break_thread
    try:
        if not _ma_break_feishu_enabled():
            return
    except Exception:
        return
    if _run_position_scan is None:  # position_builder 未加载时不可用
        return
    t = now.time()
    if now.weekday() >= 5:
        return
    in_morning = dtime(9, 30) <= t <= dtime(11, 30)
    in_afternoon = dtime(13, 0) <= t <= dtime(14, 55)
    if not (in_morning or in_afternoon):
        return
    if _ma_break_last is not None:
        if (now - _ma_break_last).total_seconds() < 300:
            return
    if _ma_break_thread is not None and _ma_break_thread.is_alive():
        log.debug("⏳ 上一轮破线报警仍在进行，跳过本轮")
        return
    _ma_break_last = now

    today = now.strftime("%Y-%m-%d")

    def _worker(day: str) -> None:
        try:
            from core.position_builder import run_ma_break_alert as _run_ma_break_alert
            if not _ma_alert_lock.acquire(timeout=120):
                log.warning("⚠️ 破线报警等待均线报警锁超时，本轮跳过（下轮重试）")
                return
            try:
                pushed = _run_ma_break_alert(date_str=day, silent=True)
            finally:
                _ma_alert_lock.release()
            for e in pushed:
                log.info(f"⚠️ 破5/10日线报警: {e['code']} {e['name']} "
                         f"现价{e.get('price')} MA5={e.get('ma5')} MA10={e.get('ma10')}")
        except Exception as ex:
            log.warning(f"⚠️ 破线报警异常（已吞掉）: {str(ex)[:200]}")

    _ma_break_thread = _threading.Thread(
        target=_worker, args=(today,), name="ma-break-alert", daemon=True)
    _ma_break_thread.start()


def _ma_reclaim_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled + notify_on_ma_reclaim 开关。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get("notify_on_ma_reclaim", True))
    except Exception:
        return True


def _maybe_run_ma_reclaim_alert(now: datetime) -> None:
    """盘中**站上5/10日线**（破线回站）报警（每5分钟，每只每天只推一次）。

    对称于 `_maybe_run_ma_break_alert`：owner 2026-10-08 要求——破线有飞书，回站上也要通知。
    """
    global _ma_reclaim_last, _ma_reclaim_thread
    try:
        if not _ma_reclaim_feishu_enabled():
            return
    except Exception:
        return
    if _run_position_scan is None:
        return
    t = now.time()
    if now.weekday() >= 5:
        return
    in_morning = dtime(9, 30) <= t <= dtime(11, 30)
    in_afternoon = dtime(13, 0) <= t <= dtime(14, 55)
    if not (in_morning or in_afternoon):
        return
    if _ma_reclaim_last is not None:
        if (now - _ma_reclaim_last).total_seconds() < 300:
            return
    if _ma_reclaim_thread is not None and _ma_reclaim_thread.is_alive():
        return
    _ma_reclaim_last = now
    today = now.strftime("%Y-%m-%d")

    def _worker(day: str) -> None:
        try:
            from core.position_builder import run_ma_reclaim_alert as _run_reclaim
            if not _ma_alert_lock.acquire(timeout=120):
                log.warning("⚠️ 站上均线报警等待均线报警锁超时，本轮跳过（下轮重试）")
                return
            try:
                pushed = _run_reclaim(date_str=day, silent=True)
            finally:
                _ma_alert_lock.release()
            for e in pushed:
                log.info(f"🔺 站上5/10日线报警: {e['code']} {e['name']} "
                         f"现价{e.get('price')} MA5={e.get('ma5')} MA10={e.get('ma10')}")
        except Exception as ex:
            log.warning(f"⚠️ 站上均线报警异常（已吞掉）: {str(ex)[:200]}")

    _ma_reclaim_thread = _threading.Thread(
        target=_worker, args=(today,), name="ma-reclaim-alert", daemon=True)
    _ma_reclaim_thread.start()


def _trend30_alert_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled + notify_on_trend30 开关。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get("notify_on_trend30", True))
    except Exception:
        return True


def _maybe_check_trend30_alert(now: datetime) -> None:
    """30min 趋势状态翻转告警（每 5 分钟，翻转即飞书；2026-10-04 方案 §4.6）。纯通知。

    持有(手动) + 自选标的逐码比较 30min 状态机最新状态；BULL↔BEAR 或 →RANGE 即推。
    """
    global _trend30_alert_last, _trend30_alert_thread
    try:
        if not _trend30_alert_feishu_enabled():
            return
    except Exception:
        return
    t = now.time()
    if now.weekday() >= 5:
        return
    in_morning = dtime(9, 30) <= t <= dtime(11, 30)
    in_afternoon = dtime(13, 0) <= t <= dtime(14, 55)
    if not (in_morning or in_afternoon):
        return
    if _trend30_alert_last is not None:
        if (now - _trend30_alert_last).total_seconds() < 300:
            return
    if _trend30_alert_thread is not None and _trend30_alert_thread.is_alive():
        return
    _trend30_alert_last = now

    def _worker() -> None:
        try:
            from core.trend30_alert import run_trend30_alert
            events = run_trend30_alert()
            for e in events:
                log.info(f"🔀 30min趋势翻转: {e['code']} {e['name']} "
                         f"{e['from']}→{e['to']} ADX={e.get('adx')}")
        except Exception as ex:
            log.warning(f"⚠️ 30min趋势告警异常（已吞掉）: {str(ex)[:200]}")

    _trend30_alert_thread = _threading.Thread(
        target=_worker, name="trend30-alert", daemon=True)
    _trend30_alert_thread.start()
