# -*- coding: utf-8 -*-
"""猎手/异常监控池 watcher + 轮询调度器（2026-11-13 从 main.py 抽出）。

三条独立 daemon 线程（都不挂主循环，因 main.py 一轮 scan_once 实测 ~15 分钟，挂上去会把
5/30 分钟节拍稀释掉）：
  ① `_start_hunter_ma5_watch`  —— 异常监控池「刚站上5日线」实时播报（20s，universe="pool"）；
  ② `_start_ma5_pullback_watch` —— 回踩5日线不破轮询（30min，体检持仓+建仓池+异常监控池）；
  ③ `_start_ma60_pullback_watch`—— 回踩60日线不破轮询（30min，同源 PROFILE_MA60）；
外加 `_maybe_run_hunter_poll` —— 选股猎手 30 分钟轮询 + **异常必发飞书**（后台线程）。

与盯盘热路径零引擎状态耦合，只依赖 config(PARAMS/load_runtime_config/log)，业务本体全部
懒加载 core.ma5_pullback / core.hunter_ma5_alert / core.hunter_poll。
"""
from __future__ import annotations

import threading as _threading
from datetime import datetime

from config import PARAMS, load_runtime_config, log

_hunter_ma5_watch_thread = None  # 猎手「刚站上5日线」实时 watcher（2026-10-20 独立线程）
_ma5_pullback_thread = None
_ma60_pullback_thread = None
_hunter_poll_last = None      # 节流（30 分钟一轮）
_hunter_poll_thread = None    # 单飞（上一轮没跑完不开新轮）
_summary_regen_thread = None  # 风向标排名 15 分钟重跑（单飞）


def _hunter_ma5_feishu_enabled(key: str = "notify_on_hunter_ma5") -> bool:
    """读取 config.json 的 feishu.enabled + 指定 notify_on_* 开关（默认猎手站上5日线）。

    2026-10-20：「回踩5日线不破」复用同一把开关，`key="notify_on_ma5_pullback"`。
    """
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get(key, True))
    except Exception:
        return True


def _start_ma5_pullback_watch() -> None:
    """2026-10-20 owner：「每天 30 分钟轮询，把回踩 5 日线不破的票发飞书」
    —— 范围 = 体检持仓 + 建仓池 + **异常监控池**（2026-10-22 起取代「猎手前 10 热门板块」）。

    独立 daemon 线程（**不挂主循环**）：main.py 一轮可达 15 分钟，挂上去会把 30 分钟
    稀释成 30+；这与「猎手站上5日线」watcher 同样的理由（见内存主循环节流那条）。
    开关：feishu.enabled + feishu.notify_on_ma5_pullback。
    """
    global _ma5_pullback_thread
    try:
        if not _hunter_ma5_feishu_enabled("notify_on_ma5_pullback"):
            log.info("📉 回踩5日线不破轮询: 已在配置中关闭（feishu.notify_on_ma5_pullback）")
            return
        if _ma5_pullback_thread is not None and _ma5_pullback_thread.is_alive():
            return
        from core.ma5_pullback import run_loop, POLL_SEC
        _interval = float(PARAMS.get("ma5_pullback_poll_sec", POLL_SEC) or POLL_SEC)
        _ma5_pullback_thread = _threading.Thread(
            target=run_loop, kwargs={"poll_sec": _interval},
            name="ma5-pullback-watch", daemon=True)
        _ma5_pullback_thread.start()
        log.info("📉 回踩5日线不破轮询已启动（独立线程，每 %.0f 分钟，交易时段）" % (_interval / 60.0))
    except Exception as e:
        log.warning(f"⚠️ 回踩5日线不破轮询启动失败（不阻断主循环）: {str(e)[:160]}")


def _start_ma60_pullback_watch() -> None:
    """2026-11-04 owner：「飞书通知增加一个：回踩60日均线不破」。

    与「回踩5日线」同源同代码（`core.ma5_pullback.PROFILE_MA60`），只是均线周期 60：
    判定 = 昨收站在 MA60 上 · 今日最低回到 MA60 附近 ±容差 · 现价未破线也没飞走 ·
    **昨日 MA60 较 20 日前上行**（下行的 60 日线不当支撑）；飞书闸门另限日线多头。
    独立 daemon 线程 + 独立去重文件（ma60_pullback_pushed.json）；
    开关：feishu.enabled + feishu.notify_on_ma60_pullback。
    """
    global _ma60_pullback_thread
    try:
        if not _hunter_ma5_feishu_enabled("notify_on_ma60_pullback"):
            log.info("📉 回踩60日线不破轮询: 已在配置中关闭（feishu.notify_on_ma60_pullback）")
            return
        if _ma60_pullback_thread is not None and _ma60_pullback_thread.is_alive():
            return
        from core.ma5_pullback import run_loop, POLL_SEC, PROFILE_MA60
        _interval = float(PARAMS.get("ma60_pullback_poll_sec", POLL_SEC) or POLL_SEC)
        _ma60_pullback_thread = _threading.Thread(
            target=run_loop, kwargs={"poll_sec": _interval, "profile": PROFILE_MA60},
            name="ma60-pullback-watch", daemon=True)
        _ma60_pullback_thread.start()
        log.info("📉 回踩60日线不破轮询已启动（独立线程，每 %.0f 分钟，交易时段）" % (_interval / 60.0))
    except Exception as e:
        log.warning(f"⚠️ 回踩60日线不破轮询启动失败（不阻断主循环）: {str(e)[:160]}")


def _start_hunter_ma5_watch() -> None:
    """启动「**异常监控池**内个股·刚站上5日线」实时 watcher（2026-10-20 建，2026-10-22 换名单）。

    owner 2026-10-22：「取消选股猎手的推送飞书，但是异常监控池需要实时播报」——
    观察名单由 `hunter_ma5_alert.pool_stocks()`（`t_io/state/abnormal_pool.json`）决定，
    池空则这一路不推送。原来的「猎手前 N 热门板块」口径仍保留（`universe="hunter"`）但不再默认启用。


    为什么不再挂在主循环里：main.py 一轮 `scan_once` 实测 ~15 分钟，
    旧路径「每 5 分钟」实际被拖成 15 分钟级 ⇒ 盘中回站提醒严重迟到。
    新 watcher 自带 20s 节拍，与主循环完全解耦；开关仍读 feishu.enabled +
    notify_on_hunter_ma5（关掉就不启动）。
    """
    global _hunter_ma5_watch_thread
    try:
        if not _hunter_ma5_feishu_enabled():
            log.info("⬆️ 异常监控池站上5日线实时播报: 已在配置中关闭（feishu.notify_on_hunter_ma5）")
            return
        if _hunter_ma5_watch_thread is not None and _hunter_ma5_watch_thread.is_alive():
            return
        from core.hunter_ma5_alert import run_live_watch
        _poll = float(PARAMS.get("hunter_ma5_poll_sec", 20) or 20)
        _hunter_ma5_watch_thread = _threading.Thread(
            target=run_live_watch,
            kwargs={"poll_sec": _poll, "universe": "pool"},
            name="pool-ma5-watch", daemon=True)
        _hunter_ma5_watch_thread.start()
        log.info("⬆️ 异常监控池「刚站上5日线」实时播报已启动（独立线程，每 %.0fs，交易时段）" % _poll)
    except Exception as e:
        log.warning(f"⚠️ 异常监控池实时播报启动失败（不阻断主循环）: {str(e)[:160]}")


# 2026-10-11 owner：「实盘过程中选股猎手每隔 30 分钟做轮询；**异常信息一定要通过飞书发出来**」
def _maybe_run_hunter_poll(now: datetime) -> None:
    """选股猎手 **30 分钟轮询 + 异常飞书告警**（2026-10-11 owner）。

    交易时段 09:30-11:30 / 13:00-15:00，**每满 30 分钟**跑一轮（不卡死整点，避免与其它 5 分钟任务撞车）。
    每轮重跑猎手取数+打分并校验；**任何异常/数据异常 ⇒ 飞书告警**（去重：同类一天一次）。
    无头运行，不依赖 GUI 是否打开；轮询本体在后台线程，不阻塞主循环。
    """
    global _hunter_poll_last, _hunter_poll_thread
    try:
        if not _hunter_poll_feishu_enabled():
            return
    except Exception:
        return
    # 时段/节流判据放 core（**可测**；根 main.py 导入重且与 stock_hunter/main.py 同名）
    from core.hunter_poll import should_poll
    if not should_poll(now, _hunter_poll_last):
        return
    if _hunter_poll_thread is not None and _hunter_poll_thread.is_alive():
        return                      # 上一轮还没跑完（猎手全流程可达 1~2 分钟）⇒ 本轮跳过
    _hunter_poll_last = now
    today = now.strftime("%Y-%m-%d")

    def _worker(day: str) -> None:
        # ⚠️ 这里**不吞异常**：告警链路本身的失败必须留痕（本仓有"告警从未发出过"的血案）。
        from core.hunter_poll import run_and_alert
        res = run_and_alert(day)
        if res.get("ok"):
            log.info(f"选股猎手轮询正常（{day}）：打分 {res['stats'].get('scored')} 只，"
                     f"行情覆盖率 {res['stats'].get('coverage')}")
        else:
            log.warning(f"⚠️ 选股猎手轮询异常（{day}）pushed={res.get('pushed')}")

    _hunter_poll_thread = _threading.Thread(
        target=_worker, args=(today,), name="hunter-poll", daemon=True)
    _hunter_poll_thread.start()


def _start_summary_regen() -> None:
    """2026-11-14 owner：「市场风向标盘内排名至少 15 分钟级刷新」。

    独立 daemon 线程（**不挂主循环**，与「站上5日线」watcher 同样的理由）：每 15 分钟全池重跑
    猎手打分并写回 daily_summary.json（风向标排名 / 星空图 / 板块贡献的真源）。**不推飞书**。
    开关：config PARAMS 的 `summary_regen_poll_sec`（默认 900，缺省即 15 分钟）。
    """
    global _summary_regen_thread
    try:
        if _summary_regen_thread is not None and _summary_regen_thread.is_alive():
            return
        from core.hunter_poll import run_summary_regen, SUMMARY_REGEN_INTERVAL_SEC
        _interval = float(PARAMS.get("summary_regen_poll_sec", SUMMARY_REGEN_INTERVAL_SEC)
                          or SUMMARY_REGEN_INTERVAL_SEC)
        _summary_regen_thread = _threading.Thread(
            target=run_summary_regen, kwargs={"poll_sec": _interval},
            name="summary-regen", daemon=True)
        _summary_regen_thread.start()
        log.info("🧭 风向标排名 15 分钟重跑已启动（独立线程，每 %.0f 分钟，交易时段）" % (_interval / 60.0))
    except Exception as e:
        log.warning(f"⚠️ 风向标排名重跑启动失败（不阻断主循环）: {str(e)[:160]}")


def _hunter_poll_feishu_enabled() -> bool:
    """config.json 的 feishu.enabled + notify_on_hunter_poll 开关（与既有 notify_on_* 同款）。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get("notify_on_hunter_poll", True))
    except Exception:
        return True
