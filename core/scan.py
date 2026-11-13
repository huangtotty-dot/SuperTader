# -*- coding: utf-8 -*-
"""做T盯盘主循环**单轮扫描**（2026-11-13 自 main.py 抽出）。

scan_once 是 main.py 里最后一个巨函数（~520 行）。它此前赖 `engine` 这个
run_watch 里 `global engine` 重绑定的模块级全局 + 一堆 config/core 单例。

**注入方案（owner #3 参数注入）**：`engine` 改为显式参数；scan_once 自己的 5 个
私有状态（`_scan_lock`/`_scan_count`/`_last_idle_log`/`_SWING_DEDUP_DATE`/
`_SWING_PUSH_DEDUP`）随函数搬来当模块全局（无跨模块共享）；其余全是可 import 的
模块单例，身份不因搬移而变。
"""
from __future__ import annotations

import time
import time as _time
from datetime import time as dtime

from config import HOLDINGS, PARAMS, STOCK_PARAMS, log
from core.utils import (
    AI_REVIEW_STATS, DAILY_DECISION_STATS, _append_jsonl,
    _ensure_ai_review_stats, _ensure_daily_decision_stats, _snapshot_write, _trace_path,
)
from core.data_fetcher import (
    DAILY_CONTEXT_CACHE, MINUTE_FETCH_DETAIL, MINUTE_FETCH_STATUS,
    _now, clean_code, fetch_minute_bar, get_daily_context, label,
)
from core.scan_timing import _ScanStage, _scan_stage_add
from core.signal_engine import (
    _minute_issue_bucket, _minute_status_label, write_shadow_signal,
)
from analysis.indicators import add_indicators
import core.spot_vwap as _sv
from core.spot_vwap import _refresh_spot_vwap_cache
import core.index_alerts as _ia
from core.index_alerts import (
    _code_intraday_board, _maybe_check_index_divergence,
    _maybe_check_index_intraday_alert, _maybe_record_index_regime_eod,
)
from core.signal_gates import (
    _below_ma5, _push_intercept_notice, _resonance_gate, _signal_whitelist, _t_gate_bypass,
)
from core.total_equity import _compute_total_equity
from core.eod_review import (
    _maybe_record_daily_pnl, _maybe_run_chart_prefetch,
    _maybe_run_daily_review, _maybe_run_freshness_patrol,
)
from core.eod_tasks import _maybe_run_sentiment_eod, _maybe_run_tdx_calibrate
from core.position_scan import _maybe_run_position_builder, _maybe_run_position_builder_intraday
from core.ma_alerts import (
    _maybe_check_trend30_alert, _maybe_run_ma_break_alert, _maybe_run_ma_reclaim_alert,
)
from core.hunter_watch import _maybe_run_hunter_poll
from optimization.support_resistance import pivot_audit


# 高抛低吸纯两点推送最小防重：同 (code, action, 5分钟桶) 每日只推一次
_SWING_DEDUP_DATE: str = ""
_SWING_PUSH_DEDUP: set = set()

# 主循环重入锁 / 扫描计数 / 非交易时段保活日志节流（2026-10-09 修复：这三行被误删，
# 导致 scan_once 首轮读未定义全局 NameError，午间/盘中启动直接崩主循环）
_scan_lock: bool = False
_scan_count: int = 0
_last_idle_log = None   # None ⇒ 首次进入空闲分支直接记录（见 scan_once 比较处）


def scan_once(engine):
    global _last_idle_log, _scan_count, _scan_lock
    global _SWING_DEDUP_DATE
    if _scan_lock:
        log.warning("⚠️ 上一轮扫描仍在进行，跳过本轮触发")
        return

    _scan_lock = True
    try:
        now = _now()
        t = now.time()

        # ⚠️ 阶段编号里**没有 ①**：原「①早盘基调」（09:26 大盘基调飞书卡片）已按 owner
        #    2026-10-29「这类消息删掉」整块移除。编号不重排，免得历史日志对不上。
        with _ScanStage("②收盘pnl"):
            _maybe_record_daily_pnl(now)               # 14:59 后每日一次：仅落盘 daily_pnl.jsonl + 收盘 pre_close（推送已删）
        with _ScanStage("③尾盘评分落库"):
            _maybe_record_index_regime_eod(now)        # 14:30-14:55 尾盘大盘评分**落库**（不推送，2026-10-29）

        if dtime(14, 50) <= t <= dtime(15, 5):
            with _ScanStage("④pivot复盘"):
                pivot_audit(now)                       # 14:50-15:05 pivot 支撑/压力复盘

        with _ScanStage("⑤盘后建仓扫描"):
            _maybe_run_position_builder(now)           # 15:05-15:15 建仓信号扫描（每日一次）
        with _ScanStage("⑥每日复盘"):
            _maybe_run_daily_review(now)               # P0-7(2026-09-01): 15:10 后自动每日复盘（子进程隔离）
        with _ScanStage("⑦K线预下载"):
            _maybe_run_chart_prefetch(now)             # 2026-10-04: 15:10 后 K线缓存预下载（与 gui 幂等）
        with _ScanStage("⑧新鲜度巡检"):
            _maybe_run_freshness_patrol(now)           # 2026-10-12: 15:40 后产物新鲜度巡检 → 飞书（零网络）
        with _ScanStage("⑨热度EOD"):
            _maybe_run_sentiment_eod(now)              # 2026-10-19: 15:30 后当日 EOD 落库（跨日 S 图当天更新）
        with _ScanStage("⑩通达信日线校准"):
            _maybe_run_tdx_calibrate(now)              # 2026-10-30: 15:40 后用通达信 .day 校准日线缓存

        if now.weekday() >= 5 or t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
            if _last_idle_log is None or (_now() - _last_idle_log).total_seconds() >= PARAMS["idle_log_minutes"] * 60:
                log.info("⏸ 非交易时段，进入低频保活")
                _last_idle_log = _now()
            return

        with _ScanStage("A大盘分时预警"):
            _maybe_check_index_intraday_alert(now)     # 09:35-14:55 大盘分时预警（300s 节流）
        with _ScanStage("B指数背离 ←嫌疑"):
            _maybe_check_index_divergence(now)         # 09:35-14:55 指数背离提醒（300s 节流，事件去重）
        with _ScanStage("C盘中建仓扫描(起线程)"):
            _maybe_run_position_builder_intraday(now)  # 09:30-11:30/13:00-14:55 盘中建仓信号扫描（每5分钟）
        with _ScanStage("D破线告警"):
            _maybe_run_ma_break_alert(now)             # 09:30-14:55 盘中破5/10日线报警（每5分钟，提醒建仓）
        with _ScanStage("E站上告警"):
            _maybe_run_ma_reclaim_alert(now)           # 09:30-14:55 盘中站上5/10日线报警（破线回站，2026-10-08）
        with _ScanStage("F趋势告警"):
            _maybe_check_trend30_alert(now)            # 09:30-14:55 30min趋势翻转告警（每5分钟，纯通知）
        # G 猎手MA5告警已迁到独立 watcher（2026-10-20）：不再受主循环 15 分钟节拍拖累
        with _ScanStage("H猎手轮询(起线程)"):
            _maybe_run_hunter_poll(now)                # 09:30-15:00 猎手 30 分钟轮询 + **异常必发飞书**（2026-10-11）

        if not HOLDINGS:
            return
        _scan_count += 1
        minute_issue_stats = {}

        # V1.28: 暴跌模式优先生成跌幅最大标的的信号
        _crash_prioritized = False
        try:
            _idx_score = float(DAILY_CONTEXT_CACHE.get("index_score", 0) or 0)
        except Exception:
            _idx_score = 0.0
        if _idx_score <= -30:
            _codes_with_decline = []
            for _c, _h in HOLDINGS.items():
                _dec = DAILY_DECISION_STATS.get(_c) or {}
                _lp = float(_dec.get("last_price") or 0)
                _pc = float(_h.get("pre_close") or 0)
                if _pc > 0 and _lp > 0:
                    _decline = (_lp - _pc) / _pc
                else:
                    _decline = 0.0
                _codes_with_decline.append((_decline, _c, _h))
            _codes_with_decline.sort(key=lambda x: x[0])  # 跌幅最大(最负)的排最前
            HOLDINGS_SORTED = [(c, h) for _, c, h in _codes_with_decline]
            _crash_prioritized = True
            if _scan_count % 4 == 1:
                log.info(f"📉 暴跌优先扫描模式(指数{_idx_score:.0f}分)：按跌幅降序扫描")
        else:
            HOLDINGS_SORTED = list(HOLDINGS.items())

        _hold_t0 = _time.perf_counter()          # 逐阶段计时（2026-10-16，见 _ScanStage）
        for code, holding in HOLDINGS_SORTED:
            _ensure_ai_review_stats(code, holding)
            dec = _ensure_daily_decision_stats(code, holding)
            # C-5(2026-08-21): 信号评估白名单——已清仓 qty=0 跳过信号评估，仅记 trace（C27）
            _wl = _signal_whitelist(code, holding)
            dec["_signal_whitelist"] = _wl
            if not (_wl.get("eval_buy") or _wl.get("eval_sell")):
                dec["last_status"] = "已清仓跳过(白名单)"
                log.info(f"🚫 {label(code, holding)} qty=0 已清仓，跳过信号评估（C-5/C27）")
                try:
                    _append_jsonl(_trace_path("data_quality"), {
                        "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                        "code": code, "source": "scan_gate", "whitelist_skip": "qty0",
                    })
                except Exception:
                    pass
                continue

            try:
                time.sleep(0.5)
                with _ScanStage("I1分钟线取数"):
                    df = fetch_minute_bar(code, is_etf=holding.get("type") == "etf")

                dec["minute_status"] = MINUTE_FETCH_STATUS.get(code, "unknown")
                dec["minute_detail"] = MINUTE_FETCH_DETAIL.get(code, "")
                dec["last_scan_time"] = _now().strftime("%H:%M:%S")

                minute_status = MINUTE_FETCH_STATUS.get(code, "unknown")
                minute_detail = MINUTE_FETCH_DETAIL.get(code, "")
                minute_label = _minute_status_label(minute_status, minute_detail)
                if df.empty:
                    dec["last_status"] = f"分钟线断流({minute_label})"
                    dec["last_status_detail"] = minute_detail
                    bucket = _minute_issue_bucket(minute_status)
                    minute_issue_stats.setdefault(bucket, {})
                    minute_issue_stats[bucket][minute_label] = minute_issue_stats[bucket].get(minute_label, 0) + 1
                    log.warning(f"⚠️  {label(code, holding)} 分钟线为空 [{minute_label}]")
                    continue
                if minute_status not in {"ok", "cache_hit"}:
                    dec["last_status"] = f"分钟线异常({minute_label})"
                    dec["last_status_detail"] = minute_detail
                    bucket = _minute_issue_bucket(minute_status)
                    minute_issue_stats.setdefault(bucket, {})
                    minute_issue_stats[bucket][minute_label] = minute_issue_stats[bucket].get(minute_label, 0) + 1
                    log.warning(f"⚠️  {label(code, holding)} 分钟线状态异常 [{minute_label}] {minute_detail}")
                    _append_jsonl(_trace_path("data_quality"), {
                        "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                        "code": code,
                        "source": "scan_gate",
                        "minute_status": minute_status,
                        "minute_detail": minute_detail,
                        "fetch_cost_ms": 0,
                    })
                    continue

                df = add_indicators(df)
                # VWAP 精度修正：有实时快照 VWAP 时覆盖分钟线自算的最后一格
                _refresh_spot_vwap_cache()
                api_code = clean_code(code)
                spot_vwap = _sv._SPOT_VWAP_CACHE.get(api_code)
                if spot_vwap and spot_vwap > 0:
                    df.loc[df.index[-1], "vwap"] = spot_vwap

                price = float(df.iloc[-1]["close"]) if "close" in df.columns else 0.0
                vwap = float(df.iloc[-1]["vwap"]) if "vwap" in df.columns else price
                amp = float(df.iloc[-1]["day_amplitude"]) if "day_amplitude" in df.columns else 0.0
                dec["last_vwap"] = vwap
                dec["close_price"] = price
                dec["last_amp"] = amp
                if len(df) < 2:
                    dec["last_status"] = "数据预热"
                    continue

                can_t = holding.get("t_qty", 0) > 0
                daily_ctx = get_daily_context(code, holding, current_price=price)
                # V1.30: 盘中分时预警注入引擎特征（供展示/回溯）
                # B-3(2026-09-07): 改按个股所属板注入（原全局 _INDEX_INTRADAY_ACTIVE_ALERTS 只盯上证，
                # 深/创/科创个股会收到错误板块的预警）。无该板活动预警时保留旧全局兜底（兼容旧启动态）。
                try:
                    _board_active = _ia._INDEX_INTRADAY_ACTIVE_ALERTS_BY_BOARD.get(
                        _code_intraday_board(code))
                    if _board_active is None:
                        # 兼容兜底：旧启动态/尚未写入分板字典时用旧全局列表
                        _board_active = _ia._INDEX_INTRADAY_ACTIVE_ALERTS
                    if _board_active:
                        daily_ctx["intraday_alerts"] = [
                            {"tag": a.get("tag"), "level": a.get("level"), "msg": a.get("msg")}
                            for a in _board_active
                        ]
                except Exception:
                    pass
                dec["daily_status"] = daily_ctx.get("daily_status", "unknown")
                dec["last_daily_gate"] = daily_ctx.get("daily_gate", "neutral")
                dec["last_daily_trend_bg"] = daily_ctx.get("daily_trend_bg", "unknown")
                dec["last_daily_support"] = daily_ctx.get("daily_support_name", "")
                dec["last_daily_support_gap"] = daily_ctx.get("daily_support_gap", 0.0)
                dec["last_daily_overheated"] = daily_ctx.get("daily_overheated", False)
                # 2026-09-22：_attach_dynamic_t_decision（T 模式选择）已随做T引擎删除。
                with _ScanStage("I2engine.evaluate"):
                    buy_score, sell_score, sig = engine.evaluate(code, holding.get("name", code), df, holding, daily_ctx=daily_ctx)

                dec["last_benchmark_code"] = sig.indicators.get("benchmark_code", "") if sig else dec.get("last_benchmark_code", "")
                dec["last_benchmark_name"] = sig.indicators.get("benchmark_name", "") if sig else dec.get("last_benchmark_name", "")
                dec["last_benchmark_state"] = sig.indicators.get("benchmark_state", "unknown") if sig else dec.get("last_benchmark_state", "unknown")
                dec["last_benchmark_gate"] = sig.indicators.get("benchmark_gate", "neutral") if sig else dec.get("last_benchmark_gate", "neutral")
                dec["last_benchmark_reason"] = sig.indicators.get("benchmark_reason", "") if sig else dec.get("last_benchmark_reason", "")

                dec["last_buy_score"] = buy_score
                dec["last_sell_score"] = sell_score

                st = AI_REVIEW_STATS[code]
                st["最大多头分"] = max(st["最大多头分"], buy_score)
                st["最大空头分"] = max(st["最大空头分"], sell_score)
                st["最大振幅"] = max(st["最大振幅"], amp)

                best_score = max(buy_score, sell_score)
                # V1.30: 熔断显性化 —— 买入被风控硬否决时面板直接显示原因，不再呈现"分够却HOLD"的假矛盾
                _last_dec = engine.last_decision.get(code, {}) if hasattr(engine, "last_decision") else {}
                _dec_reason = str(_last_dec.get("reason", ""))
                if dec.get("last_stand_down_reason"):
                    stat = f"停手:{dec.get('last_stand_down_reason')}"
                elif _dec_reason.startswith("HOLD_BUY_BLOCKED:index_uni_down_clearance"):
                    stat = "停手:买入熔断(单边下行)"
                elif _dec_reason.startswith("HOLD_BUY_BLOCKED:"):
                    stat = f"停手:买入熔断({_dec_reason.split(':', 1)[1][:20]})"
                elif _dec_reason == "HOLD_SELL_PRIORITY":
                    stat = "停手:卖压压制(卖分>买分)"
                elif dec.get("last_buy_limit_reason"):
                    stat = f"停手:{dec.get('last_buy_limit_reason')}"
                elif amp < PARAMS['min_amplitude']:
                    stat = "无波待涨"
                elif not can_t:
                    stat = "底仓"
                elif best_score >= 65:
                    stat = "强可T"
                elif best_score >= 45:
                    stat = "可T观察"
                elif best_score >= 25:
                    stat = "弱机会"
                else:
                    stat = "无信号"
                if sig and sig.action in {"BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"}:
                    stat = f"{stat}|{sig.action}"
                dec["last_status"] = stat

                _snapshot_write(code, holding, df, {
                    "price": price,
                    "vwap": vwap,
                    "market_state": sig.indicators.get("market_state", dec.get("last_market_state", "unknown")) if sig else dec.get("last_market_state", "unknown"),
                    "benchmark_code": dec.get("last_benchmark_code", ""),
                    "benchmark_name": dec.get("last_benchmark_name", ""),
                    "benchmark_state": dec.get("last_benchmark_state", "unknown"),
                    "benchmark_gate": dec.get("last_benchmark_gate", "neutral"),
                    "benchmark_reason": dec.get("last_benchmark_reason", ""),
                }, {
                    "action": sig.action,
                    "score": sig.score,
                    "reasons": sig.reasons,
                    "entry_kind": sig.factors.get("entry_kind", "") if sig else "",
                } if sig else None, daily_context=daily_ctx)

                if sig:
                    # V1.14: 新架构 — 市场状态识别 + 动态份数 + 高抛低吸组合拳
                    # 1. 识别当前市场状态
                    regime = None
                    regime_reason = ""
                    try:
                        from core.market_regime import detect_regime, MarketRegime
                        regime_obj, regime_reason = detect_regime(
                            code, _now().strftime("%Y-%m-%d")
                        )
                        regime = regime_obj
                        # 将状态注入 sig，供 notify 使用
                        sig.regime = regime.value
                        sig.regime_reason = regime_reason
                        log.info(f"🎯 {code} 市场状态: {regime.value} | {regime_reason}")
                    except Exception as e:
                        sig.regime = "normal"
                        sig.regime_reason = "状态识别失败"
                        log.debug(f"⚠️  {code} 市场状态识别失败: {e}")
                    
                    # 2. 动态份数计算（个股/ETF统一）
                    _advice = None  # W33 G1: sizing_advice 结构化落盘（买卖双侧）
                    _buy_cap = None          # F-5: 买入钳制后真实 max_buyable（归因旁路）
                    _buy_block_reason = None  # F-5: 买入拦截原因码（no_t_budget/full_position/...）
                    _is_buy = False          # F-5: 本信号是否买入动作（advice 归因字段选择）
                    try:
                        from core.position_sizer import (calc_sell_qty, calc_buy_qty, set_all_holdings,
                                                         last_buy_state)
                        set_all_holdings(HOLDINGS)  # fix P0-9(B4): 注入全量持仓供单股上限 A/B 合并判定
                        threshold = float(sig.factors.get("threshold", 35))
                        cur_price = float(sig.price or 0)
                        total_equity = _compute_total_equity()  # fix P0-9(B1): 单股上限所需真实总资产
                        if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                            dynamic_qty = calc_sell_qty(
                                code, holding, regime,
                                float(sig.score), threshold,
                                used_sells=engine.sell_count_per_stock.get(code, 0),
                                params={**PARAMS, **STOCK_PARAMS.get(code, {})},
                                index_ctx=daily_ctx,
                                current_price=cur_price,
                                total_equity=total_equity,
                            )
                        else:
                            dynamic_qty = calc_buy_qty(
                                code, holding, regime,
                                float(sig.score), threshold,
                                params={**PARAMS, **STOCK_PARAMS.get(code, {})},
                                index_ctx=daily_ctx,
                                current_price=cur_price,
                                total_equity=total_equity,
                            )
                            # F-5: 紧接 calc_buy_qty 读归因（避免跨信号污染）
                            _st_buy = last_buy_state() or {}
                            _buy_cap = _st_buy.get("max_buyable")
                            _buy_block_reason = _st_buy.get("reason")
                        if dynamic_qty > 0:
                            sig.hold_qty = dynamic_qty
                            total_t = int(holding.get("t_qty", 0))  # 纯底仓(t_qty=0)不应用qty回退
                            pct = dynamic_qty / total_t * 100 if total_t > 0 else 0
                            log.info(f"📊 动态份数 {code}: 状态={regime.value if regime else 'normal'} 信号强度{sig.score:.0f}/阈值{threshold:.0f}, 建议交易{dynamic_qty}股/份 ({pct:.0f}%)")
                        else:
                            sig.hold_qty = 0
                            log.info(f"🛑 大盘熔断/仓控阻断 {code}: {daily_ctx.get('index_circuit_state', 'normal')} / {daily_ctx.get('index_gate_advice', 'normal_t')}")
                        # W33 G1: 捕获 sizing 画像（pushed 待推送决策后回填）
                        # manual 做T 下线（2026-09-14）：虚拟成交台账不再存在，净量恒=纯底仓 t_qty。
                        _net = max(0, int(holding.get("t_qty", 0)))
                        _unrebuilt = 0
                        _is_buy = sig.action in ("BUY_LOW", "ADD_POS")
                        _raw_cap = max(0, int(holding.get("t_qty", 0)) - _net)
                        _advice = {
                            "ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
                            "code": code, "name": holding.get("name", code),
                            "action": sig.action,
                            "price": cur_price,
                            "vwap": float(sig.indicators.get("vwap", sig.price) or sig.price),
                            "suggested_qty": dynamic_qty,
                            "score": float(sig.score), "threshold": threshold,
                            "t_qty": int(holding.get("t_qty", 0)),
                            "net_qty": _net, "unrebuilt": _unrebuilt,
                            # F-5(2026-09-04): 买入显示 index_factor 钳制后真实上限；卖出保留原 raw 口径
                            "max_buyable": (_buy_cap if (_is_buy and _buy_cap is not None) else _raw_cap),
                            "block_reason": (_buy_block_reason if _is_buy else None),
                            "executable": dynamic_qty > 0,
                        }
                    except Exception as e:
                        log.warning(f"⚠️  动态份数计算失败 {code}: {e}")
                    
                    # 3. 信号分数门槛 + 通知
                    # V1.29: 推送阈值从硬编码改为 PARAMS + 个股STOCK_PARAMS 双层管理
                    # 个股专属值 > 全局PARAMS > 硬编码默认值
                    _sp = STOCK_PARAMS.get(code, {})
                    today_ret_snap = daily_ctx.get("daily_day_ret", 0.0)
                    if sig.action in ["BUY_LOW", "ADD_POS"]:
                        notify_threshold = _sp.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
                    else:
                        if t >= dtime(10, 0):
                            notify_threshold = _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
                        elif today_ret_snap < -0.04 and sig.action in ("PANIC_SELL", "SELL_HIGH"):
                            notify_threshold = PARAMS.get("notify_sell_panic_threshold", 60)
                        else:
                            notify_threshold = _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_early_threshold", 75)
                    
                    # 高抛低吸纯两点 (2026-08-13): 两点满足即推送；仓控0股也推(仅供参考不记账)
                    pushed = sig.score >= notify_threshold
                    _block_reason = None  # C13修复(2026-08-18): 记录真实拦截原因，供日志/shadow落盘

                    # 风险2修复(2026-08-24): stock_override禁用门控标的，跳过防重桶拦截
                    _stock_override_enabled = True
                    try:
                        from config import INDEX_RESONANCE_STOCK_OVERRIDE
                        _so = INDEX_RESONANCE_STOCK_OVERRIDE.get(code, {})
                        if _so.get("enabled") is False:
                            _stock_override_enabled = False
                    except Exception:
                        pass

                    # 纯两点规则 (2026-08-13): 移除轮次上限/单股日限；score=100 恒过阈值 → 一定推送
                    # 仅保留最小防重：同 (code, action, 5分钟桶) 每日只推一次
                    if pushed:
                        # 如果是stock_override禁用门控的标的，跳过防重桶检查
                        if not _stock_override_enabled:
                            _block_reason = "stock_override: 禁用门控标的跳过防重桶拦截"
                        else:
                            _today = now.strftime("%Y-%m-%d")
                            if _SWING_DEDUP_DATE != _today:
                                _SWING_DEDUP_DATE = _today
                                _SWING_PUSH_DEDUP.clear()
                            _dkey = (code, sig.action, t.hour * 12 + t.minute // 5)
                            if _dkey in _SWING_PUSH_DEDUP:
                                pushed = False
                                _block_reason = "防重桶拦截（同股同向5分钟桶内已推）"
                            else:
                                _SWING_PUSH_DEDUP.add(_dkey)
                    # 指数5分钟共振门控（2026-08-14）：指数与个股同向才推送，否则整条信号作废
                    # C-1: SELL_HIGH 已在 _resonance_gate 内部按 RESONANCE_GATE.bypass_sell_high 跳过
                    _res_blocked = False
                    if pushed:
                        _gate_pass, _res = _resonance_gate(code, sig, now)
                        if _res is not None:
                            if _res.get("bypass"):
                                _res_status = {
                                    "sell_high": "SELL_HIGH跳过共振(C-1)",
                                    "t_swing_buy": "做T低吸跳过共振(意图分流)",
                                    "stock_override_disabled": "共振放行(标的禁用)",
                                }.get(str(_res.get("bypass")), "共振放行(bypass)")
                                if str(_res.get("bypass")) == "t_swing_buy":
                                    log.info(f"⏭️ {code} {sig.action} 共振放行(做T低吸意图分流, C-1)，正常推送")
                            elif _res.get("missing"):
                                _res_status = "指数数据缺失" if not _gate_pass else "指数数据缺失放行"
                            elif _gate_pass:
                                _res_status = "共振通过"
                            else:
                                _res_status = f"共振拦截({_res.get('gate', '')})"
                        else:
                            _res_status = "共振放行(模块不可用)"
                        dec["last_resonance_status"] = _res_status
                        dec["last_resonance_gate"] = (_res or {}).get("gate", "")
                        dec["last_resonance_index"] = (_res or {}).get("index_code", "")
                        if not _gate_pass:
                            _res_blocked = True
                            pushed = False
                            log.info(f"🚫 {code} {sig.action} 指数共振拦截（{_res_status}）: {(_res or {}).get('gate_reason', '')}")
                    # C-2(2026-08-21): 个股MA5闸——破五日线只卖不买（用户规则系统化）；BUY_LOW 最新收盘<MA5 不推送
                    # 2026-09-05 意图分流: swing_renko 做T日内低吸(当日14:55了结)不受日线破MA5锁死
                    # （近一周 588170 整周 0 低吸的主因）。开关 T_INTRADAY_GATE_BYPASS.c2_ma5_for_swing_buy。
                    _ma5_suppressed = False
                    _is_t_swing_buy = (sig.action == "BUY_LOW"
                                       and str((sig.factors or {}).get("entry_kind", "")) == "swing_renko")
                    _c2_released = (_is_t_swing_buy and _t_gate_bypass("c2_ma5_for_swing_buy"))
                    if pushed and sig.action == "BUY_LOW":
                        try:
                            _below5 = _below_ma5(code)
                        except Exception:
                            _below5 = None
                        if _below5 and _c2_released:
                            # 可见性: 破线但属做T日内低吸 → 放行（周复盘逐笔可核对）
                            log.info(f"⏭️ {code} BUY_LOW 破线放行(做T低吸意图分流, C-2)")
                        elif _below5:
                            _ma5_suppressed = True
                            pushed = False
                            _block_reason = "个股收盘<MA5(破线只卖不买)"
                            log.info(f"🚫 {code} BUY_LOW 个股收盘<MA5(破线只卖不买)，不推送")
                        # else: 站上MA5，C-2 天然放行
                    # C-3(2026-08-21)+F-11(2026-09-04): 拦截可见性——被拦截且原score>=推送阈值，推低优飞书
                    # （20min 滚动冷却窗去重，同码同日复拦仍可见）
                    if (_res_blocked or _ma5_suppressed) and sig.score >= notify_threshold:
                        try:
                            _push_intercept_notice(code, sig, now,
                                                   _res_status if _res_blocked else "个股收盘<MA5")
                        except Exception:
                            pass
                    # C-5(2026-08-21): 纯底仓股(t_qty=0)保留评估但跳过买/卖推送（C25）
                    _wl = dec.get("_signal_whitelist") or {}
                    if pushed and not _wl.get("push", True):
                        pushed = False
                        _block_reason = "纯底仓股(t_qty=0)跳过推送(白名单)"
                        log.info(f"🚫 {code} {sig.action} 纯底仓股(t_qty=0)，跳过推送（C-5/C25）")
                    if pushed:
                        # manual 做T 飞书推送（notify）与信号记账（record_signal / record_trade_action）
                        # 已随 manual 做T 下线（2026-09-14）删除；仅保留轮次计数诊断。
                        if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                            engine.incr_cycle(code)
                        if sig.hold_qty <= 0:
                            # 查证(2026-09-11 W37): 日志无原因码，无法区分 no_t_budget(无T仓) vs full_position(满仓)。
                            # 补 reason/max_buyable 归因（不改闸门），下一交易日即可定性。
                            _st_b = locals().get("_st_buy") or {}
                            log.info(f"📡 {code} {sig.action}两点触发(score={sig.score:.0f})但仓控可交易量为0"
                                     f" [reason={locals().get('_buy_block_reason') or '?'} "
                                     f"max_buyable={_st_b.get('max_buyable')} t_qty={holding.get('t_qty')} "
                                     f"net_qty={_st_b.get('net_qty')}]")
                    else:
                        action_type = "买入" if sig.action in ["BUY_LOW", "ADD_POS"] else "卖出"
                        time_window = "10:00前" if t < dtime(10, 0) else "10:00后"
                        # C13修复(2026-08-18): 日志与shadow miss_reason 写真实拦截原因（阈值/防重/共振三态）
                        if _res_blocked:
                            _miss = f"指数共振拦截({(_res or {}).get('gate', '')})"
                            log.info(f"🚫 {code} {action_type}信号被指数共振拦截（非阈值不足），不推送")
                        elif _ma5_suppressed:
                            _miss = "个股收盘<MA5(破线只卖不买)"
                            log.info(f"🚫 {code} BUY_LOW 个股收盘<MA5(破线只卖不买)，不推送")
                        elif _block_reason:
                            _miss = _block_reason
                            log.info(f"🔁 {code} {action_type}信号得分{sig.score:.0f}分，{_block_reason}，不重复推送")
                        else:
                            _miss = f"低于推送阈值静默（{time_window}阈值{notify_threshold}分）"
                            log.info(f"📉 {code} {action_type}信号得分{sig.score:.0f}分，低于{time_window}阈值{notify_threshold}分，静默处理（不推送飞书）")
                        try:
                            _sp2 = STOCK_PARAMS.get(code, {})
                            _nb = _sp2.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
                            _ns = _sp2.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
                            write_shadow_signal(
                                code, holding.get("name", code), sig.price,
                                float(sig.indicators.get("vwap", sig.price) or sig.price),
                                buy_score, sell_score, _nb, _ns,
                                _miss,
                                extra={"action": sig.action,
                                       "decision_reason": engine.last_decision.get(code, {}).get("reason", "")},
                            )
                        except Exception:
                            pass
                    # W33 G1: sizing_advice 结构化落盘（每次 sizing 调用一行，喂每日 Review §3 加仓逐笔）
                    if _advice is not None:
                        try:
                            _advice["pushed"] = bool(pushed)
                            _advice["buy_kind"] = "rebuild" if _advice["unrebuilt"] > 0 else "first_add"
                            if sig.hold_qty <= 0:
                                # F-5: 买入拦截携带真实原因码（替代笼统"满仓/熔断"）；卖出保持原文案
                                _advice["note"] = (f"仓控拦截({_buy_block_reason})仅供参考不记账"
                                                   if _is_buy and _buy_block_reason
                                                   else "仓控可交易量为0(满仓/熔断)，仅供参考不记账")
                            _append_jsonl(_trace_path("sizing_advice"), _advice)
                        except Exception as _e:
                            log.warning(f"⚠️  sizing_advice 落盘失败 {code}: {_e}")

                # V1.14: 尾盘强制平仓已删除（用户反馈不需要）

            except Exception as e:
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

        _scan_stage_add("I0持仓循环(全部)", _time.perf_counter() - _hold_t0)

        # 2026-10-12 owner：「console 里对持仓股的轮询显示没有任何必要」⇒ 移除「护城河防御面板」
        # （每 4 轮一张 7 行表：标的/现价/VWAP/振幅/买卖评分/状态）。`panel_rows` 的收集点一并清掉：
        # 它只喂这张表，没有任何其它消费者（也无测试依赖）。逐股信号决策照旧落 trace/snapshot，
        # 只是不再往 console 刷这张表；`_scan_count % 4` 的**另一处**用途（暴跌优先模式单行提示）保留。
    finally:
        _scan_lock = False
