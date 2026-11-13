# -*- coding: utf-8 -*-
"""建仓信号扫描调度器（2026-11-13 从 main.py 抽出）：盘中实时 + 收盘 EOD。

两者共用 `_position_scan_lock`（盘中/收盘扫描互斥，保证 trace 写盘线程安全）与
`_run_position_scan`（position_builder 加载失败时二者都不可用）。与盯盘热路径零引擎
状态耦合，只依赖 config.log + core.position_builder.run_position_scan。
"""
from __future__ import annotations

import threading as _threading
from datetime import datetime, time as dtime

from config import log

# 建仓扫描可用性守卫（与 main.py 顶部同源：position_builder 加载失败时建仓扫描不可用）
try:
    from core.position_builder import run_position_scan as _run_position_scan
except Exception:
    _run_position_scan = None

_position_builder_push_date = ""  # 模块级：收盘后建仓扫描防重复
_position_builder_intraday_last = None  # 盘中建仓扫描节流（datetime）
_position_builder_intraday_pushed = set()  # 当日已推送的股票代码（防重复推送）
_position_builder_eod_last_attempt = None  # eod 扫描失败重试节流（datetime）
_position_builder_intraday_thread = None  # 盘中建仓扫描后台线程（fix P1-5）
_position_scan_lock = _threading.Lock()  # 盘中/收盘建仓扫描互斥（trace 写盘线程安全）


def _maybe_run_position_builder_intraday(now: datetime) -> None:
    """盘中实时建仓信号扫描（每 5 分钟，signal 股即时飞书推送，每只每天只推一次）。

    fix P1-5: 扫描移入独立线程执行，避免阻塞做T主循环；与收盘档扫描互斥保证 trace 写盘线程安全。
    """
    global _position_builder_intraday_last, _position_builder_intraday_pushed, _position_builder_intraday_thread
    if _run_position_scan is None:
        return
    t = now.time()
    # 仅交易时段: 9:30-11:30, 13:00-14:55（09:30 开盘即扫，竞价期间9:15-9:25无分钟线不扫）
    if now.weekday() >= 5:
        return
    in_morning = dtime(9, 30) <= t <= dtime(11, 30)
    in_afternoon = dtime(13, 0) <= t <= dtime(14, 55)
    if not (in_morning or in_afternoon):
        return
    # 节流：每 5 分钟最多跑一次
    if _position_builder_intraday_last is not None:
        if (now - _position_builder_intraday_last).total_seconds() < 300:
            return
    # fix P1-5: 上一轮扫描线程未结束则跳过本轮（不推进节流时间戳，下一轮重试）
    if _position_builder_intraday_thread is not None and _position_builder_intraday_thread.is_alive():
        log.debug("⏳ 上一轮盘中建仓扫描仍在进行，跳过本轮")
        return
    _position_builder_intraday_last = now

    # 日期翻转时清空推送记录
    today = now.strftime("%Y-%m-%d")
    if not hasattr(_maybe_run_position_builder_intraday, "_date") or \
       _maybe_run_position_builder_intraday._date != today:
        _maybe_run_position_builder_intraday._date = today
        _position_builder_intraday_pushed.clear()

    def _scan_worker(day: str) -> None:
        try:
            # 与收盘档扫描互斥，保证 trace 写盘线程安全
            with _position_scan_lock:
                results = _run_position_scan(date_str=day, silent=True, scan_type="intraday")
            for r in results:
                if r.get("verdict") == "signal" and r["code"] not in _position_builder_intraday_pushed:
                    _position_builder_intraday_pushed.add(r["code"])
                    log.info(f"🏗️ 盘中建仓信号触发: {r['code']} {r['name']} 得分={r['composite_score']}")
        except Exception as e:
            log.warning(f"⚠️ 盘中建仓扫描异常（已吞掉）: {str(e)[:200]}")

    _position_builder_intraday_thread = _threading.Thread(
        target=_scan_worker, args=(today,), name="position-scan-intraday", daemon=True)
    _position_builder_intraday_thread.start()


def _maybe_run_position_builder(now: datetime) -> None:
    """收盘后（15:05 起）每日一次建仓信号扫描 + 盘后汇总飞书推送。
    fix P0-15关联(收盘档断供): 原 15:05-15:15 硬窗口在进程休眠/重启时整日断供（08-07 复盘：
    15:00:02 进入低频保活后进程疑似休眠，错过窗口；且异常时日期占位已写入导致当日不重试）。
    改为 15:05 后任意时刻补扫一次（限交易日）；扫描成功才占位，失败 10 分钟后重试。
    """
    global _position_builder_push_date, _position_builder_eod_last_attempt
    if _run_position_scan is None:
        return
    if now.weekday() >= 5:  # fix: 周末不触发（原逻辑周末 15:05 也会扫）
        return
    today = now.strftime("%Y-%m-%d")
    if _position_builder_push_date == today:
        return
    t = now.time()
    if t < dtime(15, 5):
        return
    # 失败重试节流：10 分钟（防持续异常刷屏）
    if _position_builder_eod_last_attempt is not None and \
       (now - _position_builder_eod_last_attempt).total_seconds() < 600:
        return
    _position_builder_eod_last_attempt = now
    _catchup = t > dtime(15, 15)  # 超过原窗口视为补扫（进程休眠/重启兜底）
    # fix P1-5: 与盘中扫描线程互斥，保证 trace 写盘线程安全；带超时防盘中线程卡死拖住主循环
    if not _position_scan_lock.acquire(timeout=120):
        log.warning("⚠️ 建仓信号扫描等待盘中扫描释放锁超时，本轮跳过（下轮重试）")
        return
    try:
        results = _run_position_scan(date_str=today, silent=True, scan_type="eod")
        _position_builder_push_date = today  # 扫描成功才占位（原逻辑异常也占位 → 当日断供）
        signals = [r for r in results if r.get("verdict") == "signal"]
        _tag = "（补扫）" if _catchup else ""
        if signals:
            log.info(f"🏗️ 建仓信号扫描完成{_tag}: {len(signals)} 只触发 signal, "
                     f"{len(results) - len(signals)} 只未满足")
        else:
            log.info(f"🏗️ 建仓信号扫描完成{_tag}: 0/{len(results)} 只触发 signal")

    except Exception as e:
        log.warning(f"⚠️ 建仓信号扫描异常（已吞掉，10分钟后重试）: {str(e)[:200]}")
    finally:
        _position_scan_lock.release()
