# -*- coding: utf-8 -*-
"""
A股持仓实时做T盯盘脚本（V1.8 确认型收敛版）
基于 v1.4 稳定运行骨架，整合 v1.5 风控与评分优化。
新增：轻量动态市场状态调节、EMA 趋势辅助、区间位置辅助、当日买入次数限制、T-cycle 持仓计时与确认型收敛门控。
"""
import os
import sys
import json
import time
import logging
import importlib.util
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any

# ==================== 代理终极修复 (强制直连) ====================
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
os.environ['ALL_PROXY'] = ''
os.environ['all_proxy'] = ''

import akshare as ak
import numpy as np
import pandas as pd
import requests
import urllib.request
import urllib.error

# V1.11: 日志增强模块导入
try:
    import log_enhancer as _log_enhancer
except Exception:
    _log_enhancer = None

# ==================== 路径与常量 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
T_IO_DIR = os.path.join(BASE_DIR, "t_io")
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
LEARNING_FILE = os.path.join(T_IO_DIR, "t_trader_learning.json")
LOG_DIR = os.path.join(T_IO_DIR, "logs")
CACHE_DIR = os.path.join(T_IO_DIR, "cache")
SNAPSHOT_DIR = os.path.join(T_IO_DIR, "minute_snapshots")
PREOPEN_DIR = os.path.join(T_IO_DIR, "preopen")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TRACE_DIR = os.path.join(T_IO_DIR, "traces")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

for d in [T_IO_DIR, LOG_DIR, CACHE_DIR, SNAPSHOT_DIR, TRACE_DIR, PREOPEN_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)


def load_runtime_config() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"⚠️  运行配置读取失败: {str(e)[:80]}")
    return {}


def resolve_feishu_webhook() -> str:
    env_webhook = (os.getenv("FEISHU_WEBHOOK", "") or "").strip()
    if env_webhook:
        return env_webhook
    config = load_runtime_config()
    return (config.get("feishu", {}).get("webhook_url", "") or "").strip()


def resolve_feishu_keyword() -> str:
    config = load_runtime_config()
    return (config.get("feishu", {}).get("keyword", "") or "做T猎手预警").strip() or "做T猎手预警"


def resolve_feishu_system_keyword() -> str:
    config = load_runtime_config()
    return (config.get("feishu", {}).get("system_keyword", "") or "系统消息").strip() or "系统消息"


FEISHU_WEBHOOK = resolve_feishu_webhook()
FEISHU_KEYWORD = resolve_feishu_keyword()
FEISHU_SYSTEM_KEYWORD = resolve_feishu_system_keyword()
PUSH_THROTTLE_SECONDS = 300

def should_run_startup_self_test() -> bool:
    config = load_runtime_config()
    return bool(config.get("feishu", {}).get("startup_self_test", True))


def send_feishu_payload(payload: dict, success_log: str, error_prefix: str, trigger_urgent_alarm_after_success: bool = False) -> bool:
    if not FEISHU_WEBHOOK:
        log.warning(f"⚠️  {error_prefix}：飞书 Webhook 未配置")
        return False

    last_error = None
    for attempt in range(1):
        try:
            response = requests.post(FEISHU_WEBHOOK, json=payload, timeout=8)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) != 0:
                log.warning(f"⚠️  {error_prefix}失败: {result}")
                return False
            log.info(success_log)
            if trigger_urgent_alarm_after_success and SYS_ALERT_AVAILABLE:
                try:
                    trigger_alert("urgent")
                    log.info("🔔 已触发急促报警")
                except Exception as e:
                    log.warning(f"⚠️  急促报警触发失败: {str(e)[:80]}")
            return True
        except Exception as e:
            last_error = e
            if attempt == 0:
                log.warning(f"⚠️  {error_prefix}第1次发送异常，准备重试: {str(e)[:100]}")
            else:
                log.error(f"❌ {error_prefix}发送异常: {str(e)[:120]}")
    return False


def send_startup_self_test():
    if not FEISHU_WEBHOOK:
        log.warning("⚠️  启动自检跳过：飞书 Webhook 未配置")
        return

    preopen = _ensure_preopen_context(force=False)

    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    at_all = feishu_cfg.get("at_all_on_signal", True)
    use_strong = feishu_cfg.get("use_strong_notification", True)
    relay_urgent_alarm = feishu_cfg.get("relay_urgent_alarm_on_feishu", True)
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    title = f"🚨🚨🚨 【加急】{FEISHU_KEYWORD} - 启动自检 🚨🚨🚨" if use_strong else f"📢 【提醒】{FEISHU_KEYWORD} - 启动自检"

    card_elements = []
    if at_all:
        card_elements.append({
            "tag": "div",
            "text": {"content": at_text, "tag": "lark_md"}
        })
    card_elements.append({
        "tag": "div",
        "text": {"content": title, "tag": "lark_md"}
    })
    preopen_text = "盘前解读：未生成"
    if preopen is not None:
        adv = _preopen_adv_counts(preopen)
        hot_theme = preopen.breadth.get("hot_theme_text", "") if isinstance(preopen.breadth, dict) else ""
        preopen_text = (
            f"盘前解读：{preopen.market_bias} | 评分 {preopen.market_score:.1f} | {preopen.session_note}\n"
            f"涨跌家数：{adv['up']} / {adv['down']} / {adv['flat']} | 热主题：{hot_theme or '暂无'}"
        )
    card_elements.append({
        "tag": "div",
        "text": {
            "content": (
                f"【{FEISHU_SYSTEM_KEYWORD}】\n"
                f"t_trader_v1.8 已启动。\n"
                f"{preopen_text}\n"
                f"如果你收到此消息并听到急促报警音，说明飞书推送与本地报警链路均正常。"
            ),
            "tag": "lark_md"
        }
    })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": card_elements
        },
        "notify_type": 1
    }
    send_feishu_payload(
        payload=payload,
        success_log="✅ 启动自检飞书消息已成功送达",
        error_prefix="启动自检飞书推送",
        trigger_urgent_alarm_after_success=use_strong and relay_urgent_alarm,
    )


SIM_NOW: Optional[datetime] = None


def _now() -> datetime:
    return SIM_NOW or datetime.now()


def get_today_str():
    """动态获取今日日期字符串，防止跨日运行Bug"""
    return _now().strftime("%Y-%m-%d")


def chunk_list(items: List[Any], size: int):
    size = max(1, int(size or 1))
    for i in range(0, len(items), size):
        yield items[i:i + size]

# ==================== 【高级声音报警引擎动态挂载】 ====================
SYS_ALERT_AVAILABLE = False
try:
    for alert_filename in ["system_alert_v17_3.py", "system_alert_v17.3.py"]:
        alert_file = os.path.join(BASE_DIR, alert_filename)
        if not os.path.exists(alert_file):
            continue
        spec = importlib.util.spec_from_file_location("sys_alert", alert_file)
        sys_alert = importlib.util.module_from_spec(spec)
        sys.modules["sys_alert"] = sys_alert
        spec.loader.exec_module(sys_alert)
        init_alert = sys_alert.init_alert
        trigger_alert = sys_alert.trigger_alert
        SYS_ALERT_AVAILABLE = True
        break
except Exception:
    pass # 挂载失败不影响主程序运行

# ==================== 【做T核心风控参数 V1.8】 ====================
PARAMS = {
    "poll_interval": 15,
    "rsi_period": 6,
    "rsi_overbought": 78,
    "rsi_oversold": 25,
    "bb_period": 20,
    "bb_std": 2.0,
    "ema_fast_period": 8,
    "ema_slow_period": 21,
    "min_amplitude": 0.015,
    "min_profit_space": 0.010,
    "commission_rate": 0.0015,
    "cooldown_minutes": 30,
    "repeat_signal_gap_minutes": 60,
    "repeat_signal_price_move": 0.004,
    "repeat_signal_score_boost": 10,
    "buy_signal_price_move": 0.004,
    "buy_signal_score_boost": 4,
    "add_pos_signal_price_move": 0.003,
    "add_pos_signal_score_boost": 3,
    "sell_repeat_block_minutes": 60,
    "sell_signal_price_move": 0.003,
    "sell_signal_score_boost": 4,
    "panic_sell_signal_price_move": 0.002,
    "panic_sell_signal_score_boost": 2,
    "stand_down_min_amplitude": 0.010,
    "stand_down_flat_range_gap": 0.0015,
    "stand_down_score_gap": 10,
    "idle_log_minutes": 10,
    "scan_timeout_seconds": 12,
    "cache_ttl_seconds": 180,
    "cache_cleanup_limit": 200,
    "vol_ratio_confirm": 1.8,
    "vol_confirm_boost": 3,
    "macd_strong_threshold": 0.001,
    "macd_strong_boost": 3,
    "max_buy_times_per_stock": 1,
    "max_sell_times_per_stock": 1,
    "max_holding_minutes": 30,
    "sell_score_boost_holding": 1,
    "sell_holding_min_minutes": 55,
    "sell_holding_strict_minutes": 80,
    "sell_score_boost_eod": 8,
    "sell_momentum_drop_threshold": 0.002,
    "sell_momentum_bonus": 8,
    "buy_confirm_min_seconds": 60,
    "buy_confirm_min_factors": 4,
    "buy_confirm_min_score": 42,
    "buy_rebound_min_score_gap": 4,
    "buy_priority_margin": 2,
    "buy_soft_margin": 1,
    "buy_soft_min_support_factors": 1,
    "buy_starvation_days": 3,
    "buy_starvation_relax_seconds": 25,
    "buy_starvation_relax_factors": 2,
    "buy_starvation_relax_gap": 2,
    "buy_starvation_relax_ttl_days": 2,
    "sell_fast_path_min_gap": 20,
    "post_sell_rebuild_min_seconds": 30,
    "post_sell_rebuild_buy_threshold_penalty": 2,
    "post_sell_rebuild_relax_gap": 4,
    "post_sell_rebuild_relax_factors": 3,
    "post_sell_rebuild_weak_gate_discount": 0.5,
    "sell_confirm_min_factors": 8,
    "sell_confirm_min_seconds": 75,
    "max_t_cycles_per_stock": 2,
    "post_sell_rebuild_minutes": 18,
    "post_sell_rebuild_price_gap": 0.003,
    "post_sell_rebuild_score_gap": 6,
    "trend_today_ret_threshold": 0.025,
    "trend_vwap_dev_threshold": 0.004,
    "market_state_threshold_bias": 5,
    "range_pos_low_threshold": 0.20,
    "range_pos_high_threshold": 0.85,
    "daily_context_enabled": True,
    "daily_cache_ttl_seconds": 1800,
    "daily_context_min_rows": 65,
    "daily_ma_support_gap": 0.025,
    "daily_ma_support_loose_gap": 0.04,
    "daily_ma_breakdown_gap": 0.015,
    "daily_ma_hard_breakdown_gap": 0.035,
    "daily_overheat_ma10_gap": 0.08,
    "daily_overheat_ma20_gap": 0.12,
    "daily_overheat_day_ret": 0.065,
    "daily_support_buy_boost": 6,
    "daily_base_buy_boost": 3,
    "daily_trend_buy_boost": 2,
    "daily_overheat_buy_penalty": 8,
    "daily_breakdown_buy_penalty": 10,
    "daily_downtrend_buy_penalty": 5,
    "daily_breakdown_sell_boost": 7,
    "daily_overheat_sell_boost": 4,
    "daily_risk_buy_threshold_penalty": 8,
    "daily_overheat_buy_threshold_penalty": 6,
    "daily_support_buy_threshold_relief": 2,
    "profit_guard_open_gap_threshold": 0.005,   # 低开触发幅度（绝对值），0.5%
    "profit_guard_profit_max": 0.04,            # 微盈区间上限，4%浮盈以内激活
    "profit_guard_profit_min": 0.0,             # 微盈区间下限，仅浮盈时保护
    "profit_guard_minutes_end": 600,            # 护利窗口结束，10:00 = 600分钟
    "profit_guard_buy_threshold_add": 15,       # buy_threshold 惩罚点数
    "profit_guard_buy_score_penalty": 8,        # buy_score 直接扣分
    "auction_weak_surge_range_pos": 0.72,       # 弱势冲高触发：日内区间位置阈值
    "auction_weak_surge_ret_threshold": 0.015,  # 弱势冲高触发：今日涨幅阈值
    "auction_weak_surge_vwap_gap": 0.008,       # 弱势冲高触发：需高于VWAP的幅度
    "auction_weak_low_range_pos": 0.25,         # 弱势低位买回：日内区间位置阈值
    "auction_weak_low_ret_threshold": -0.005,   # 弱势低位买回：今日跌幅阈值
    "auction_weak_max_sell_alerts": 2,          # 每日最多冲高卖出提醒次数
    "auction_weak_max_buy_alerts": 2,           # 每日最多低位买回提醒次数
}

# ==================== 日志双写配置 ====================
log = logging.getLogger("做T助手")
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

if not log.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    sys_log_file = os.path.join(LOG_DIR, f"t_trader_sys_{get_today_str()}.log")
    file_handler = logging.FileHandler(sys_log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ==================== 全局状态与统计 ====================
_name_cache: Dict[str, str] = {}
HOLDINGS: Dict[str, dict] = {}
STRATEGY_MEMORY: Dict[str, dict] = {}
VIRTUAL_TRADES: Dict[str, Dict[str, list]] = {}
AI_REVIEW_STATS: Dict[str, dict] = {}
MINUTE_FETCH_STATUS: Dict[str, str] = {}
MINUTE_FETCH_DETAIL: Dict[str, str] = {}
DAILY_DECISION_STATS: Dict[str, dict] = {}
SIGNAL_OUTCOME_TRACKER: Dict[str, list] = {}
DAILY_CONTEXT_CACHE: Dict[str, Dict[str, Any]] = {}
SESSION_CONTEXT: Dict[str, Any] = {}
PREOPEN_CONTEXT: Optional[Any] = None
_preopen_logged_date: Optional[str] = None
_preopen_pushed_date: Optional[str] = None
_preopen_monitor_date: Optional[str] = None
_preopen_monitor_last_push_at: Optional[datetime] = None
_preopen_monitor_last_signature: Optional[str] = None
_preopen_monitor_push_count: int = 0
_preopen_overview_last_push_at: Optional[datetime] = None
_eod_logged_date: Optional[str] = None
_scan_lock = False
_auction_alert_state: Dict[str, dict] = {}  # 集合竞价驱动信号的去重状态

def _ensure_ai_review_stats(code: str, holding: dict) -> dict:
    if code not in AI_REVIEW_STATS:
        AI_REVIEW_STATS[code] = {"名称": holding.get("name", code), "最大多头分": 0, "最大空头分": 0, "最大振幅": 0.0, "触发买入次数": 0, "触发卖出次数": 0, "触发买入股数": 0, "触发卖出股数": 0}
    AI_REVIEW_STATS[code]["名称"] = holding.get("name", code)
    return AI_REVIEW_STATS[code]


def _ensure_daily_decision_stats(code: str, holding: dict) -> dict:
    default_price = float(holding.get("cost", 0) or 0)
    if code not in DAILY_DECISION_STATS:
        DAILY_DECISION_STATS[code] = {
            "name": holding.get("name", code),
            "buy_signals": [],
            "buy_low_signals": [],
            "buy_add_signals": [],
            "sell_signals": [],
            "sell_high_signals": [],
            "panic_sell_signals": [],
            "last_price": default_price,
            "last_vwap": default_price,
            "close_price": default_price,
            "last_score": 0.0,
            "last_buy_score": 0.0,
            "last_sell_score": 0.0,
            "last_amp": 0.0,
            "last_scan_time": "",
            "last_status": "未扫描",
            "last_status_detail": "",
            "last_market_state": "unknown",
            "last_benchmark_code": "",
            "last_benchmark_name": "",
            "last_benchmark_state": "unknown",
            "last_benchmark_gate": "unknown",
            "last_benchmark_reason": "",
            "last_buy_limit_reason": "",
            "minute_status": "未拉取",
            "minute_detail": "",
        }
    stats = DAILY_DECISION_STATS[code]
    stats["name"] = holding.get("name", code)
    return stats


def _low_buy_cash_reference() -> float:
    runtime_config = load_runtime_config()
    strategy_cfg = runtime_config.get("strategy", {}) if isinstance(runtime_config, dict) else {}
    return float(strategy_cfg.get("low_buy_cash_reference", 35454.23) or 35454.23)


def _special_low_buy_qty(code: str, holding: dict, price: float, stage: str = "intraday") -> int:
    code = str(code or "").strip()
    price = float(price or 0)
    if price <= 0:
        return 0
    ratio_map = {
        "688102": 0.18,
        "601698": 0.22,
        "300364": 0.10,
        "002639": 0.12,
        "588000": 0.42,  # 科创50ETF - 分批加仓，先加¥15,000，约8,200份
        "601998": 0.42,  # 中信银行 - 按计划加¥15,000，约1,900股
        "600089": 0.70,  # 特变电工 - 大幅加仓¥25,000，约1,090股
    }
    code_stage_factor_map = {
        "688102": {
            "open_trial": 0.18,
            "open_add": 0.26,
            "intraday_trial": 0.30,
            "intraday_add": 0.42,
            "eod_trial": 0.12,
            "eod_add": 0.18,
        },
        "601698": {
            "open_trial": 0.12,
            "open_add": 0.18,
            "intraday_trial": 0.22,
            "intraday_add": 0.32,
            "eod_trial": 0.08,
            "eod_add": 0.12,
        },
        "300364": {
            "open_trial": 0.10,
            "open_add": 0.12,
            "intraday_trial": 0.14,
            "intraday_add": 0.18,
            "eod_trial": 0.08,
            "eod_add": 0.10,
        },
        "002639": {
            "open_trial": 0.08,
            "open_add": 0.10,
            "intraday_trial": 0.12,
            "intraday_add": 0.16,
            "eod_trial": 0.06,
            "eod_add": 0.08,
        },
        "588000": {
            "open_trial": 0.18,
            "open_add": 0.20,
            "intraday_trial": 0.25,
            "intraday_add": 0.30,
            "eod_trial": 0.12,
            "eod_add": 0.15,
        },
        "601998": {
            "open_trial": 0.15,
            "open_add": 0.18,
            "intraday_trial": 0.20,
            "intraday_add": 0.30,
            "eod_trial": 0.10,
            "eod_add": 0.15,
        },
        "600089": {
            "open_trial": 0.25,
            "open_add": 0.35,
            "intraday_trial": 0.40,
            "intraday_add": 0.50,
            "eod_trial": 0.15,
            "eod_add": 0.20,
        },
    }
    stage_factor_map = {
        "open": 0.22,
        "open_trial": 0.22,
        "open_add": 0.30,
        "intraday": 0.35,
        "intraday_trial": 0.35,
        "intraday_add": 0.50,
        "eod": 0.15,
        "eod_trial": 0.15,
        "eod_add": 0.22,
    }
    ratio = float(ratio_map.get(code, 0.0) or 0.0)
    stage_key = str(stage or "intraday")
    stage_factor = float(code_stage_factor_map.get(code, {}).get(stage_key, stage_factor_map.get(stage_key, 0.45)) or 0.45)
    if ratio <= 0 or stage_factor <= 0:
        return 0
    cash_pool = _low_buy_cash_reference() * ratio * stage_factor
    qty = int((cash_pool // price) // 100 * 100)
    current_cap = int(holding.get("qty") or holding.get("t_qty") or holding.get("position_qty") or 0)
    if current_cap > 0:
        qty = min(qty, current_cap)
    return max(100, qty) if cash_pool >= price * 100 else 0


def _default_trade_qty(holding: dict, sig: Optional["Signal"] = None) -> int:
    if sig is not None and sig.action in {"BUY_LOW", "ADD_POS"}:
        special_qty = _special_low_buy_qty(sig.code, holding, float(getattr(sig, "price", 0) or 0))
        if special_qty > 0:
            return special_qty
    candidates = []
    if sig is not None:
        candidates.extend([
            sig.hold_qty,
            sig.factors.get("hold_qty", 0) if isinstance(sig.factors, dict) else 0,
            sig.factors.get("net_qty", 0) if isinstance(sig.factors, dict) else 0,
        ])
    candidates.extend([holding.get("t_qty"), holding.get("qty"), holding.get("position_qty")])
    for value in candidates:
        try:
            qty = int(value or 0)
        except Exception:
            qty = 0
        if qty > 0:
            return qty
    return 0


def _signal_qty(record: dict, fallback_qty: int = 0) -> int:
    try:
        qty = int(record.get("qty", 0) or 0)
    except Exception:
        qty = 0
    if qty > 0:
        return qty
    try:
        hold_qty = int(record.get("hold_qty", 0) or 0)
    except Exception:
        hold_qty = 0
    if hold_qty > 0:
        return hold_qty
    try:
        net_qty = int(record.get("net_qty", 0) or 0)
    except Exception:
        net_qty = 0
    if net_qty > 0:
        return net_qty
    return max(0, int(fallback_qty or 0))


def _sum_signal_qty(signals: List[dict], fallback_qty: int = 0) -> int:
    return sum(_signal_qty(item, fallback_qty) for item in signals)


def _qty_weight(qty: int, base_qty: int) -> float:
    qty = max(0, int(qty or 0))
    base_qty = max(100, int(base_qty or 0))
    weight = qty / base_qty if base_qty else 0.0
    return float(_clamp(weight, 0.5, 3.0))


def _snapshot_file(code: str, day: str) -> str:
    folder = os.path.join(SNAPSHOT_DIR, day[:4], day[5:7])
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{code}_{day}.json")


def _trace_path(kind: str, day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(TRACE_DIR, f"{kind}_{day}.jsonl")


def _preopen_path(day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(PREOPEN_DIR, f"preopen_{day}.json")


def _result_trace_path(day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(TRACE_DIR, f"signal_outcome_{day}.jsonl")


def _append_jsonl(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _register_signal_outcome(sig: "Signal", holding: dict) -> None:
    SIGNAL_OUTCOME_TRACKER.setdefault(sig.code, []).append({
        "signal_time": sig.ts,
        "action": sig.action,
        "signal_price": sig.price,
        "signal_score": sig.score,
        "vwap_at_signal": sig.indicators.get("vwap", sig.price),
        "market_state": sig.indicators.get("market_state", "unknown"),
        "benchmark_state": sig.indicators.get("benchmark_state", "unknown"),
        "benchmark_gate": sig.indicators.get("benchmark_gate", "neutral"),
        "qty": _default_trade_qty(holding, sig),
        "hold_qty": int(sig.factors.get("hold_qty", holding.get("t_qty", 0)) or 0),
        "name": sig.name,
        "price_points": [],
        "maturity_5m": False,
        "maturity_15m": False,
    })


def _snapshot_write(code: str, holding: dict, df: pd.DataFrame, indicators: dict, signal: Optional[dict] = None, daily_context: Optional[dict] = None) -> None:
    if df.empty:
        return
    day = str(df.iloc[-1].get("time", ""))[:10]
    if len(day) != 10:
        return
    path = _snapshot_file(code, day)
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    old_bars = existing.get("bars", []) if isinstance(existing, dict) else []
    new_bars = df[[c for c in ["time", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]].to_dict(orient="records")
    merged_bars: Dict[str, dict] = {}
    for row in old_bars + new_bars:
        ts = str(row.get("time", ""))
        if ts:
            merged_bars[ts] = row
    bars = [merged_bars[k] for k in sorted(merged_bars.keys())]
    rec = {
        "code": code,
        "name": holding.get("name", code),
        "date": day,
        "saved_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "row_count": int(len(bars)),
        "last_time": str(df.iloc[-1].get("time", "")),
        "last_close": float(df.iloc[-1].get("close", 0) or 0),
        "last_vwap": float(indicators.get("vwap", df.iloc[-1].get("close", 0)) or 0),
        "market_state": indicators.get("market_state", "unknown"),
        "benchmark_code": indicators.get("benchmark_code", ""),
        "benchmark_name": indicators.get("benchmark_name", ""),
        "benchmark_state": indicators.get("benchmark_state", "unknown"),
        "benchmark_gate": indicators.get("benchmark_gate", "neutral"),
        "benchmark_reason": indicators.get("benchmark_reason", ""),
        "signal": signal or existing.get("signal", {}) if isinstance(existing, dict) else (signal or {}),
        "daily_context": daily_context or (existing.get("daily_context", {}) if isinstance(existing, dict) else {}),
        "bars": bars,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)


def _benchmark_meta_for_code(code: str) -> Dict[str, str]:
    code = str(code or "").strip()
    if code.startswith(("688", "689")):
        return {"code": "sh000688", "name": "科创50", "market": "sh", "kind": "star"}
    if code.startswith(("300", "301")):
        return {"code": "sz399006", "name": "创业板指", "market": "sz", "kind": "chi_next"}
    if code.startswith(("60", "68", "90")):
        return {"code": "sh000001", "name": "上证指数", "market": "sh", "kind": "sse"}
    return {"code": "sz399001", "name": "深证成指", "market": "sz", "kind": "szse"}


def _default_daily_context(code: str, status: str = "unavailable", reason: str = "") -> Dict[str, Any]:
    return {
        "daily_status": status,
        "daily_reason": reason,
        "daily_asof": get_today_str(),
        "daily_price_ref": 0.0,
        "daily_prev_close": 0.0,
        "daily_day_ret": 0.0,
        "daily_ma5": 0.0,
        "daily_ma5_slope": 0.0,
        "daily_above_ma5": False,
        "daily_ma5_gap": 0.0,
        "daily_ma5_state": "unknown",
        "daily_ma10": 0.0,
        "daily_ma20": 0.0,
        "daily_ma30": 0.0,
        "daily_ma60": 0.0,
        "daily_ma10_slope": 0.0,
        "daily_ma20_slope": 0.0,
        "daily_ma30_slope": 0.0,
        "daily_ma60_slope": 0.0,
        "daily_trend_bg": "unknown",
        "daily_gate": "neutral",
        "daily_support_name": "",
        "daily_support_level": 0.0,
        "daily_support_gap": 0.0,
        "daily_near_support": False,
        "daily_pullback_support": False,
        "daily_breakdown_risk": False,
        "daily_hard_breakdown": False,
        "daily_overheated": False,
        "daily_ma_clustered": False,
        "daily_bull_aligned": False,
    }


def _fetch_daily_bar(code: str, is_etf: bool = False, as_of: Optional[str] = None) -> pd.DataFrame:
    try:
        import akshare as ak
        end_date = (as_of or _now().strftime("%Y%m%d")).replace("-", "")
        start_date = (_now() - timedelta(days=180)).strftime("%Y%m%d")
        if is_etf:
            for fn in ["fund_etf_hist_em", "fund_etf_hist_sina"]:
                if hasattr(ak, fn):
                    try:
                        df = getattr(ak, fn)(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
                        if isinstance(df, pd.DataFrame) and not df.empty:
                            break
                    except Exception:
                        df = pd.DataFrame()
                else:
                    df = pd.DataFrame()
        else:
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df is None or df.empty:
            return pd.DataFrame()
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame()
        df = df[keep_cols].copy()
        df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def _build_daily_context_from_df(code: str, df: pd.DataFrame, current_price: float = 0.0) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < PARAMS["daily_context_min_rows"]:
        return _default_daily_context(code, status="insufficient", reason=f"日线数据不足({0 if df is None else len(df)})")
    try:
        work = df.copy()
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce")
        work = work.dropna(subset=["date", "open", "close", "high", "low"]).reset_index(drop=True)
        if work.empty or len(work) < PARAMS["daily_context_min_rows"]:
            return _default_daily_context(code, status="insufficient", reason="清洗后日线不足")
        work["ma5"] = work["close"].rolling(5).mean()
        work["ma10"] = work["close"].rolling(10).mean()
        work["ma20"] = work["close"].rolling(20).mean()
        work["ma30"] = work["close"].rolling(30).mean()
        work["ma60"] = work["close"].rolling(60).mean()
        today = work.iloc[-1]
        prev = work.iloc[-2]
        ref_price = float(current_price or today["close"] or 0.0)
        prev_close = float(prev["close"] or 0.0)
        day_ret = (float(today["close"]) - prev_close) / prev_close if prev_close else 0.0
        ma5 = float(today["ma5"] or 0.0)
        ma10 = float(today["ma10"] or 0.0)
        ma20 = float(today["ma20"] or 0.0)
        ma30 = float(today["ma30"] or 0.0)
        ma60 = float(today["ma60"] or 0.0)
        ma5_prev = float(work.iloc[-6]["ma5"] or ma5) if len(work) >= 6 else ma5
        ma10_prev = float(work.iloc[-6]["ma10"] or ma10) if len(work) >= 6 else ma10
        ma20_prev = float(work.iloc[-6]["ma20"] or ma20) if len(work) >= 6 else ma20
        ma30_prev = float(work.iloc[-6]["ma30"] or ma30) if len(work) >= 6 else ma30
        ma60_prev = float(work.iloc[-6]["ma60"] or ma60) if len(work) >= 6 else ma60
        ma5_slope = (ma5 - ma5_prev) / ma5_prev if ma5_prev else 0.0
        ma10_slope = (ma10 - ma10_prev) / ma10_prev if ma10_prev else 0.0
        ma20_slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev else 0.0
        ma30_slope = (ma30 - ma30_prev) / ma30_prev if ma30_prev else 0.0
        ma60_slope = (ma60 - ma60_prev) / ma60_prev if ma60_prev else 0.0
        gap_to_ma5 = abs(ref_price - ma5) / ma5 if ma5 else 999.0
        gap_to_ma10 = abs(ref_price - ma10) / ma10 if ma10 else 999.0
        gap_to_ma20 = abs(ref_price - ma20) / ma20 if ma20 else 999.0
        gap_to_ma30 = abs(ref_price - ma30) / ma30 if ma30 else 999.0
        gap_to_ma60 = abs(ref_price - ma60) / ma60 if ma60 else 999.0
        near_candidates = []
        for level_name, level, gap in [("MA5", ma5, gap_to_ma5), ("MA20", ma20, gap_to_ma20), ("MA30", ma30, gap_to_ma30), ("MA60", ma60, gap_to_ma60)]:
            if level > 0 and gap <= PARAMS["daily_ma_support_loose_gap"]:
                near_candidates.append((gap, level_name, level))
        near_candidates.sort(key=lambda x: (x[0], x[1]))
        support_name = near_candidates[0][1] if near_candidates else ""
        support_level = float(near_candidates[0][2]) if near_candidates else 0.0
        support_gap = float(near_candidates[0][0]) if near_candidates else 0.0
        bull_aligned = ma10 > ma20 > ma30 > 0 and ma20_slope >= 0 and ma30_slope >= 0
        ma_clustered = ma20 > 0 and ma30 > 0 and abs(ma20 - ma30) / ma30 < 0.05 if ma30 else False
        trend_bg = "unknown"
        if ma60 and ref_price < ma60 * (1 - PARAMS["daily_ma_hard_breakdown_gap"]) and ma60_slope <= 0:
            trend_bg = "weak_breakdown"
        elif ma30 and ref_price < ma30 and ma30_slope < 0 and ma20 <= ma30:
            trend_bg = "downtrend"
        elif bull_aligned:
            trend_bg = "bull"
        elif ref_price >= ma20 > 0 and ma30_slope > 0 and ref_price >= ma60 * 0.97 if ma60 else False:
            trend_bg = "uptrend"
        elif ma_clustered and ref_price >= ma60 * 0.97 if ma60 else False:
            trend_bg = "base"
        elif ma30 > 0 and ref_price < ma30:
            trend_bg = "downtrend"
        else:
            trend_bg = "neutral"
        near_support = bool(support_name)
        pullback_support = near_support and trend_bg in {"bull", "uptrend", "base"} and not (ref_price < ma60 * (1 - PARAMS["daily_ma_breakdown_gap"]) if ma60 else False)
        breakdown_risk = False
        if ma20 > 0 and ma30 > 0:
            breakdown_risk = (ref_price < ma20 * (1 - PARAMS["daily_ma_breakdown_gap"]) and ref_price < ma30) or (ref_price < ma30 * (1 - PARAMS["daily_ma_breakdown_gap"]) and ma30_slope < 0)
        hard_breakdown = bool(ma60 and ref_price < ma60 * (1 - PARAMS["daily_ma_hard_breakdown_gap"]) and ma60_slope <= 0)
        overheated = False
        if ma10 > 0 and ref_price > ma10 * (1 + PARAMS["daily_overheat_ma10_gap"]):
            overheated = True
        if ma20 > 0 and ref_price > ma20 * (1 + PARAMS["daily_overheat_ma20_gap"]):
            overheated = True
        if day_ret > PARAMS["daily_overheat_day_ret"] and ma10 > 0 and ref_price > ma10 * 1.04:
            overheated = True
        if ma5 > 0 and gap_to_ma5 <= 0.01:
            ma5_state = "near_ma5_chop"
        elif ma5 > 0 and ref_price >= ma5 and ma5_slope >= 0:
            ma5_state = "above_ma5_trend"
        elif ma5 > 0 and (ref_price < ma5 or ma5_slope < 0):
            ma5_state = "below_ma5_weak"
        else:
            ma5_state = "unknown"
        if hard_breakdown or breakdown_risk:
            gate = "risk"
        elif overheated:
            gate = "overheat"
        elif pullback_support:
            gate = "supportive"
        elif trend_bg in {"downtrend", "weak_breakdown"}:
            gate = "caution"
        else:
            gate = "neutral"
        return {
            "daily_status": "ok",
            "daily_reason": "",
            "daily_asof": str(work.iloc[-1]["date"]),
            "daily_price_ref": ref_price,
            "daily_prev_close": prev_close,
            "daily_day_ret": day_ret,
            "daily_ma5": ma5,
            "daily_ma5_slope": ma5_slope,
            "daily_above_ma5": bool(ref_price >= ma5) if ma5 else False,
            "daily_ma5_gap": (ref_price - ma5) / ma5 if ma5 else 0.0,
            "daily_ma5_state": ma5_state,
            "daily_ma10": ma10,
            "daily_ma20": ma20,
            "daily_ma30": ma30,
            "daily_ma60": ma60,
            "daily_ma10_slope": ma10_slope,
            "daily_ma20_slope": ma20_slope,
            "daily_ma30_slope": ma30_slope,
            "daily_ma60_slope": ma60_slope,
            "daily_trend_bg": trend_bg,
            "daily_gate": gate,
            "daily_support_name": support_name,
            "daily_support_level": support_level,
            "daily_support_gap": support_gap,
            "daily_near_support": near_support,
            "daily_pullback_support": pullback_support,
            "daily_breakdown_risk": breakdown_risk,
            "daily_hard_breakdown": hard_breakdown,
            "daily_overheated": overheated,
            "daily_ma_clustered": ma_clustered,
            "daily_bull_aligned": bull_aligned,
        }
    except Exception as e:
        return _default_daily_context(code, status="error", reason=str(e)[:80])


def get_daily_context(code: str, holding: dict, current_price: float = 0.0, as_of: Optional[str] = None) -> Dict[str, Any]:
    if not PARAMS.get("daily_context_enabled", True):
        return _default_daily_context(code, status="disabled", reason="参数关闭")
    cache_key = f"{code}_{as_of or get_today_str()}"
    cached = DAILY_CONTEXT_CACHE.get(cache_key)
    if isinstance(cached, dict):
        ts = cached.get("ts")
        ctx = cached.get("ctx")
        if isinstance(ts, datetime) and isinstance(ctx, dict):
            if (_now() - ts).total_seconds() < PARAMS["daily_cache_ttl_seconds"]:
                return ctx
    try:
        df = _fetch_daily_bar(code, is_etf=holding.get("type") == "etf", as_of=as_of)
        if df.empty:
            ctx = _default_daily_context(code, status="unavailable", reason="日线拉取为空")
        else:
            ctx = _build_daily_context_from_df(code, df, current_price=current_price)
        DAILY_CONTEXT_CACHE[cache_key] = {"ts": _now(), "ctx": ctx}
        return ctx
    except Exception as e:
        ctx = _default_daily_context(code, status="error", reason=str(e)[:80])
        DAILY_CONTEXT_CACHE[cache_key] = {"ts": _now(), "ctx": ctx}
        return ctx


def _fetch_benchmark_minute_bar(meta: Dict[str, str]) -> pd.DataFrame:
    symbol = meta.get("code", "")
    if not symbol:
        return pd.DataFrame()

    last_error = ""
    for attempt in range(2):
        try:
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.qq.com/"
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read().decode("utf-8", errors="ignore")
                if not content.strip() or "<html" in content.lower() or "<!doctype html" in content.lower():
                    raise ValueError("benchmark minute response invalid")
                data = json.loads(content)
            minute_data = data.get("data", {}).get(symbol) or data.get("data", {}).get(symbol.replace("sh", "")) or data.get("data", {}).get(symbol.replace("sz", ""))
            if not minute_data:
                raise ValueError("benchmark minute symbol missing")
            rows = minute_data.get("data") or minute_data.get("day") or []
            if isinstance(rows, dict):
                rows = rows.get("data") or []

            parsed = []
            today_str = _now().strftime("%Y-%m-%d")
            for row in rows:
                if isinstance(row, str):
                    parts = row.split()
                elif isinstance(row, list):
                    parts = [str(x) for x in row]
                else:
                    continue
                if len(parts) >= 4:
                    tm = str(parts[0]).strip()
                    close_p = float(parts[1])
                    vol = float(parts[2])
                    amount = float(parts[3]) if len(parts) > 3 else np.nan
                    open_p = high_p = low_p = close_p
                    if len(parts) >= 6:
                        open_p, close_p, high_p, low_p, vol = map(float, parts[1:6])
                        amount = float(parts[6]) if len(parts) > 6 else amount
                    if tm.isdigit() and len(tm) in (3, 4):
                        tm = tm.zfill(4)
                        ts = f"{today_str} {tm[:2]}:{tm[2:]}:00"
                    elif ":" in tm and len(tm) <= 5:
                        ts = f"{today_str} {tm}:00"
                    else:
                        ts = tm
                    parsed.append({"time": ts, "open": open_p, "close": close_p, "high": high_p, "low": low_p, "volume": vol, "amount": amount})
            df = pd.DataFrame(parsed)
            if df.empty:
                raise ValueError("benchmark minute empty")
            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
            return df
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                time.sleep(0.6)
    log.debug(f"⚠️  指数分钟线获取失败 {meta.get('name', symbol)}[{symbol}]: {last_error[:80]}")
    return pd.DataFrame()


def _resolve_benchmark_snapshot(code: str, holding: dict) -> Dict[str, Any]:
    meta = _benchmark_meta_for_code(code)
    df = _fetch_benchmark_minute_bar(meta)
    snapshot: Dict[str, Any] = {
        "benchmark_code": meta.get("code", ""),
        "benchmark_name": meta.get("name", ""),
        "benchmark_kind": meta.get("kind", ""),
        "benchmark_state": "unknown",
        "benchmark_gate": "neutral",
        "benchmark_gate_reason": "指数数据不足",
        "benchmark_price": 0.0,
        "benchmark_vwap": 0.0,
        "benchmark_today_ret": 0.0,
        "benchmark_vol_ratio": 0.0,
        "benchmark_momentum": 0.0,
    }
    if df.empty:
        return snapshot

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    price = float(last["close"]) if pd.notna(last.get("close")) else 0.0
    vwap = float(last["vwap"]) if pd.notna(last.get("vwap")) else price
    today_open = float(df[df["date"] == last["date"]].iloc[0]["open"]) if "date" in df.columns and not df[df["date"] == last["date"]].empty else price
    today_ret = (price - today_open) / today_open if today_open > 0 else 0.0
    vol_ratio = float(last["vol_ratio"]) if pd.notna(last.get("vol_ratio")) else 1.0
    momentum = float(last["macd_hist"]) if pd.notna(last.get("macd_hist")) else 0.0
    ema_spread = float(last["ema_spread"]) if pd.notna(last.get("ema_spread")) else 0.0
    day_amplitude = float(last["day_amplitude"]) if pd.notna(last.get("day_amplitude")) else 0.0
    state = "range_bound"
    if day_amplitude < PARAMS["min_amplitude"]:
        state = "dead_water"
    elif today_ret >= PARAMS["trend_today_ret_threshold"] and price >= vwap and ema_spread >= 0 and vol_ratio >= 1.05:
        state = "trend_up"
    elif today_ret <= -PARAMS["trend_today_ret_threshold"] and price <= vwap and ema_spread <= 0:
        state = "trend_down"
    elif price >= vwap and momentum >= 0:
        state = "bias_up"
    elif price <= vwap and momentum <= 0:
        state = "bias_down"

    benchmark_gate = "neutral"
    benchmark_reason = "指数中性"
    if state in {"trend_down", "bias_down"} and today_ret < 0:
        benchmark_gate = "weak"
        benchmark_reason = "指数偏弱，抬高买入门槛"
    elif state in {"trend_up", "bias_up"} and today_ret >= 0:
        benchmark_gate = "strong"
        benchmark_reason = "指数偏强，允许顺势低吸/加仓"
    elif state == "dead_water":
        benchmark_gate = "weak"
        benchmark_reason = "指数波动不足，谨慎出手"

    snapshot.update({
        "benchmark_state": state,
        "benchmark_gate": benchmark_gate,
        "benchmark_gate_reason": benchmark_reason,
        "benchmark_price": price,
        "benchmark_vwap": vwap,
        "benchmark_today_ret": today_ret,
        "benchmark_vol_ratio": vol_ratio,
        "benchmark_momentum": momentum,
        "benchmark_ema_spread": ema_spread,
        "benchmark_day_amplitude": day_amplitude,
        "benchmark_prev_close": float(prev["close"]) if pd.notna(prev.get("close")) else price,
    })
    return snapshot

def fetch_stock_name(code: str, is_etf: bool = False) -> str:
    if code in _name_cache: return _name_cache[code]
    try:
        df = ak.fund_etf_spot_em() if is_etf else ak.stock_bid_ask_em(symbol=code)
        if is_etf:
            row = df[df["代码"] == code]
            name = row.iloc[0]["名称"] if not row.empty else code
        else:
            snap = dict(zip(df["item"], df["value"]))
            name = snap.get("股票简称") or snap.get("名称") or code
        _name_cache[code] = name
        return name
    except: return code

def label(code: str, holding: dict) -> str:
    return f"{holding.get('name') or code}({code})"

def load_strategy_memory() -> Dict[str, dict]:
    if not os.path.exists(LEARNING_FILE):
        return {}
    try:
        with open(LEARNING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _strategy_memory_for_code(code: str) -> Dict[str, Any]:
    base = {}
    if isinstance(STRATEGY_MEMORY, dict):
        global_mem = STRATEGY_MEMORY.get("GLOBAL", {})
        if isinstance(global_mem, dict):
            base.update(global_mem)
        code_mem = STRATEGY_MEMORY.get(code, {})
        if isinstance(code_mem, dict):
            base.update(code_mem)
    return base


def load_starvation_state() -> Dict[str, dict]:
    path = _starvation_state_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_starvation_state(state: Dict[str, dict]):
    try:
        with open(_starvation_state_file(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_watchlist() -> Dict[str, dict]:
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_holdings() -> Dict[str, dict]:
    global STRATEGY_MEMORY
    if not os.path.exists(HOLDINGS_FILE): return {}
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            holdings = json.load(f)
    except json.JSONDecodeError as e:
        log.error(f"❌ holdings.json 格式错误: {e}。请检查标点符号是否遗漏！")
        return {}

    STRATEGY_MEMORY = load_strategy_memory()
    for code, h in holdings.items():
        if not h.get("name"):
            h["name"] = code
    return holdings

@dataclass
class Signal:
    code: str
    name: str
    action: str
    price: float
    score: float
    reasons: List[str] = field(default_factory=list)
    details: List[Dict[str, Any]] = field(default_factory=list)
    indicators: Dict[str, float] = field(default_factory=dict)
    factors: Dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=datetime.now)
    cycle_id: str = ""
    cycle_action_count: int = 0
    hold_qty: int = 0

def _minute_cache_file(code: str, market_date: str) -> str:
    return os.path.join(CACHE_DIR, f"minute_{code}_{market_date}.csv")


def _load_minute_cache(code: str, market_date: str) -> pd.DataFrame:
    cache_file = _minute_cache_file(code, market_date)
    if not os.path.exists(cache_file):
        return pd.DataFrame()

    try:
        age = _now().timestamp() - os.path.getmtime(cache_file)
        if age > PARAMS["cache_ttl_seconds"]:
            return pd.DataFrame()

        df = pd.read_csv(cache_file)
        if not df.empty and "time" in df.columns:
            df["time"] = df["time"].astype(str).str.strip()
            mask = df["time"].str.fullmatch(r"\d{3,4}", na=False)
            if mask.any():
                padded = df.loc[mask, "time"].str.zfill(4)
                df.loc[mask, "time"] = padded.str.slice(0, 2) + ":" + padded.str.slice(2, 4) + ":00"
        if not df.empty:
            return df
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def _save_minute_cache(code: str, market_date: str, df: pd.DataFrame):
    try:
        df.to_csv(_minute_cache_file(code, market_date), index=False, encoding="utf-8")
    except Exception:
        pass


def cleanup_expired_minute_cache():
    """清理过期分钟线缓存"""
    try:
        if not os.path.exists(CACHE_DIR):
            return

        now_ts = _now().timestamp()
        removed = 0
        for filename in os.listdir(CACHE_DIR):
            if not filename.startswith("minute_") or not filename.endswith(".csv"):
                continue
            file_path = os.path.join(CACHE_DIR, filename)
            try:
                age = now_ts - os.path.getmtime(file_path)
                if age > PARAMS["cache_ttl_seconds"] * 10:
                    os.remove(file_path)
                    removed += 1
            except Exception as e:
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

        if removed:
            log.info(f"🧹 清理过期分钟线缓存 {removed} 个")
    except Exception as e:
        log.debug(f"⚠️  清理缓存失败: {str(e)[:60]}")


def fetch_minute_bar(code: str, is_etf: bool = False) -> pd.DataFrame:
    """获取分钟线数据，优先使用本地缓存，再使用直连接口。"""
    market_date = _now().strftime("%Y-%m-%d")
    fetch_started = _now()
    MINUTE_FETCH_DETAIL[code] = ""

    cached = _load_minute_cache(code, market_date)
    if not cached.empty:
        MINUTE_FETCH_STATUS[code] = "cache_hit"
        log.debug(f"♻️  {code} 命中分钟线缓存")
        _append_jsonl(_trace_path("data_quality", market_date), {
            "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
            "source": "cache",
            "minute_status": "cache_hit",
            "raw_rows": int(len(cached)),
            "parsed_rows": int(len(cached)),
            "valid_rows": int(len(cached)),
            "fetch_cost_ms": int((_now() - fetch_started).total_seconds() * 1000),
        })
        return cached

    last_error = ""
    for attempt in range(3):
        try:
            market = "sh" if code.startswith(("5", "6", "9")) else "sz"
            symbol = f"{market}{code}"
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.qq.com/"
            }

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read().decode("utf-8", errors="ignore")
                if not content.strip():
                    MINUTE_FETCH_STATUS[code] = "json_empty"
                    MINUTE_FETCH_DETAIL[code] = "响应体为空"
                    raise ValueError("empty response body")
                if "<html" in content.lower() or "<!doctype html" in content.lower():
                    MINUTE_FETCH_STATUS[code] = "json_html"
                    MINUTE_FETCH_DETAIL[code] = "响应像HTML拦截页"
                    raise ValueError("html response body")
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    MINUTE_FETCH_STATUS[code] = "json_error"
                    MINUTE_FETCH_DETAIL[code] = f"非JSON响应: {content[:80]}"
                    raise ValueError("json decode error")

            if data.get("code") != 0 or not data.get("data"):
                MINUTE_FETCH_STATUS[code] = "api_empty"
                MINUTE_FETCH_DETAIL[code] = f"返回code={data.get('code')} data为空"
                raise ValueError("minute api returned empty data")

            minute_data = data["data"].get(symbol) or data["data"].get(code)
            if not minute_data:
                MINUTE_FETCH_STATUS[code] = "symbol_missing"
                MINUTE_FETCH_DETAIL[code] = f"data中未找到{symbol}或{code}"
                raise ValueError("minute api missing symbol data")

            rows = minute_data.get("data") or minute_data.get("day") or []
            if isinstance(rows, dict):
                rows = rows.get("data") or []

            parsed = []
            today_str = _now().strftime("%Y-%m-%d")
            total_rows = len(rows) if hasattr(rows, "__len__") else 0
            if total_rows == 1 and isinstance(rows[0], str) and rows[0].strip() == "0":
                MINUTE_FETCH_STATUS[code] = "parse_zero_placeholder"
                MINUTE_FETCH_DETAIL[code] = "接口返回占位0行，不是有效分钟数据"
                raise ValueError("minute api returned zero placeholder")
            short_rows = 0
            type_rows = 0
            parse_fail_rows = 0
            derived_ohlc_rows = 0
            for row in rows:
                try:
                    if isinstance(row, str):
                        parts = row.split()
                    elif isinstance(row, list):
                        parts = [str(x) for x in row]
                    else:
                        type_rows += 1
                        continue

                    if len(parts) >= 6:
                        tm = parts[0]
                        open_p, close_p, high_p, low_p, vol = map(float, parts[1:6])
                        amount = float(parts[6]) if len(parts) > 6 else np.nan
                    elif len(parts) >= 4:
                        tm = parts[0]
                        close_p = float(parts[1])
                        vol = float(parts[2])
                        amount = float(parts[3]) if len(parts) > 3 else np.nan
                        open_p = high_p = low_p = close_p
                        derived_ohlc_rows += 1
                    else:
                        short_rows += 1
                        continue

                    tm = str(tm).strip()
                    if tm.isdigit() and len(tm) in (3, 4):
                        tm = tm.zfill(4)
                        ts = f"{today_str} {tm[:2]}:{tm[2:]}:00"
                    elif ":" in tm and len(tm) <= 5:
                        ts = f"{today_str} {tm}:00"
                    else:
                        ts = tm

                    parsed.append({
                        "time": ts,
                        "open": open_p,
                        "close": close_p,
                        "high": high_p,
                        "low": low_p,
                        "volume": vol,
                        "amount": amount,
                    })
                except Exception:
                    parse_fail_rows += 1
                    continue

            df = pd.DataFrame(parsed)
            if df.empty:
                if total_rows == 0:
                    MINUTE_FETCH_STATUS[code] = "parse_no_rows"
                    MINUTE_FETCH_DETAIL[code] = "接口返回0行分钟数据"
                elif short_rows == total_rows:
                    MINUTE_FETCH_STATUS[code] = "parse_short_rows"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，全部字段不足4列"
                elif type_rows == total_rows:
                    MINUTE_FETCH_STATUS[code] = "parse_type_rows"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，全部为不支持的行类型"
                elif parse_fail_rows == total_rows:
                    MINUTE_FETCH_STATUS[code] = "parse_value_error"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，全部在数值转换时失败"
                else:
                    MINUTE_FETCH_STATUS[code] = "parse_empty"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，短行{short_rows}，类型行{type_rows}，解析失败{parse_fail_rows}"
                raise ValueError("no parsed minute rows")

            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
            _save_minute_cache(code, market_date, df)
            MINUTE_FETCH_STATUS[code] = "ok"
            MINUTE_FETCH_DETAIL[code] = f"解析成功{len(df)}行，4列行{derived_ohlc_rows}，跳过短行{short_rows}，类型行{type_rows}，解析失败{parse_fail_rows}"
            _append_jsonl(_trace_path("data_quality", market_date), {
                "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code,
                "source": "api",
                "minute_status": "ok",
                "raw_rows": int(total_rows),
                "parsed_rows": int(len(df)),
                "valid_rows": int(len(df)),
                "short_rows": int(short_rows),
                "type_rows": int(type_rows),
                "parse_fail_rows": int(parse_fail_rows),
                "derived_ohlc_rows": int(derived_ohlc_rows),
                "fetch_cost_ms": int((_now() - fetch_started).total_seconds() * 1000),
            })
            return df

        except urllib.error.URLError as e:
            last_error = str(e)
            reason = getattr(e, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in last_error.lower():
                MINUTE_FETCH_STATUS[code] = "network_timeout"
                MINUTE_FETCH_DETAIL[code] = f"请求超时: {last_error[:80]}"
            elif isinstance(reason, OSError):
                err_text = str(reason).lower()
                if "name or service not known" in err_text or "temporary failure" in err_text or "dns" in err_text:
                    MINUTE_FETCH_STATUS[code] = "network_dns"
                    MINUTE_FETCH_DETAIL[code] = f"DNS解析失败: {last_error[:80]}"
                elif "ssl" in err_text or "certificate" in err_text:
                    MINUTE_FETCH_STATUS[code] = "network_ssl"
                    MINUTE_FETCH_DETAIL[code] = f"SSL握手失败: {last_error[:80]}"
                else:
                    MINUTE_FETCH_STATUS[code] = "network_error"
                    MINUTE_FETCH_DETAIL[code] = f"网络错误: {last_error[:80]}"
            elif hasattr(reason, "code"):
                MINUTE_FETCH_STATUS[code] = "network_http"
                MINUTE_FETCH_DETAIL[code] = f"HTTP错误{getattr(reason, 'code', '')}: {last_error[:80]}"
            else:
                MINUTE_FETCH_STATUS[code] = "network_error"
                MINUTE_FETCH_DETAIL[code] = f"网络错误: {last_error[:80]}"
        except Exception as e:
            last_error = str(e)
            if MINUTE_FETCH_STATUS.get(code) not in {"json_empty", "json_html", "json_error", "api_empty", "symbol_missing", "parse_no_rows", "parse_short_rows", "parse_type_rows", "parse_value_error", "parse_zero_placeholder", "parse_empty"}:
                MINUTE_FETCH_STATUS[code] = "network_error"
                MINUTE_FETCH_DETAIL[code] = f"其他异常: {last_error[:80]}"
        if attempt < 2:
            time.sleep(0.8)

    log.warning(f"⚠️  {code} 分钟线获取失败[{MINUTE_FETCH_STATUS.get(code, 'unknown')}]: {MINUTE_FETCH_DETAIL.get(code, last_error[:60])}")
    return pd.DataFrame()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 2:
        return df
    c = df["close"]

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(PARAMS["rsi_period"], min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(PARAMS["rsi_period"], min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

    ma = c.rolling(PARAMS["bb_period"], min_periods=1).mean()
    sd = c.rolling(PARAMS["bb_period"], min_periods=1).std()
    df["bb_up"] = ma + PARAMS["bb_std"] * sd
    df["bb_dn"] = ma - PARAMS["bb_std"] * sd
    band_width = (df["bb_up"] - df["bb_dn"]).replace(0, np.nan)
    df["bb_pct"] = (c - df["bb_dn"]) / band_width

    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = exp1 - exp2
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd"] - df["macd_signal"]) * 2

    df["ema_fast"] = c.ewm(span=PARAMS["ema_fast_period"], adjust=False).mean()
    df["ema_slow"] = c.ewm(span=PARAMS["ema_slow_period"], adjust=False).mean()
    df["ema_spread"] = (df["ema_fast"] - df["ema_slow"]) / df["ema_slow"].replace(0, np.nan)

    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = tp * df["volume"]
    time_text = df["time"].astype(str).str.strip()
    parsed_time = pd.to_datetime(time_text, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    if parsed_time.isna().all():
        parsed_hms = pd.to_datetime(time_text, format="%H:%M:%S", errors="coerce")
        if parsed_hms.notna().all():
            parsed_time = pd.Timestamp.now().normalize() + (parsed_hms - parsed_hms.dt.normalize())
        else:
            parsed_hm = pd.to_datetime(time_text, format="%H:%M", errors="coerce")
            if parsed_hm.notna().all():
                parsed_time = pd.Timestamp.now().normalize() + (parsed_hm - parsed_hm.dt.normalize())
    df["date"] = parsed_time.dt.date

    df["vwap"] = df.groupby("date")["tp_vol"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["vwap"].ffill().fillna(df["close"])
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"].replace(0, np.nan)

    day_high = df.groupby("date")["high"].transform("max")
    day_low = df.groupby("date")["low"].transform("min")
    df["day_amplitude"] = (day_high - day_low) / day_low.replace(0, np.nan)
    df["range_pos"] = (c - day_low) / (day_high - day_low + 1e-9)

    last_date = df["date"].iloc[-1]
    prev_data = df[df["date"] < last_date]
    df["prev_high"] = prev_data["high"].max() if not prev_data.empty else df["high"].rolling(120).max()

    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    df["mom5"] = c.pct_change(5)

    k_length = df["high"] - df["low"] + 1e-5
    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / k_length
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / k_length

    return df

class SignalEngine:
    def __init__(self):
        self.buy_cooldown: Dict[str, datetime] = {}
        self.sell_cooldown: Dict[str, datetime] = {}
        self.buy_count_per_stock: Dict[str, int] = {}
        self.sell_count_per_stock: Dict[str, int] = {}
        self.state_reset_date = get_today_str()
        self.t_cycle_start_time: Dict[str, datetime] = {}
        self.last_signal_state: Dict[str, Dict[str, Any]] = {}
        self.last_trade_state: Dict[str, Dict[str, Any]] = {}
        self.cycle_count: Dict[str, int] = {}
        self.cycle_direction: Dict[str, str] = {}
        self.post_sell_block_until: Dict[str, datetime] = {}
        self.daily_realized_loss_monitor = 0.0
        self.diagnostics: Dict[str, Dict[str, Any]] = {}
    def _reset_daily_state_if_needed(self):
        today = get_today_str()
        if self.state_reset_date != today:
            self.buy_count_per_stock = {}
            self.sell_count_per_stock = {}
            self.t_cycle_start_time = {}
            self.last_signal_state = {}
            self.last_trade_state = {}
            self.cycle_count = {}
            self.cycle_direction = {}
            self.post_sell_block_until = {}
            self.daily_realized_loss_monitor = 0.0
            self.diagnostics = {}
            self.state_reset_date = today
    def _in_cooldown(self, code: str, action: str) -> bool:
        cd_dict = self.sell_cooldown if "SELL" in action else self.buy_cooldown
        last = cd_dict.get(code)
        return bool(last) and (_now() - last).total_seconds() < PARAMS["cooldown_minutes"] * 60

    def record_signal(self, code: str, action: str, price: float, score: float):
        snapshot = self.last_signal_state.setdefault(code, {})
        snapshot["action"] = action
        snapshot["price"] = price
        snapshot["score"] = score
        snapshot["ts"] = _now()
        if "SELL" in action:
            self.sell_cooldown[code] = _now()
        else:
            self.buy_cooldown[code] = _now()

    def record_trade_action(self, code: str, action: str, qty: int = 0):
        self._reset_daily_state_if_needed()
        self.last_trade_state[code] = {"action": action, "qty": qty, "ts": _now()}
        if action in ["BUY_LOW", "ADD_POS"]:
            self.buy_count_per_stock[code] = self.buy_count_per_stock.get(code, 0) + 1
            self.t_cycle_start_time.setdefault(code, _now())
            self.cycle_direction[code] = "buy"
        elif action in ["SELL_HIGH", "PANIC_SELL"]:
            self.sell_count_per_stock[code] = self.sell_count_per_stock.get(code, 0) + 1
            self.cycle_direction[code] = "sell"
            self.post_sell_block_until[code] = _now() + timedelta(minutes=PARAMS["post_sell_rebuild_minutes"])
            buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
            sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
            net_qty = sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells)
            if qty > 0:
                net_qty -= qty
            if net_qty <= 0 and code in self.t_cycle_start_time:
                del self.t_cycle_start_time[code]

    def _virtual_net_qty(self, code: str, holding: dict) -> int:
        buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
        sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
        base_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
        return max(0, base_qty + sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells))

    def _is_redundant_signal(self, code: str, action: str, price: float, score: float) -> bool:
        if action in ["SELL_HIGH", "PANIC_SELL"]:
            last_trade = self.last_trade_state.get(code, {})
            last_action = last_trade.get("action")
            last_ts = last_trade.get("ts")
            if last_action in ["SELL_HIGH", "PANIC_SELL"] and isinstance(last_ts, datetime):
                elapsed = (_now() - last_ts).total_seconds() / 60
                if elapsed < PARAMS["sell_repeat_block_minutes"]:
                    return True
        snapshot = self.last_signal_state.get(code)
        if not snapshot:
            return False
        if snapshot.get("action") != action:
            return False
        last_ts = snapshot.get("ts")
        if not isinstance(last_ts, datetime):
            return False
        elapsed = (_now() - last_ts).total_seconds() / 60
        if elapsed >= PARAMS["repeat_signal_gap_minutes"]:
            return False
        last_price = float(snapshot.get("price") or 0)
        price_move = abs(price - last_price) / last_price if last_price else 1.0
        last_score = float(snapshot.get("score") or 0)
        if price_move < PARAMS["repeat_signal_price_move"] and score <= last_score + PARAMS["repeat_signal_score_boost"]:
            return True
        return False

    def _should_stand_down(self, code: str, holding: dict, df: pd.DataFrame, buy_score: float, sell_score: float, market_state: str, can_sell: bool) -> tuple[bool, str]:
        if df.empty:
            return True, "分钟数据为空"
        if market_state == "dead_water":
            return True, "日内波动过低"
        last = df.iloc[-1]
        vwap = float(last["vwap"]) if pd.notna(last["vwap"]) else 0.0
        price = float(last["close"]) if pd.notna(last["close"]) else 0.0
        range_pos = float(last["range_pos"]) if pd.notna(last["range_pos"]) else 0.5
        gap = abs(price - vwap) / vwap if vwap else 0.0
        if not can_sell and buy_score < 28:
            return True, "无可卖仓且买点不强"
        if can_sell and buy_score < 28 and sell_score < 35:
            return True, "买卖都不够强"
        if market_state == "range_bound" and gap < PARAMS["stand_down_flat_range_gap"] and abs(buy_score - sell_score) < PARAMS["stand_down_score_gap"]:
            return True, "震荡贴均且分差不大"
        if holding.get("type") != "etf" and range_pos > 0.85 and sell_score < 45 and buy_score < 45:
            return True, "高位但无明确优势"
        if holding.get("type") == "etf" and gap < 0.003 and buy_score < 44 and sell_score < 44:
            return True, "ETF波动不足"
        return False, ""

    def _classify_market_state(self, today_ret: float, price: float, vwap: float, vol_ratio: float, day_amplitude: float, ema_spread: float) -> str:
        if day_amplitude < PARAMS["min_amplitude"]:
            return "dead_water"
        if today_ret >= PARAMS["trend_today_ret_threshold"] and price >= vwap and ema_spread >= 0 and vol_ratio >= 1.1:
            return "trend_up"
        if today_ret <= -PARAMS["trend_today_ret_threshold"] and price <= vwap and ema_spread <= 0:
            return "trend_down"
        return "range_bound"

    def _dynamic_threshold(self, side: str, price: float, vwap: float, rsi: float, vol_ratio: float, holding: dict, market_state: str, is_strong_pullback: bool = False, code: str = "") -> int:
        memory = _strategy_memory_for_code(code)
        is_etf = holding.get("type") == "etf"
        base = 40 if is_etf else 45
        if side == "buy":
            base += int(memory.get("buy_threshold_adj", 0))
        else:
            base += int(memory.get("sell_threshold_adj", 0))
        if side == "buy":
            if price < vwap:
                base -= 2
            if rsi <= PARAMS["rsi_oversold"]:
                base -= 2
            if vol_ratio >= PARAMS["vol_ratio_confirm"]:
                base -= 1
            if market_state == "trend_down":
                base += PARAMS["market_state_threshold_bias"] + 2
            elif market_state == "trend_up" and not is_strong_pullback:
                base += 3
            elif market_state == "range_bound":
                base += 1
        else:
            if price > vwap:
                base -= 3
            if rsi >= PARAMS["rsi_overbought"]:
                base -= 3
            if vol_ratio >= PARAMS["vol_ratio_confirm"]:
                base -= 2
            if market_state == "trend_down":
                base -= 2
            elif market_state == "trend_up":
                base += 2
        if vwap and abs(price - vwap) / vwap < 0.002:
            base += 2
        if side == "buy" and not is_strong_pullback:
            base += 1
        return max(35, min(60, base))

    def evaluate(self, code, name, df, holding, daily_ctx=None):
        if df.empty or len(df) < 15:
            return 0, 0, None

        daily_ctx = daily_ctx if isinstance(daily_ctx, dict) else _default_daily_context(code)

        minute_status = MINUTE_FETCH_STATUS.get(code, "unknown")
        if minute_status not in {"ok", "cache_hit"}:
            return 0, 0, None

        self._reset_daily_state_if_needed()
        last = df.iloc[-1]
        prev = df.iloc[-2]
        memory = _strategy_memory_for_code(code)
        starvation_state = load_starvation_state().get(code, {})
        starvation_days = int(starvation_state.get("days", 0) or 0)
        starvation_relax_until = str(starvation_state.get("relax_until", "") or "")
        starvation_relax_active = bool(starvation_relax_until and starvation_relax_until >= get_today_str())
        buy_confirm_min_score = int(memory.get("buy_confirm_min_score", PARAMS["buy_confirm_min_score"]))
        buy_confirm_min_factors = int(memory.get("buy_confirm_min_factors", PARAMS["buy_confirm_min_factors"]))
        buy_confirm_min_seconds = int(memory.get("buy_confirm_min_seconds", PARAMS["buy_confirm_min_seconds"]))
        buy_rebound_min_score_gap = int(memory.get("buy_rebound_min_score_gap", PARAMS["buy_rebound_min_score_gap"]))
        if starvation_relax_active and starvation_days >= PARAMS["buy_starvation_days"]:
            buy_confirm_min_seconds = max(20, buy_confirm_min_seconds - PARAMS["buy_starvation_relax_seconds"])
            buy_confirm_min_factors = max(2, buy_confirm_min_factors - PARAMS["buy_starvation_relax_factors"])
            buy_rebound_min_score_gap = max(2, buy_rebound_min_score_gap - PARAMS["buy_starvation_relax_gap"])
        sell_confirm_min_factors = int(memory.get("sell_confirm_min_factors", PARAMS["sell_confirm_min_factors"]))
        sell_confirm_min_seconds = int(memory.get("sell_confirm_min_seconds", PARAMS["sell_confirm_min_seconds"]))
        buy_needs_momentum = bool(memory.get("buy_needs_momentum", True))
        buy_needs_ema = bool(memory.get("buy_needs_ema", True))
        buy_needs_volume = bool(memory.get("buy_needs_volume", True))
        sell_needs_momentum = bool(memory.get("sell_needs_momentum", True))
        sell_needs_ema = bool(memory.get("sell_needs_ema", True))
        sell_needs_volume = bool(memory.get("sell_needs_volume", True))
        buy_min_time = str(memory.get("buy_min_time", "09:40"))

        price = float(last["close"])
        vwap = float(last["vwap"])
        day_amplitude = float(last["day_amplitude"]) if pd.notna(last["day_amplitude"]) else 0.0
        dt_time = pd.to_datetime(last["time"])
        t_val = dt_time.hour * 100 + dt_time.minute
        current_minute = dt_time.hour * 60 + dt_time.minute
        try:
            min_hour, min_minute = [int(x) for x in buy_min_time.split(":", 1)]
            min_trade_minute = min_hour * 60 + min_minute
        except Exception:
            min_trade_minute = 9 * 60 + 40

        # V1.11: 早盘冲高窗口降低卖出确认门槛（必须在t_val定义后）
        if 930 <= t_val <= 940:
            sell_confirm_min_seconds = max(20, sell_confirm_min_seconds - 40)
            sell_confirm_min_factors = max(4, sell_confirm_min_factors - 3)

        rsi = float(last["rsi"]) if pd.notna(last["rsi"]) else 50
        bb_pct = float(last["bb_pct"]) if pd.notna(last["bb_pct"]) else 0.5
        macd_hist = float(last["macd_hist"]) if pd.notna(last["macd_hist"]) else 0.0
        prev_macd_hist = float(prev["macd_hist"]) if pd.notna(prev["macd_hist"]) else 0.0
        ema_spread = float(last["ema_spread"]) if pd.notna(last["ema_spread"]) else 0.0
        prev_ema_spread = float(prev["ema_spread"]) if pd.notna(prev["ema_spread"]) else 0.0
        range_pos = float(last["range_pos"]) if pd.notna(last["range_pos"]) else 0.5

        vol_ratio = float(last["vol_ratio"]) if pd.notna(last["vol_ratio"]) else 1.0
        mom5 = float(last["mom5"]) if pd.notna(last["mom5"]) else 0.0
        lower_shadow = float(last["lower_shadow"]) if pd.notna(last["lower_shadow"]) else 0.0
        upper_shadow = float(last["upper_shadow"]) if pd.notna(last["upper_shadow"]) else 0.0

        today_open = float(df[df["date"] == last["date"]].iloc[0]["open"])
        today_ret = (price - today_open) / today_open if today_open > 0 else 0.0
        prev_high = float(last["prev_high"]) if pd.notna(last["prev_high"]) else price
        is_strong_trend = (today_ret > 0.035) and (price >= prev_high * 0.99) and (vol_ratio > 1.2)
        is_strong_pullback = is_strong_trend and abs((price - vwap) / vwap) < 0.005 if vwap else False

        benchmark = _resolve_benchmark_snapshot(code, holding)
        market_state = self._classify_market_state(today_ret, price, vwap, vol_ratio, day_amplitude, ema_spread)
        benchmark = benchmark if isinstance(benchmark, dict) else {}
        benchmark_state = benchmark.get("benchmark_state", "unknown")
        benchmark_gate = benchmark.get("benchmark_gate", "neutral")
        benchmark_reason = benchmark.get("benchmark_gate_reason", "")
        daily_ctx = daily_ctx if isinstance(daily_ctx, dict) else {}
        daily_status = daily_ctx.get("daily_status", "unknown")
        daily_gate = daily_ctx.get("daily_gate", "neutral")
        daily_trend_bg = daily_ctx.get("daily_trend_bg", "unknown")
        daily_ma5 = float(daily_ctx.get("daily_ma5", 0.0) or 0.0)
        daily_ma5_slope = float(daily_ctx.get("daily_ma5_slope", 0.0) or 0.0)
        daily_above_ma5 = bool(daily_ctx.get("daily_above_ma5", False))
        daily_ma5_gap = float(daily_ctx.get("daily_ma5_gap", 0.0) or 0.0)
        daily_ma5_state = str(daily_ctx.get("daily_ma5_state", "unknown") or "unknown")
        daily_buy_t_ok = daily_status == "ok" and daily_ma5 > 0 and daily_ma5_state in {"near_ma5_chop", "above_ma5_trend"}
        daily_buy_t_relaxed = daily_buy_t_ok and daily_ma5_state == "above_ma5_trend"
        daily_sell_t_preferred = daily_ma5_state == "below_ma5_weak"
        daily_support_gap = float(daily_ctx.get("daily_support_gap", 0.0) or 0.0)
        daily_breakdown_risk = bool(daily_ctx.get("daily_breakdown_risk", False))
        daily_hard_breakdown = bool(daily_ctx.get("daily_hard_breakdown", False))
        daily_overheated = bool(daily_ctx.get("daily_overheated", False))
        daily_pullback_support = bool(daily_ctx.get("daily_pullback_support", False))
        daily_near_support = bool(daily_ctx.get("daily_near_support", False))
        indicators = {
            "price": price,
            "rsi": rsi,
            "bb_pct": bb_pct,
            "vwap": vwap,
            "ema_spread": ema_spread,
            "range_pos": range_pos,
            "market_state": market_state,
            "benchmark_code": benchmark.get("benchmark_code", ""),
            "benchmark_name": benchmark.get("benchmark_name", ""),
            "benchmark_state": benchmark_state,
            "benchmark_gate": benchmark_gate,
            "benchmark_reason": benchmark_reason,
            "daily_status": daily_ctx.get("daily_status", "unknown"),
            "daily_gate": daily_ctx.get("daily_gate", "neutral"),
            "daily_trend_bg": daily_ctx.get("daily_trend_bg", "unknown"),
            "daily_ma5": daily_ctx.get("daily_ma5", 0.0),
            "daily_ma5_slope": daily_ctx.get("daily_ma5_slope", 0.0),
            "daily_above_ma5": daily_ctx.get("daily_above_ma5", False),
            "daily_ma5_gap": daily_ctx.get("daily_ma5_gap", 0.0),
            "daily_ma5_state": daily_ctx.get("daily_ma5_state", "unknown"),
            "daily_buy_t_ok": daily_buy_t_ok,
            "daily_sell_t_preferred": daily_sell_t_preferred,
            "daily_buy_t_relaxed": daily_buy_t_relaxed,
            "daily_ma10": daily_ctx.get("daily_ma10", 0.0),
            "daily_ma20": daily_ctx.get("daily_ma20", 0.0),
            "daily_ma30": daily_ctx.get("daily_ma30", 0.0),
            "daily_ma60": daily_ctx.get("daily_ma60", 0.0),
            "daily_support_name": daily_ctx.get("daily_support_name", ""),
            "daily_support_level": daily_ctx.get("daily_support_level", 0.0),
            "daily_support_gap": daily_ctx.get("daily_support_gap", 0.0),
            "daily_breakdown_risk": daily_ctx.get("daily_breakdown_risk", False),
            "daily_hard_breakdown": daily_ctx.get("daily_hard_breakdown", False),
            "daily_overheated": daily_ctx.get("daily_overheated", False),
            "profit_guard_active": False,
        }

        is_diving = (mom5 < -0.015) and (vol_ratio > 2.0)
        if is_diving and not self._in_cooldown(code, "PANIC_SELL"):
            details = [{"指标": "巨量跳水", "当前": f"5分钟跌{mom5*100:.2f}%", "阈值": "<-1.5%", "解读": "资金突发性踩踏出逃", "加分": 100}]
            return 0, 100, Signal(code, name, "PANIC_SELL", price, 100, ["资金踩踏出逃"], details, indicators)

        sell_details, buy_details = [], []
        # V1.11: 早盘冲高窗口优化 - 09:30-09:35为机会窗口(+8)，09:36-09:45为观察期(0)
        if 1000 <= t_val <= 1045 or 1400 <= t_val <= 1445:
            time_score = 15
        elif 930 <= t_val <= 935:
            time_score = 8
        elif 936 <= t_val <= 945:
            time_score = 0
        else:
            time_score = 0
        sell_score = buy_score = time_score
        required_profit_buy = PARAMS["min_profit_space"] * 1.5 if rsi < 15 else PARAMS["min_profit_space"]
        buy_profit_space = (vwap - price) / price if price > 0 else 0.0
        if buy_profit_space > 0:
            buy_score += 8
            buy_details.append({"指标": "回归空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "现价低于均价", "加分": 8})
        if buy_profit_space > required_profit_buy:
            buy_score += 12
            buy_details.append({"指标": "盈利空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "距离均价回归空间足", "加分": 12})
        # V1.11: 下午回落接回因子 - 13:00-14:30回落到VWAP下方且RSI超卖
        had_afternoon_pullback = False
        if 1300 <= t_val <= 1430 and buy_profit_space > 0.005 and rsi <= PARAMS["rsi_oversold"]:
            buy_score += 8
            buy_details.append({"指标": "下午回落", "当前": f"RSI{rsi:.1f}/低于VWAP{buy_profit_space*100:.2f}%", "解读": "下午回落超卖，建议接回", "加分": 8})
            had_afternoon_pullback = True
        if rsi <= PARAMS["rsi_oversold"]:
            buy_score += 12
            buy_details.append({"指标": "RSI超卖", "当前": f"{rsi:.1f}", "阈值": f"≤{PARAMS['rsi_oversold']}", "加分": 12})
        if bb_pct <= 0.15:
            buy_score += 8
            buy_details.append({"指标": "布林偏下", "当前": f"{bb_pct:.2f}", "阈值": "≤0.15", "加分": 8})
        if buy_profit_space > 0 and rsi <= PARAMS["rsi_oversold"] and mom5 < 0:
            buy_score -= 4
            buy_details.append({"指标": "回落未确认", "当前": f"{mom5*100:.2f}%", "阈值": "5分钟仍未转正", "加分": -4})
        if buy_profit_space > 0 and range_pos > 0.35:
            buy_score -= 2
            buy_details.append({"指标": "低位不够深", "当前": f"{range_pos:.2f}", "阈值": "≤0.35", "加分": -2})
        if macd_hist > prev_macd_hist and macd_hist < 0:
            buy_score += 15
            buy_details.append({"指标": "MACD拐头", "当前": f"{macd_hist:.4f}", "阈值": "负区抬头", "加分": 15})
            if abs(macd_hist) > PARAMS["macd_strong_threshold"]:
                buy_score += PARAMS["macd_strong_boost"]
                buy_details.append({"指标": "MACD强拐头", "当前": f"{macd_hist:.4f}", "阈值": f">{PARAMS['macd_strong_threshold']}", "加分": PARAMS["macd_strong_boost"]})
        if vol_ratio >= PARAMS["vol_ratio_confirm"]:
            buy_score += PARAMS["vol_confirm_boost"]
            buy_details.append({"指标": "量能确认", "当前": f"{vol_ratio:.2f}", "阈值": f"≥{PARAMS['vol_ratio_confirm']}", "加分": PARAMS["vol_confirm_boost"]})
        if lower_shadow >= 0.5:
            buy_score += 8
            buy_details.append({"指标": "长下影", "当前": f"{lower_shadow:.2f}", "阈值": "≥0.5", "加分": 8})
        if ema_spread > prev_ema_spread and ema_spread > -0.002:
            buy_score += 4
            buy_details.append({"指标": "EMA转强", "当前": f"{ema_spread*100:.2f}%", "阈值": "短均线改善", "加分": 4})
        if buy_score < PARAMS["buy_confirm_min_score"] and len(buy_details) >= PARAMS["buy_confirm_min_factors"]:
            buy_score -= 2
            buy_details.append({"指标": "买点未成型", "当前": f"{buy_score:.0f}", "阈值": f"≥{PARAMS['buy_confirm_min_score']}且确认因子不足", "加分": -2})
        if buy_score >= PARAMS["buy_confirm_min_score"] and mom5 <= 0 and price < vwap and range_pos <= 0.45:
            buy_score += 6
            buy_details.append({"指标": "回落确认", "当前": f"{mom5*100:.2f}%", "阈值": "贴近VWAP且5分钟不再走弱", "加分": 6})
        elif buy_score >= PARAMS["buy_confirm_min_score"] and mom5 > 0 and price < vwap:
            buy_score -= 4
            buy_details.append({"指标": "反弹过快", "当前": f"{mom5*100:.2f}%", "阈值": "仍需低位回落确认", "加分": -4})
        if buy_score >= PARAMS["buy_confirm_min_score"] and price > vwap and mom5 > 0:
            buy_score -= 3
            buy_details.append({"指标": "买点过热", "当前": f"{price:.2f}", "阈值": "确认买点不应强行追高", "加分": -3})
        if range_pos <= PARAMS["range_pos_low_threshold"] and mom5 > -0.01:
            buy_score += 4
            buy_details.append({"指标": "区间低位", "当前": f"{range_pos:.2f}", "阈值": f"≤{PARAMS['range_pos_low_threshold']}", "加分": 4})
        if daily_pullback_support and price <= vwap and mom5 > -0.004:
            buy_score += 8
            buy_details.append({"指标": "日线回踩承接", "当前": f"{price:.2f}/{vwap:.2f}", "阈值": "回踩支撑后止跌", "加分": 8})
        elif daily_near_support and price <= vwap and mom5 > -0.002:
            buy_score += 4
            buy_details.append({"指标": "日线支撑企稳", "当前": f"{price:.2f}/{vwap:.2f}", "阈值": "支撑附近不再走弱", "加分": 4})
        if is_strong_pullback:
            buy_score += 30
            buy_details.append({"指标": "主升浪回踩", "当前": "贴近VWAP", "解读": "强势突破股回踩均价", "加分": 30})
        elif is_strong_trend and price >= prev_high and vol_ratio >= PARAMS["vol_ratio_confirm"] and benchmark_gate != "weak":
            buy_score += 20
            buy_details.append({"指标": "强势突破", "当前": f"{price:.2f}", "解读": "突破前高并放量，提示顺势加仓", "加分": 20})

        required_profit_sell = PARAMS["min_profit_space"] * 1.5 if rsi > 85 else PARAMS["min_profit_space"]
        sell_profit_space = (price - vwap) / vwap if vwap else 0.0
        if sell_profit_space > 0:
            sell_score += 15
            sell_details.append({"指标": "回吐空间", "当前": f"+{sell_profit_space*100:.2f}%", "解读": "现价高于均价", "加分": 15})
        # V1.11: 早盘冲高因子 - 开盘后5分钟内快速拉升，优先高抛
        had_morning_surge = False
        if 930 <= t_val <= 935 and today_ret > 0.006 and price > vwap * 1.005:
            surge_strength = min(18, int(today_ret * 1000))
            sell_score += surge_strength
            sell_details.append({"指标": "早盘冲高", "当前": f"+{today_ret*100:.2f}%", "解读": "开盘后急速拉升，建议高抛做T", "加分": surge_strength})
            had_morning_surge = True
        if sell_profit_space > required_profit_sell:
            sell_score += 15
            sell_details.append({"指标": "盈利空间", "当前": f"+{sell_profit_space*100:.2f}%", "解读": "覆盖手续费并获利", "加分": 15})
        if not is_strong_trend:
            if rsi >= PARAMS["rsi_overbought"]:
                sell_score += 15
                sell_details.append({"指标": "RSI超买", "当前": f"{rsi:.1f}", "阈值": f"≥{PARAMS['rsi_overbought']}", "加分": 15})
            if bb_pct >= 0.85:
                sell_score += 12
                sell_details.append({"指标": "布林偏上", "当前": f"{bb_pct:.2f}", "阈值": "≥0.85", "加分": 12})
        if macd_hist < prev_macd_hist and macd_hist > 0:
            sell_score += 10
            sell_details.append({"指标": "MACD拐头", "当前": f"{macd_hist:.4f}", "阈值": "正区走弱", "加分": 10})
        if vol_ratio >= PARAMS["vol_ratio_confirm"]:
            sell_score += PARAMS["vol_confirm_boost"]
            sell_details.append({"指标": "量能确认", "当前": f"{vol_ratio:.2f}", "阈值": f"≥{PARAMS['vol_ratio_confirm']}", "加分": PARAMS["vol_confirm_boost"]})
        if upper_shadow >= 0.5:
            sell_score += 15
            sell_details.append({"指标": "长上影", "当前": f"{upper_shadow:.2f}", "阈值": "≥0.5", "加分": 15})
        if ema_spread < prev_ema_spread and ema_spread < 0.002:
            sell_score += 4
            sell_details.append({"指标": "EMA转弱", "当前": f"{ema_spread*100:.2f}%", "阈值": "短均线走弱", "加分": 4})
        if range_pos >= PARAMS["range_pos_high_threshold"] and mom5 < 0.01:
            sell_score += 4
            sell_details.append({"指标": "区间高位", "当前": f"{range_pos:.2f}", "阈值": f"≥{PARAMS['range_pos_high_threshold']}", "加分": 4})

        holding_start = self.t_cycle_start_time.get(code)
        holding_minutes = (_now() - holding_start).total_seconds() / 60 if holding_start else 0.0
        if holding_minutes >= PARAMS["sell_holding_min_minutes"]:
            bonus = PARAMS["sell_score_boost_holding"] if holding_minutes < PARAMS["sell_holding_strict_minutes"] else PARAMS["sell_score_boost_holding"] + 2
            sell_score += bonus
            sell_details.append({"指标": "持仓时间", "当前": f"{holding_minutes:.0f}分钟", "阈值": f"≥{PARAMS['sell_holding_min_minutes']}分钟", "加分": bonus})
            if holding_minutes < PARAMS["sell_holding_strict_minutes"] and sell_score - buy_score < 8:
                sell_score -= 4
                sell_details.append({"指标": "时间未成熟", "当前": f"{holding_minutes:.0f}分钟", "阈值": f"≥{PARAMS['sell_holding_strict_minutes']}分钟或卖优更强", "加分": -4})
        if 1455 <= t_val <= 1500 and sell_score >= 50 and sell_score - buy_score >= 8:
            sell_score += PARAMS["sell_score_boost_eod"]
            sell_details.append({"指标": "收盘前", "当前": f"{t_val}", "阈值": "14:55-15:00 且卖分足够", "加分": PARAMS["sell_score_boost_eod"]})
        if holding_minutes >= PARAMS["sell_holding_strict_minutes"] and sell_score - buy_score >= 6:
            sell_score += PARAMS["sell_momentum_bonus"]
            sell_details.append({"指标": "持仓转弱", "当前": f"{holding_minutes:.0f}分钟", "阈值": f"≥{PARAMS['sell_holding_strict_minutes']}分钟且卖优于买", "加分": PARAMS["sell_momentum_bonus"]})

        sig = None
        is_dead_water = (day_amplitude < PARAMS["min_amplitude"] and t_val > 1000)
        buy_threshold = self._dynamic_threshold("buy", price, vwap, rsi, vol_ratio, holding, market_state, is_strong_pullback, code)
        sell_threshold = self._dynamic_threshold("sell", price, vwap, rsi, vol_ratio, holding, market_state, is_strong_pullback, code)
        # V1.11: 在阈值计算后记录早盘冲高/下午回落日志
        if _log_enhancer:
            if had_morning_surge:
                _log_enhancer.log_morning_surge(
                    code=code, name=name, stage="detected" if sell_score < sell_threshold else "triggered",
                    price=price, vwap=vwap, today_ret=today_ret,
                    sell_score=sell_score, sell_threshold=sell_threshold,
                    factors=["早盘冲高"], is_triggered=sell_score >= sell_threshold
                )
            if had_afternoon_pullback:
                _log_enhancer.log_afternoon_pullback(
                    code=code, name=name, stage="detected" if buy_score < buy_threshold else "triggered",
                    price=price, vwap=vwap, rsi=rsi,
                    buy_score=buy_score, buy_threshold=buy_threshold, factors=["下午回落"]
                )
        diag = self.diagnostics.setdefault(code, {})
        diag["buy_block_reasons"] = []
        diag["sell_block_reasons"] = []
        diag["priority_path"] = "hold"
        diag["preempted_by_sell_fast_path"] = False
        diag["buy_candidate"] = False
        diag["sell_candidate"] = False

        buy_factor_map = {d["指标"]: d for d in buy_details}
        sell_factor_map = {d["指标"]: d for d in sell_details}
        hold_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
        buy_today_count = self.buy_count_per_stock.get(code, 0)
        sell_today_count = self.sell_count_per_stock.get(code, 0)
        can_buy_more = buy_today_count < PARAMS["max_buy_times_per_stock"]
        can_sell_today = sell_today_count < PARAMS["max_sell_times_per_stock"]
        can_sell = hold_qty > 0 and can_sell_today
        buy_limit_reason = ""
        if buy_today_count >= PARAMS["max_buy_times_per_stock"]:
            buy_limit_reason = f"已达当日买入上限{PARAMS['max_buy_times_per_stock']}次"
        sell_limit_reason = "" if can_sell_today else f"已达当日卖出上限{PARAMS['max_sell_times_per_stock']}次"
        net_qty = self._virtual_net_qty(code, holding)
        last_state = self.last_signal_state.get(code, {})
        base_memory = _strategy_memory_for_code(code)

        if benchmark_gate == "weak":
            buy_threshold += 8
            if not is_strong_pullback:
                buy_threshold += 6
            sell_threshold += 1
        elif benchmark_gate == "strong":
            buy_threshold -= 1
            if not is_strong_pullback:
                sell_threshold += 3

        daily_status = daily_ctx.get("daily_status", "unknown")
        daily_gate = daily_ctx.get("daily_gate", "neutral")
        daily_trend_bg = daily_ctx.get("daily_trend_bg", "unknown")
        daily_support_gap = float(daily_ctx.get("daily_support_gap", 0.0) or 0.0)
        daily_breakdown_risk = bool(daily_ctx.get("daily_breakdown_risk", False))
        daily_hard_breakdown = bool(daily_ctx.get("daily_hard_breakdown", False))
        daily_overheated = bool(daily_ctx.get("daily_overheated", False))
        daily_pullback_support = bool(daily_ctx.get("daily_pullback_support", False))
        daily_near_support = bool(daily_ctx.get("daily_near_support", False))
        preopen_context = PREOPEN_CONTEXT if isinstance(PREOPEN_CONTEXT, PreOpenContext) else None
        auction_profile = {}
        if preopen_context and isinstance(preopen_context.code_snapshots, dict):
            auction_profile = preopen_context.code_snapshots.get(code, {}) or {}
        auction_tag = str(auction_profile.get("auction_tag", "") or "")
        auction_score = float(auction_profile.get("auction_score", 0.0) or 0.0)
        auction_quality = float(auction_profile.get("data_quality", 0.0) or 0.0)
        auction_open_gap = float(auction_profile.get("open_gap", 0.0) or 0.0)

        if auction_profile:
            if auction_tag == "strong_open" and auction_quality >= 0.6:
                buy_threshold = max(35, buy_threshold - 2)
                buy_score += 2
                buy_details.append({"指标": "盘前竞价", "当前": f"{auction_score:.1f}/{auction_open_gap*100:+.2f}%", "阈值": "强竞价", "加分": 2})
                if daily_gate in {"supportive", "neutral"} and daily_trend_bg in {"bull", "uptrend", "base"}:
                    sell_threshold = max(35, sell_threshold - 1)
            elif auction_tag == "weak_open" and auction_quality >= 0.6:
                buy_threshold += 2
                buy_score -= 2
                buy_details.append({"指标": "盘前竞价", "当前": f"{auction_score:.1f}/{auction_open_gap*100:+.2f}%", "阈值": "弱竞价", "加分": -2})
                if daily_gate in {"risk", "overheat"} or daily_hard_breakdown:
                    sell_threshold += 1
                    sell_score += 1
            elif auction_tag == "stale_or_missing":
                buy_threshold += 1
                buy_details.append({"指标": "盘前竞价", "当前": "缺失/过期", "阈值": "低质量", "加分": -1})

        # ── 护利门控：低开 + 微盈早盘，大幅提高 T 买门槛 ──────────────────
        cost = float(holding.get("cost", 0) or 0)
        profit_guard_active = False
        profit_guard_reason = ""
        if cost > 0 and price > 0 and auction_open_gap < -PARAMS["profit_guard_open_gap_threshold"]:
            float_profit_pct = (price - cost) / cost
            in_thin_profit = PARAMS["profit_guard_profit_min"] <= float_profit_pct < PARAMS["profit_guard_profit_max"]
            in_early_window = current_minute < PARAMS["profit_guard_minutes_end"]
            if in_thin_profit and in_early_window:
                profit_guard_active = True
                buy_threshold += PARAMS["profit_guard_buy_threshold_add"]
                buy_score -= PARAMS["profit_guard_buy_score_penalty"]
                profit_guard_reason = (
                    f"护利拦截：低开{auction_open_gap*100:.2f}%/浮盈{float_profit_pct*100:+.2f}%"
                )
                buy_details.append({
                    "指标": "护利门控",
                    "当前": f"低开{auction_open_gap*100:.2f}%/浮盈{float_profit_pct*100:+.2f}%",
                    "阈值": f"<={PARAMS['profit_guard_open_gap_threshold']*100:.1f}%低开+微盈区间",
                    "加分": -PARAMS["profit_guard_buy_score_penalty"],
                })

        if daily_status == "ok":
            if daily_gate == "risk" or daily_hard_breakdown:
                buy_threshold += PARAMS["daily_risk_buy_threshold_penalty"]
                sell_threshold = max(35, sell_threshold - 1)
            elif daily_gate == "overheat":
                buy_threshold += PARAMS["daily_overheat_buy_threshold_penalty"]
                if not is_strong_pullback:
                    buy_threshold += 2
                sell_threshold += 2
            elif daily_gate == "supportive" and range_pos <= 0.45:
                buy_threshold = max(35, buy_threshold - PARAMS["daily_support_buy_threshold_relief"])
            elif daily_gate == "caution":
                buy_threshold += 2

            if daily_trend_bg in {"bull", "uptrend"}:
                buy_score += PARAMS["daily_trend_buy_boost"]
                buy_details.append({"指标": "日线趋势背景", "当前": daily_trend_bg, "阈值": "多头/上行", "加分": PARAMS["daily_trend_buy_boost"]})
            elif daily_trend_bg == "base":
                buy_score += PARAMS["daily_base_buy_boost"]
                buy_details.append({"指标": "日线底座", "当前": "均线粘合", "阈值": "底部整理", "加分": PARAMS["daily_base_buy_boost"]})
            elif daily_trend_bg in {"downtrend", "weak_breakdown"}:
                buy_score -= PARAMS["daily_downtrend_buy_penalty"]
                buy_details.append({"指标": "日线偏弱", "当前": daily_trend_bg, "阈值": "日线走弱", "加分": -PARAMS["daily_downtrend_buy_penalty"]})

            if daily_pullback_support or (daily_near_support and range_pos <= 0.55 and price <= vwap):
                buy_score += PARAMS["daily_support_buy_boost"]
                buy_details.append({"指标": "日线回踩支撑", "当前": f"{daily_ctx.get('daily_support_name', '')}@{daily_support_gap*100:.2f}%", "阈值": "MA20/30/60附近", "加分": PARAMS["daily_support_buy_boost"]})
            elif daily_near_support:
                buy_score += PARAMS["daily_base_buy_boost"]
                buy_details.append({"指标": "日线靠近支撑", "当前": f"{daily_ctx.get('daily_support_name', '')}@{daily_support_gap*100:.2f}%", "阈值": "支撑附近", "加分": PARAMS["daily_base_buy_boost"]})

            if daily_breakdown_risk:
                buy_score -= PARAMS["daily_breakdown_buy_penalty"]
                buy_details.append({"指标": "日线破位风险", "当前": daily_trend_bg, "阈值": "跌破关键均线", "加分": -PARAMS["daily_breakdown_buy_penalty"]})
                sell_score += PARAMS["daily_breakdown_sell_boost"]
                sell_details.append({"指标": "日线破位风险", "当前": daily_trend_bg, "阈值": "跌破关键均线", "加分": PARAMS["daily_breakdown_sell_boost"]})
            if daily_hard_breakdown:
                buy_score -= PARAMS["daily_breakdown_buy_penalty"]
                sell_score += PARAMS["daily_breakdown_sell_boost"] + 3
                sell_details.append({"指标": "日线硬破位", "当前": daily_trend_bg, "阈值": "MA60下方", "加分": PARAMS["daily_breakdown_sell_boost"] + 3})
            if daily_overheated:
                buy_score -= PARAMS["daily_overheat_buy_penalty"]
                buy_details.append({"指标": "日线过热", "当前": daily_trend_bg, "阈值": "远离MA10/20", "加分": -PARAMS["daily_overheat_buy_penalty"]})
                sell_score += PARAMS["daily_overheat_sell_boost"]
                sell_details.append({"指标": "日线过热", "当前": daily_trend_bg, "阈值": "远离MA10/20", "加分": PARAMS["daily_overheat_sell_boost"]})

        if market_state == "trend_down":
            buy_threshold += PARAMS["market_state_threshold_bias"] + 4
            sell_threshold = max(38, sell_threshold - 1)
        elif market_state == "trend_up":
            if not is_strong_pullback:
                buy_threshold += 3
            sell_threshold += 3

        buy_threshold, sell_threshold, buy_score, sell_score = _special_loss_threshold_adjustments(
            code,
            "BUY_LOW" if buy_score >= buy_threshold else ("SELL_HIGH" if sell_score >= sell_threshold else "HOLD"),
            buy_threshold,
            sell_threshold,
            buy_score,
            sell_score,
            price,
            vwap,
            is_strong_pullback,
        )

        cycle_count = self.cycle_count.get(code, 0)
        if cycle_count >= PARAMS["max_t_cycles_per_stock"]:
            buy_threshold += 100
            sell_threshold += 100
        elif cycle_count == 1:
            buy_threshold += 8
            sell_threshold += 8

        if buy_today_count >= PARAMS["max_buy_times_per_stock"]:
            buy_threshold += 100
        if sell_today_count >= PARAMS["max_sell_times_per_stock"]:
            sell_threshold += 100

        if sell_today_count == 0 and buy_today_count == 0:
            buy_threshold += 2
            sell_threshold += 2

        if hold_qty <= 0:
            sell_score = -999
        if net_qty <= 0:
            sell_score = -999
        last_trade = self.last_trade_state.get(code, {})
        post_sell_block_until = self.post_sell_block_until.get(code)
        post_sell_block_active = bool(post_sell_block_until and _now() < post_sell_block_until)
        post_sell_block_remaining = (post_sell_block_until - _now()).total_seconds() if post_sell_block_active else 0.0
        post_sell_rebuild_allowed = False
        post_sell_rebuild_reason = ""
        if post_sell_block_active and hold_qty > 0:
            price_rebuild_ok = bool(vwap) and price <= vwap * (1 + PARAMS["post_sell_rebuild_price_gap"])
            score_rebuild_ok = (buy_score - sell_score) >= max(0, PARAMS["post_sell_rebuild_score_gap"] - PARAMS["post_sell_rebuild_relax_gap"] - 4)
            post_sell_elapsed = PARAMS["post_sell_rebuild_minutes"] * 60 - post_sell_block_remaining
            time_rebuild_ok = post_sell_elapsed >= PARAMS["post_sell_rebuild_min_seconds"]
            post_sell_rebuild_allowed = price_rebuild_ok and score_rebuild_ok and time_rebuild_ok
            parts = []
            if price_rebuild_ok:
                parts.append("price")
            if score_rebuild_ok:
                parts.append("score")
            if time_rebuild_ok:
                parts.append("time")
            post_sell_rebuild_reason = ",".join(parts) if parts else "blocked"
            if not post_sell_rebuild_allowed:
                buy_threshold += PARAMS["post_sell_rebuild_buy_threshold_penalty"]
                if benchmark_gate == "weak":
                    buy_threshold += int(round(3 * PARAMS["post_sell_rebuild_weak_gate_discount"]))
                sell_threshold += 20
        if hold_qty > 0 and can_buy_more and last_state.get("action") in ["BUY_LOW", "ADD_POS"]:
            buy_threshold += 6
        if hold_qty > 0 and self._is_redundant_signal(code, "SELL_HIGH", price, sell_score):
            sell_threshold += 120
            sell_score -= 8
        if self._is_redundant_signal(code, "BUY_LOW", price, buy_score):
            buy_threshold += 100
        if self._is_redundant_signal(code, "ADD_POS", price, buy_score):
            buy_threshold += 100
        if last_state.get("action") in ["SELL_HIGH", "PANIC_SELL"] and hold_qty > 0:
            buy_threshold += 8
        if last_state.get("action") in ["BUY_LOW", "ADD_POS"] and hold_qty > 0:
            sell_threshold += 8
        if last_trade.get("action") in ["BUY_LOW", "ADD_POS"] and holding_minutes < 12:
            sell_threshold += 10
        if last_trade.get("action") in ["SELL_HIGH", "PANIC_SELL"] and holding_minutes < 12:
            buy_threshold += 10

        stand_down, stand_down_reason = self._should_stand_down(code, holding, df, buy_score, sell_score, market_state, can_sell)
        if stand_down:
            buy_threshold += 100
            sell_threshold += 100
            diag["buy_block_reasons"].append(stand_down_reason)
            diag["sell_block_reasons"].append(stand_down_reason)
            sig = None

        if post_sell_block_active:
            dec = DAILY_DECISION_STATS.get(code)
            if dec is not None:
                dec["last_stand_down_reason"] = f"卖后重建{PARAMS['post_sell_rebuild_minutes']}分钟"

        if code in SIGNAL_OUTCOME_TRACKER:
            traces = SIGNAL_OUTCOME_TRACKER[code]
            now_ts = _now()
            for item in traces:
                elapsed_min = (now_ts - item["signal_time"]).total_seconds() / 60
                item["price_points"].append({
                    "ts": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": price,
                    "vwap": vwap,
                    "elapsed_min": round(elapsed_min, 2),
                })
                if elapsed_min >= 5 and not item["maturity_5m"]:
                    item["price_after_5m"] = price
                    item["vwap_after_5m"] = vwap
                    item["maturity_5m"] = True
                if elapsed_min >= 15 and not item["maturity_15m"]:
                    item["price_after_15m"] = price
                    item["vwap_after_15m"] = vwap
                    item["maturity_15m"] = True
                item["last_seen_price"] = price
                item["last_seen_vwap"] = vwap
                item["last_seen_time"] = now_ts.strftime("%Y-%m-%d %H:%M:%S")

        if not is_dead_water and not stand_down:
            buy_fast_path_gap = max(PARAMS["sell_fast_path_min_gap"], 18)
            buy_fast_path_protected = buy_score >= buy_threshold + PARAMS["buy_priority_margin"] and (buy_score - sell_score) >= PARAMS["buy_priority_margin"]
            if can_sell and sell_score >= sell_threshold and (sell_score - buy_score) >= buy_fast_path_gap and not self._in_cooldown(code, "SELL_HIGH"):
                if buy_fast_path_protected:
                    diag["preempted_by_sell_fast_path"] = True
                    diag["buy_block_reasons"].append("buy_priority_protection")
                else:
                    reasons = [d["指标"] for d in sell_details if d.get("加分", 0) > 0]
                    sell_details.append({"指标": "触发阈值", "当前": f"{sell_score:.0f}", "阈值": f">={sell_threshold}", "加分": 0})
                    diag["sell_candidate"] = True
                    diag["priority_path"] = "sell_observe_path"
                    sig = Signal(code, name, "SELL_HIGH", price, sell_score, reasons, sell_details, indicators, {
                    "side": "sell",
                    "threshold": sell_threshold,
                    "time_score": time_score,
                    "buy_score": buy_score,
                    "sell_score": sell_score,
                    "buy_factors": buy_factor_map,
                    "sell_factors": sell_factor_map,
                    "is_dead_water": is_dead_water,
                    "is_strong_trend": is_strong_trend,
                    "today_ret": today_ret,
                    "day_amplitude": day_amplitude,
                    "market_state": market_state,
                    "benchmark_state": benchmark_state,
                    "benchmark_gate": benchmark_gate,
                    "benchmark_reason": benchmark_reason,
                    "buy_limit_reason": buy_limit_reason,
                    "hold_qty": hold_qty,
                    "net_qty": net_qty,
                    "cycle_count": cycle_count,
                    "entry_kind": "sell_high",
                    "sell_stage": "observe",
                }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
            elif can_buy_more and buy_score >= buy_threshold and not self._in_cooldown(code, "BUY_LOW") and (not post_sell_block_active or post_sell_rebuild_allowed):
                diag["buy_candidate"] = True
                reasons = [d["指标"] for d in buy_details if d.get("加分", 0) > 0]
                if benchmark_gate == "strong" and is_strong_trend and price >= prev_high and vol_ratio >= PARAMS["vol_ratio_confirm"]:
                    reasons = list(dict.fromkeys(reasons + ["强势突破"]))
                buy_time_ready = current_minute >= min_trade_minute
                buy_confirm_ts = self.last_signal_state.get(code, {}).get("ts")
                buy_confirm_elapsed = (_now() - buy_confirm_ts).total_seconds() if isinstance(buy_confirm_ts, datetime) else 9999
                buy_confirm_floor = buy_confirm_min_seconds if not post_sell_rebuild_allowed else max(20, buy_confirm_min_seconds - 30)
                buy_confirm_ready = buy_confirm_elapsed >= max(buy_confirm_floor, 90 if not post_sell_rebuild_allowed else 45)
                buy_momentum_ok = (mom5 > 0 or is_strong_pullback or market_state == "trend_up") if buy_needs_momentum else True
                buy_ema_ok = (ema_spread > prev_ema_spread or ema_spread > 0.0001 or daily_pullback_support) if buy_needs_ema else True
                buy_volume_ok = (vol_ratio >= PARAMS["vol_ratio_confirm"] - 0.2) if buy_needs_volume else True
                buy_gap_floor = buy_rebound_min_score_gap + 1
                if post_sell_rebuild_allowed:
                    buy_gap_floor = max(0, buy_gap_floor - PARAMS["post_sell_rebuild_relax_gap"] - 2)
                buy_gap_ok = (buy_score - sell_score) >= buy_gap_floor
                buy_detail_need = buy_confirm_min_factors + 1
                if post_sell_rebuild_allowed:
                    buy_detail_need = max(1, buy_detail_need - PARAMS["post_sell_rebuild_relax_factors"] - 1)
                buy_detail_count_ok = len(buy_details) >= buy_detail_need
                buy_price_ok = (price <= vwap and mom5 <= 0) or is_strong_pullback or market_state == "trend_up"
                sell_time_ready = current_minute >= min_trade_minute
                sell_confirm_ts = self.last_signal_state.get(code, {}).get("ts")
                sell_confirm_elapsed = (_now() - sell_confirm_ts).total_seconds() if isinstance(sell_confirm_ts, datetime) else 9999
                sell_confirm_ready = sell_confirm_elapsed >= max(sell_confirm_min_seconds, 75)
                sell_detail_count_ok = len(sell_details) >= sell_confirm_min_factors + 2
                sell_momentum_ok = (mom5 < -0.006 or market_state == "trend_down") if sell_needs_momentum else True
                sell_ema_ok = ((ema_spread < prev_ema_spread and ema_spread < -0.001) or (ema_spread < -0.002)) if sell_needs_ema else True
                sell_volume_ok = (vol_ratio >= PARAMS["vol_ratio_confirm"] + 0.3) if sell_needs_volume else True
                buy_support_count = _buy_soft_support_count(buy_momentum_ok, buy_ema_ok, buy_volume_ok, buy_price_ok, buy_gap_ok, buy_detail_count_ok, buy_time_ready)
                low_buy_threshold = buy_threshold - PARAMS["buy_soft_margin"]
                low_buy_threshold -= int(base_memory.get("buy_low_threshold_adj", 0) or 0)
                buy_candidate_preheat = daily_pullback_support and price <= vwap and mom5 > -0.004 and buy_support_count >= max(3, buy_detail_need - 2) and buy_score >= low_buy_threshold - 2
                if buy_candidate_preheat:
                    diag["buy_candidate"] = True
                    diag["priority_path"] = "buy_soft_path"
                    diag.setdefault("buy_block_reasons", []).append("buy_preheat")
                buy_soft_ready = buy_score >= max(low_buy_threshold, buy_threshold - 4) and buy_support_count >= max(4, buy_detail_need - 1) and (buy_confirm_ready or current_minute >= min_trade_minute)
                if buy_time_ready and buy_confirm_ready and buy_momentum_ok and buy_ema_ok and buy_volume_ok and buy_gap_ok and buy_detail_count_ok and buy_price_ok and daily_buy_t_ok:
                    diag["priority_path"] = "buy_path"
                    reasons = [d["指标"] for d in buy_details if d.get("加分", 0) > 0]
                    if post_sell_rebuild_allowed:
                        reasons = list(dict.fromkeys(reasons + ["卖后重建"]))
                    if daily_ma5_state == "above_ma5_trend":
                        reasons = list(dict.fromkeys(reasons + ["MA5上行顺势T"]))
                    act = "ADD_POS" if ("主升浪回踩" in reasons or "强势突破" in reasons) else "BUY_LOW"
                    buy_details.append({"指标": "触发阈值", "当前": f"{buy_score:.0f}", "阈值": f">={buy_threshold}", "加分": 0})
                    sig = Signal(code, name, act, price, buy_score, reasons, buy_details, indicators, {
                        "side": "buy",
                        "threshold": buy_threshold,
                        "time_score": time_score,
                        "buy_score": buy_score,
                        "sell_score": sell_score,
                        "buy_factors": buy_factor_map,
                        "sell_factors": sell_factor_map,
                        "is_dead_water": is_dead_water,
                        "is_strong_trend": is_strong_trend,
                        "today_ret": today_ret,
                        "day_amplitude": day_amplitude,
                        "market_state": market_state,
                        "benchmark_state": benchmark_state,
                        "benchmark_gate": benchmark_gate,
                        "benchmark_reason": benchmark_reason,
                        "buy_limit_reason": buy_limit_reason,
                        "hold_qty": hold_qty,
                        "net_qty": net_qty,
                        "cycle_count": cycle_count,
                        "entry_kind": "strong_breakout" if act == "ADD_POS" and "强势突破" in reasons else ("pullback" if act == "ADD_POS" else "low_buy"),
                    }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                elif can_buy_more and buy_soft_ready and not self._in_cooldown(code, "BUY_LOW") and (not post_sell_block_active or post_sell_rebuild_allowed):
                    if daily_ma5_state == "below_ma5_weak":
                        diag.setdefault("buy_block_reasons", []).append("below_daily_ma5")
                    elif daily_ma5_state == "unknown":
                        diag.setdefault("buy_block_reasons", []).append("daily_ma5_unavailable")
                    elif daily_ma5_state == "above_ma5_trend" and not (is_strong_pullback or market_state == "trend_up"):
                        diag.setdefault("buy_block_reasons", []).append("above_ma5_trend_soft_buy_blocked")
                    else:
                        diag["buy_candidate"] = True
                        diag["priority_path"] = "buy_soft_path"
                        reasons = [d["指标"] for d in buy_details if d.get("加分", 0) > 0]
                        if post_sell_rebuild_allowed:
                            reasons = list(dict.fromkeys(reasons + ["卖后重建"]))
                        if daily_ma5_state == "above_ma5_trend":
                            reasons = list(dict.fromkeys(reasons + ["MA5上行顺势T"]))
                        buy_details.append({"指标": "软确认阈值", "当前": f"{buy_score:.0f}", "阈值": f">={buy_threshold - PARAMS['buy_soft_margin']}", "加分": 0})
                        sig = Signal(code, name, "BUY_LOW", price, buy_score, reasons, buy_details, indicators, {
                            "side": "buy",
                            "threshold": buy_threshold,
                            "time_score": time_score,
                            "buy_score": buy_score,
                            "sell_score": sell_score,
                            "buy_factors": buy_factor_map,
                            "sell_factors": sell_factor_map,
                            "is_dead_water": is_dead_water,
                            "is_strong_trend": is_strong_trend,
                            "today_ret": today_ret,
                            "day_amplitude": day_amplitude,
                            "market_state": market_state,
                            "benchmark_state": benchmark_state,
                            "benchmark_gate": benchmark_gate,
                            "benchmark_reason": benchmark_reason,
                            "buy_limit_reason": buy_limit_reason,
                            "hold_qty": hold_qty,
                            "net_qty": net_qty,
                            "cycle_count": cycle_count,
                            "entry_kind": "soft_buy" if post_sell_rebuild_allowed else "low_buy",
                            "soft_buy": True,
                            "soft_support_count": buy_support_count,
                            "low_buy_threshold": low_buy_threshold,
                            "preopen_stage": "open" if current_minute < 945 else ("eod" if current_minute >= 1455 else "intraday"),
                        }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                elif can_sell and sell_score >= sell_threshold and sell_time_ready and sell_confirm_ready and sell_detail_count_ok and sell_momentum_ok and sell_ema_ok and sell_volume_ok:
                    diag["sell_candidate"] = True
                    diag["priority_path"] = "sell_confirm_path"
                    reasons = [d["指标"] for d in sell_details if d.get("加分", 0) > 0]
                    sell_details.append({"指标": "触发阈值", "当前": f"{sell_score:.0f}", "阈值": f">={sell_threshold}", "加分": 0})
                    sig = Signal(code, name, "SELL_HIGH", price, sell_score, reasons, sell_details, indicators, {
                        "side": "sell",
                        "threshold": sell_threshold,
                        "time_score": time_score,
                        "buy_score": buy_score,
                        "sell_score": sell_score,
                        "buy_factors": buy_factor_map,
                        "sell_factors": sell_factor_map,
                        "is_dead_water": is_dead_water,
                        "is_strong_trend": is_strong_trend,
                        "today_ret": today_ret,
                        "day_amplitude": day_amplitude,
                        "market_state": market_state,
                        "buy_limit_reason": buy_limit_reason,
                        "hold_qty": hold_qty,
                        "net_qty": net_qty,
                        "cycle_count": cycle_count,
                        "sell_stage": "execute",
                    }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)

        dec = DAILY_DECISION_STATS.get(code)
        if dec is not None:
            dec["last_market_state"] = market_state
            dec["last_buy_limit_reason"] = buy_limit_reason
            dec["last_stand_down_reason"] = stand_down_reason if stand_down else ""

        if sig is None:
            if diag["buy_candidate"]:
                if not buy_time_ready:
                    diag["buy_block_reasons"].append("buy_time_not_ready")
                if not buy_confirm_ready:
                    diag["buy_block_reasons"].append("buy_confirm_wait")
                if not buy_momentum_ok:
                    diag["buy_block_reasons"].append("buy_momentum_fail")
                if not buy_ema_ok:
                    diag["buy_block_reasons"].append("buy_ema_fail")
                if not buy_volume_ok:
                    diag["buy_block_reasons"].append("buy_volume_fail")
                if not buy_gap_ok:
                    diag["buy_block_reasons"].append("buy_gap_fail")
                if not buy_detail_count_ok:
                    diag["buy_block_reasons"].append("buy_detail_fail")
                if not buy_price_ok:
                    diag["buy_block_reasons"].append("buy_price_fail")
                if not can_buy_more:
                    diag["buy_block_reasons"].append("buy_limit")
                if self._in_cooldown(code, "BUY_LOW"):
                    diag["buy_block_reasons"].append("buy_cooldown")
                if post_sell_block_active and not post_sell_rebuild_allowed:
                    diag["buy_block_reasons"].append("post_sell_block")
                if 'buy_soft_ready' in locals() and buy_soft_ready:
                    diag["buy_block_reasons"].append("buy_soft_ready")
                    diag["priority_path"] = diag.get("priority_path") or "buy_soft_path"
            if diag["sell_candidate"] and sig is None:
                if not can_sell:
                    diag["sell_block_reasons"].append("sell_no_position")
                if not sell_time_ready:
                    diag["sell_block_reasons"].append("sell_time_not_ready")
                if not sell_confirm_ready:
                    diag["sell_block_reasons"].append("sell_confirm_wait")
                if not sell_detail_count_ok:
                    diag["sell_block_reasons"].append("sell_detail_fail")
                if not sell_momentum_ok:
                    diag["sell_block_reasons"].append("sell_momentum_fail")
                if not sell_ema_ok:
                    diag["sell_block_reasons"].append("sell_ema_fail")
                if not sell_volume_ok:
                    diag["sell_block_reasons"].append("sell_volume_fail")
                if self._in_cooldown(code, "SELL_HIGH"):
                    diag["sell_block_reasons"].append("sell_cooldown")
        decision = sig.action if sig else "HOLD"
        decision_reason = " + ".join(sig.reasons) if sig and sig.reasons else (stand_down_reason if stand_down else "")
        _append_jsonl(_trace_path("decision_trace"), {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
            "name": name,
            "price": price,
            "vwap": vwap,
            "rsi": rsi,
            "bb_pct": bb_pct,
            "macd_hist": macd_hist,
            "ema_spread": ema_spread,
            "range_pos": range_pos,
            "vol_ratio": vol_ratio,
            "mom5": mom5,
            "lower_shadow": lower_shadow,
            "upper_shadow": upper_shadow,
            "day_amplitude": day_amplitude,
            "today_ret": today_ret,
            "prev_high": prev_high,
            "is_strong_trend": is_strong_trend,
            "is_strong_pullback": is_strong_pullback,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "market_state": market_state,
            "benchmark_code": benchmark.get("benchmark_code", ""),
            "benchmark_state": benchmark_state,
            "benchmark_gate": benchmark_gate,
            "daily_status": daily_ctx.get("daily_status", "unknown"),
            "daily_gate": daily_ctx.get("daily_gate", "neutral"),
            "daily_trend_bg": daily_ctx.get("daily_trend_bg", "unknown"),
            "daily_ma5": daily_ctx.get("daily_ma5", 0.0),
            "daily_ma5_slope": daily_ctx.get("daily_ma5_slope", 0.0),
            "daily_above_ma5": daily_ctx.get("daily_above_ma5", False),
            "daily_ma5_gap": daily_ctx.get("daily_ma5_gap", 0.0),
            "daily_ma5_state": daily_ctx.get("daily_ma5_state", "unknown"),
            "daily_buy_t_ok": daily_buy_t_ok,
            "daily_sell_t_preferred": daily_sell_t_preferred,
            "daily_buy_t_relaxed": daily_buy_t_relaxed,
            "daily_support_name": daily_ctx.get("daily_support_name", ""),
            "daily_support_gap": daily_ctx.get("daily_support_gap", 0.0),
            "daily_breakdown_risk": daily_ctx.get("daily_breakdown_risk", False),
            "daily_overheated": daily_ctx.get("daily_overheated", False),
            "daily_ma10": daily_ctx.get("daily_ma10", 0.0),
            "daily_ma20": daily_ctx.get("daily_ma20", 0.0),
            "daily_ma30": daily_ctx.get("daily_ma30", 0.0),
            "daily_ma60": daily_ctx.get("daily_ma60", 0.0),
            "hold_qty": hold_qty,
            "net_qty": net_qty,
            "cycle_count": cycle_count,
            "minute_status": MINUTE_FETCH_STATUS.get(code, "unknown"),
            "minute_detail": MINUTE_FETCH_DETAIL.get(code, ""),
            "decision": decision,
            "decision_reason": decision_reason,
            "buy_factors": buy_factor_map,
            "sell_factors": sell_factor_map,
            "buy_detail_count": len(buy_details),
            "sell_detail_count": len(sell_details),
            "stand_down_reason": stand_down_reason if stand_down else "",
            "cooldown_reason": "卖后重建" if post_sell_block_active else "",
            "post_sell_block": post_sell_block_active,
            "is_dead_water": is_dead_water,
            "priority_path": diag.get("priority_path", "hold"),
            "buy_candidate": diag.get("buy_candidate", False),
            "buy_candidate_preheat": bool(locals().get("buy_candidate_preheat", False)),
            "sell_candidate": diag.get("sell_candidate", False),
            "buy_block_reasons": diag.get("buy_block_reasons", []),
            "sell_block_reasons": diag.get("sell_block_reasons", []),
            "preempted_by_sell_fast_path": diag.get("preempted_by_sell_fast_path", False),
            "soft_buy_ready": bool(locals().get("buy_soft_ready", False)),
            "soft_buy_support_count": int(locals().get("buy_support_count", 0) or 0),
        })
        if sig is None and (buy_score >= buy_threshold - 4 or sell_score >= sell_threshold - 4):
            shadow_side = "buy" if buy_score >= sell_score else "sell"
            _append_jsonl(_trace_path("shadow_signals"), {
                "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code,
                "name": name,
                "buy_score": buy_score,
                "sell_score": sell_score,
                "buy_threshold": buy_threshold,
                "sell_threshold": sell_threshold,
                "current_price": price,
                "vwap": vwap,
                "distance_to_buy_threshold": buy_threshold - buy_score,
                "distance_to_sell_threshold": sell_threshold - sell_score,
                "would_trigger_new_params": False,
                "would_trigger_old_params": bool(buy_score >= (buy_threshold - 4) or sell_score >= (sell_threshold - 4)),
                "best_signal_type": shadow_side,
                "best_signal_score": max(buy_score, sell_score),
                "miss_reason": decision_reason or "接近阈值但未触发",
                "market_state": market_state,
                "benchmark_gate": benchmark_gate,
                "daily_gate": daily_ctx.get("daily_gate", "neutral"),
                "daily_trend_bg": daily_ctx.get("daily_trend_bg", "unknown"),
                "daily_status": daily_ctx.get("daily_status", "unknown"),
                "daily_ma5_state": daily_ctx.get("daily_ma5_state", "unknown"),
                "daily_buy_t_ok": daily_buy_t_ok,
                "daily_buy_t_relaxed": daily_buy_t_relaxed,
                "daily_sell_t_preferred": daily_sell_t_preferred,
                "minute_status": MINUTE_FETCH_STATUS.get(code, "unknown"),
                "buy_factor_count": len(buy_details),
                "sell_factor_count": len(sell_details),
            })
        if code in SIGNAL_OUTCOME_TRACKER:
            out_path = _result_trace_path()
            for item in SIGNAL_OUTCOME_TRACKER[code]:
                item["final_time"] = _now().strftime("%Y-%m-%d %H:%M:%S")
                item["price_now"] = price
                item["vwap_now"] = vwap
                item["price_after_5m"] = item.get("price_after_5m", price)
                item["price_after_15m"] = item.get("price_after_15m", price)
                signal_price = float(item.get("signal_price", 0) or 0)
                price_now = float(item.get("price_now", price) or price)
                vwap_now = float(item.get("vwap_now", vwap) or vwap)
                price_5m = float(item.get("price_after_5m", price_now) or price_now)
                price_15m = float(item.get("price_after_15m", price_now) or price_now)
                item["ret_5m_pct"] = ((price_5m - signal_price) / signal_price * 100) if signal_price else 0.0
                item["ret_15m_pct"] = ((price_15m - signal_price) / signal_price * 100) if signal_price else 0.0
                item["win_5m"] = False
                item["win_15m"] = False
                if item.get("action") in {"BUY_LOW", "ADD_POS"}:
                    item["win_5m"] = price_5m >= signal_price * 1.001 if signal_price else False
                    item["win_15m"] = price_15m >= signal_price * 1.0015 if signal_price else False
                    if price_now >= signal_price * 1.003:
                        item["final_classification"] = "correct"
                    elif price_now <= signal_price * 0.997:
                        item["final_classification"] = "buy_early"
                    elif price_now > signal_price and price_now < signal_price * 1.003:
                        item["final_classification"] = "buy_validating"
                    else:
                        item["final_classification"] = "hold_pending"
                elif item.get("action") in {"SELL_HIGH", "PANIC_SELL"}:
                    item["win_5m"] = price_5m <= signal_price * 0.999 if signal_price else False
                    item["win_15m"] = price_15m <= signal_price * 0.9985 if signal_price else False
                    if price_now <= signal_price * 0.997:
                        item["final_classification"] = "correct"
                    elif price_now >= signal_price * 1.008:
                        item["final_classification"] = "sell_early"
                    elif price_now < signal_price and price_now > signal_price * 0.995:
                        item["final_classification"] = "sell_validating"
                    else:
                        item["final_classification"] = "hold_pending"
                else:
                    item["final_classification"] = "correct"
                item["maturity_5m"] = bool(item.get("maturity_5m", False))
                item["maturity_15m"] = bool(item.get("maturity_15m", False))
                item["final_vwap_gap_pct"] = ((price_now - vwap_now) / vwap_now * 100) if vwap_now else 0.0
                _append_jsonl(out_path, item)
            SIGNAL_OUTCOME_TRACKER[code] = []
        return buy_score, sell_score, sig

# ==================== 信号处理与推送 ====================
_last_push: Dict[str, Dict[str, Any]] = {}
def _signal_push_limits(action: str) -> tuple[float, float]:
    if action == "ADD_POS":
        return PARAMS["add_pos_signal_price_move"], PARAMS["add_pos_signal_score_boost"]
    if action == "SELL_HIGH":
        return PARAMS["sell_signal_price_move"], PARAMS["sell_signal_score_boost"]
    if action == "PANIC_SELL":
        return PARAMS["panic_sell_signal_price_move"], PARAMS["panic_sell_signal_score_boost"]
    return PARAMS["buy_signal_price_move"], PARAMS["buy_signal_score_boost"]


def _should_push(key: str, sig: Optional[Signal] = None) -> bool:
    now = _now()
    last = _last_push.get(key)
    if last:
        last_ts = last.get("ts")
        elapsed = (now - last_ts).total_seconds() if isinstance(last_ts, datetime) else PUSH_THROTTLE_SECONDS
        if elapsed < PUSH_THROTTLE_SECONDS:
            if sig is not None:
                last_score = float(last.get("score", 0) or 0)
                last_price = float(last.get("price", 0) or 0)
                score_gap = abs(float(sig.score or 0) - last_score)
                price_move = abs(float(sig.price or 0) - last_price) / last_price if last_price else 1.0
                price_threshold, score_threshold = _signal_push_limits(sig.action)
                if score_gap >= score_threshold or price_move >= price_threshold:
                    _last_push[key] = {"ts": now, "score": float(sig.score or 0), "price": float(sig.price or 0)}
                    return True
            log.info(f"⏭️  {key} 推送节流，{max(0, int(PUSH_THROTTLE_SECONDS - elapsed))}s 后可再发")
            return False
    _last_push[key] = {"ts": now, "score": float(getattr(sig, 'score', 0) or 0), "price": float(getattr(sig, 'price', 0) or 0)}
    return True


# ==================== 集合竞价驱动做T优化 ====================

def _get_auction_bias(code: str) -> tuple[str, float, float]:
    """获取股票的集合竞价偏向（强/弱/中性）

    Returns:
        (bias: "strong" | "weak" | "neutral", auction_score, open_gap)
    """
    if not isinstance(PREOPEN_CONTEXT, dict) or "code_snapshots" not in PREOPEN_CONTEXT:
        return "neutral", 0.0, 0.0

    auction_profile = PREOPEN_CONTEXT.get("code_snapshots", {}).get(str(code), {})
    if not auction_profile:
        return "neutral", 0.0, 0.0

    auction_tag = str(auction_profile.get("auction_tag", "") or "flat_open")
    auction_score = float(auction_profile.get("auction_score", 0.0) or 0.0)
    open_gap = float(auction_profile.get("open_gap", 0.0) or 0.0)

    if auction_tag == "strong_open":
        bias = "strong"
    elif auction_tag == "weak_open":
        bias = "weak"
    else:
        bias = "neutral"

    return bias, auction_score, open_gap


def _get_auction_bias_label(code: str) -> str:
    """获取集合竞价状态标签（用于飞书卡片显示）"""
    bias, auction_score, open_gap = _get_auction_bias(code)
    if bias == "strong":
        return f"🟢强势开盘(score={auction_score:.0f}, gap={open_gap*100:+.1f}%)"
    elif bias == "weak":
        return f"🔴弱势开盘(score={auction_score:.0f}, gap={open_gap*100:+.1f}%)"
    else:
        return f"⚪中性开盘(score={auction_score:.0f}, gap={open_gap*100:+.1f}%)"





def check_auction_driven_signal(code: str, holding: dict, df: pd.DataFrame, indicators: dict) -> Optional[dict]:
    """检测集合竞价驱动的T信号（弱势开盘时的冲高卖/低位买）

    Returns:
        dict with keys: action, price, range_pos, today_ret, reason
        or None if no signal triggered
    """
    if df.empty or len(df) < 5:
        return None

    bias, auction_score, open_gap = _get_auction_bias(code)
    # V1.11: 扩展检测范围 - 不仅弱势开盘，中性/小幅高开也需检测早盘冲高
    if bias == "strong" and open_gap > 0.015:
        return None  # 大幅高开不处理（已在高位）
    # 处理：弱势开盘、中性开盘、小幅低开、小幅高开
    is_weak = bias == "weak"
    is_neutral = bias == "neutral"
    is_small_gap = abs(open_gap) < 0.01
    if not (is_weak or is_neutral or is_small_gap):
        return None

    last = df.iloc[-1]
    price = float(last["close"])
    vwap = float(last["vwap"]) if pd.notna(last.get("vwap")) else price
    range_pos = float(last["range_pos"]) if pd.notna(last.get("range_pos")) else 0.5

    today_open = float(df[df["date"] == last["date"]].iloc[0]["open"]) if "date" in df.columns else price
    today_ret = (price - today_open) / today_open if today_open > 0 else 0.0

    # 初始化该股票的竞价状态跟踪
    code_state = _auction_alert_state.setdefault(code, {
        "alert_date": get_today_str(),
        "surge_sell_count": 0,
        "surge_sold": False,
        "low_buy_count": 0,
    })

    # 检测日期变更，重置状态
    if code_state.get("alert_date") != get_today_str():
        code_state = _auction_alert_state[code] = {
            "alert_date": get_today_str(),
            "surge_sell_count": 0,
            "surge_sold": False,
            "low_buy_count": 0,
        }

    # 【冲高卖出检测】
    surge_range_threshold = float(PARAMS.get("auction_weak_surge_range_pos", 0.72))
    surge_ret_threshold = float(PARAMS.get("auction_weak_surge_ret_threshold", 0.015))
    surge_vwap_gap = float(PARAMS.get("auction_weak_surge_vwap_gap", 0.008))
    max_sell_alerts = int(PARAMS.get("auction_weak_max_sell_alerts", 2))

    if (range_pos >= surge_range_threshold and
        price > vwap * (1 + surge_vwap_gap) and
        today_ret > surge_ret_threshold and
        code_state["surge_sell_count"] < max_sell_alerts):

        code_state["surge_sell_count"] += 1
        code_state["surge_sold"] = True

        return {
            "action": "AUCTION_SURGE_SELL",
            "price": price,
            "range_pos": range_pos,
            "today_ret": today_ret,
            "auction_score": auction_score,
            "open_gap": open_gap,
            "reason": f"⚠️ 弱势开盘(score={auction_score:.0f}, gap={open_gap*100:.1f}%)冲高至日内高位{range_pos*100:.0f}%, 建议先卖出规避风险"
        }

    # 【低位买回检测】
    low_range_threshold = float(PARAMS.get("auction_weak_low_range_pos", 0.25))
    low_ret_threshold = float(PARAMS.get("auction_weak_low_ret_threshold", -0.005))
    max_buy_alerts = int(PARAMS.get("auction_weak_max_buy_alerts", 2))

    if (code_state["surge_sold"] and
        range_pos <= low_range_threshold and
        price < vwap and
        today_ret < low_ret_threshold and
        code_state["low_buy_count"] < max_buy_alerts):

        code_state["low_buy_count"] += 1

        return {
            "action": "AUCTION_LOW_BUY",
            "price": price,
            "range_pos": range_pos,
            "today_ret": today_ret,
            "auction_score": auction_score,
            "open_gap": open_gap,
            "reason": f"🟢 弱势开盘已回到全天低位{range_pos*100:.0f}%, 价格低于VWAP, 建议低位买回补仓"
        }

    return None


def send_auction_alert(sig_dict: dict, holding: dict):
    """发送集合竞价驱动信号的飞书提醒

    Args:
        sig_dict: check_auction_driven_signal 返回的字典
        holding: 股票持仓信息
    """
    if not sig_dict or not _should_push(f"{holding['code']}-auction-{sig_dict['action']}"):
        return

    code = holding.get("name", holding.get("code", ""))
    action = sig_dict["action"]
    price = float(sig_dict.get("price", 0))
    range_pos = float(sig_dict.get("range_pos", 0.5))
    today_ret = float(sig_dict.get("today_ret", 0))
    auction_score = float(sig_dict.get("auction_score", 0))
    open_gap = float(sig_dict.get("open_gap", 0))
    reason = str(sig_dict.get("reason", ""))

    # 选择标题和颜色
    if action == "AUCTION_SURGE_SELL":
        title = "⚠️ 【加急】弱势开盘冲高预警"
        theme_color = "red"
        advice = f"📊 现价 {price:.2f} | 日内位置 {range_pos*100:.0f}% | 涨幅 {today_ret*100:.2f}%\n\n建议**立即卖出**部分仓位规避风险，等待全天低位再择机买回"
    else:  # AUCTION_LOW_BUY
        title = "🟢 【提醒】弱势开盘低位买回"
        theme_color = "green"
        advice = f"📊 现价 {price:.2f} | 日内位置 {range_pos*100:.0f}% | 跌幅 {today_ret*100:.2f}%\n\n建议**低位补仓**，但需控制手数，谨慎对待弱势格局"

    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    at_all = feishu_cfg.get("at_all_on_signal", True)
    use_strong = feishu_cfg.get("use_strong_notification", True)
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""

    card_elements = []
    if at_all:
        card_elements.append({"tag": "div", "text": {"content": at_text, "tag": "lark_md"}})

    card_elements.append({"tag": "div", "text": {"content": title, "tag": "lark_md"}})
    card_elements.append({
        "tag": "div",
        "text": {
            "content": (
                f"【{FEISHU_KEYWORD}】\n"
                f"股票：{code}\n"
                f"集合竞价：score {auction_score:.0f} | gap {open_gap*100:+.1f}%\n"
                f"\n{advice}\n"
                f"\n💡 {reason}"
            ),
            "tag": "lark_md"
        }
    })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": card_elements
        },
        "notify_type": 1
    }

    send_feishu_payload(
        payload=payload,
        success_log=f"✅ 集合竞价{action}提醒已推送: {code}",
        error_prefix=f"集合竞价{action}提醒推送",
        trigger_urgent_alarm_after_success=use_strong,
    )




def notify(sig, holding):
    """当信号触发时发送飞书通知（V1.11增强版）"""
    stage = "intraday_trial"
    if sig.code in {"688102", "601698", "588000", "601998", "600089"}:
        stage_hint = str(sig.factors.get("preopen_stage") or "intraday").strip()
        entry_kind = str(sig.factors.get("entry_kind") or "").strip()
        if stage_hint == "open":
            stage = "open_add" if entry_kind == "pullback" else "open_trial"
        elif stage_hint == "eod":
            stage = "eod_add" if entry_kind == "pullback" else "eod_trial"
        elif stage_hint == "intraday":
            stage = "intraday_add" if entry_kind == "pullback" else "intraday_trial"
    trade_qty = _default_trade_qty(holding, sig)
    if sig.code in {"688102", "601698", "588000", "601998", "600089"} and sig.action in {"BUY_LOW", "ADD_POS"}:
        special_qty = _special_low_buy_qty(sig.code, holding, sig.price, stage=stage)
        if special_qty > 0:
            trade_qty = special_qty
    
    # 科创50ETF 588000 分批加仓控制: 单次加仓限制为50%，防止一次性追高
    if sig.code == "588000" and sig.action in {"ADD_POS"} and trade_qty > 0:
        max_batch_qty = max(5000, int(trade_qty * 0.50))  # 单批最多50%，最少5000份
        trade_qty = min(trade_qty, max_batch_qty)
    
    
    loss_rule = _special_loss_reduction_rule(sig.code)
    loss_stage_rule = _special_loss_reduction_stage_rule(sig.code, stage)
    
    if sig.action == "PANIC_SELL":
        action_cn, action_tip, action_theme = "⚠️ 跳水减仓避险", f"资金踩踏，请立即卖出 {trade_qty} 股！", "🔴 红色卖出"
    elif sig.action == "ADD_POS":
        if sig.factors.get("entry_kind") == "strong_breakout":
            action_cn, action_tip, action_theme = "🚀 强势突破加仓", f"突破前高并放量，建议加仓 {trade_qty} 股顺势跟进。", "🟢 绿色买入"
        else:
            action_cn, action_tip, action_theme = "🚀 主升浪回踩加仓", f"强势突破，建议加仓 {trade_qty} 股做顺向T。", "🟢 绿色买入"
    elif sig.action == "BUY_LOW" and sig.factors.get("soft_buy"):
        action_cn, action_tip, action_theme = "🟡 软确认低吸", f"核心条件已到位，软确认通过，建议买入 {trade_qty} 股观察推进。", "🟢 绿色买入"
    elif sig.action == "SELL_HIGH":
        if sig.factors.get("sell_stage") == "execute":
            action_cn, action_tip, action_theme = "🔴 卖出执行信号", f"建议立即减仓/卖出 {trade_qty} 股 | 现持仓 {sig.factors.get('hold_qty', trade_qty)} 股 | 参考均价 {sig.indicators['vwap']:.2f}", "🔴 红色卖出"
        else:
            action_cn, action_tip, action_theme = "🟠 卖出观察信号", f"建议关注减仓 {trade_qty} 股 | 现持仓 {sig.factors.get('hold_qty', trade_qty)} 股 | 参考均价 {sig.indicators['vwap']:.2f}", "🟠 橙色观察"
    else:
        action_cn, action_tip, action_theme = "🟢 低吸信号(先买)", f"建议买入 {trade_qty} 股 | 现持仓 {sig.factors.get('hold_qty', trade_qty)} 股 | 均价高抛≥ {sig.indicators['vwap']:.2f}", "🟢 绿色买入"
    
    reason_str = " + ".join(sig.reasons) if sig.reasons else "综合指标达标"
    
    # 【防爆修复】：动态获取日期写入独立日志
    with open(os.path.join(LOG_DIR, f"t_signals_{get_today_str()}.log"), "a", encoding="utf-8") as f:
        f.write(f"[{sig.ts.strftime('%H:%M:%S')}] {action_theme} | {action_cn} | {sig.name}({sig.code}) | 现价: {sig.price:.2f} | 强度: {sig.score:.0f}\n")
        f.write(f"  └─ 核心原因: {reason_str}\n  └─ 建议操作: {action_tip}\n")
        if sig.factors:
            f.write(f"  └─ 因子分解: {json.dumps(sig.factors, ensure_ascii=False, default=str)}\n")
    
    act_key = "BUY_LOW" if sig.action in ["BUY_LOW", "ADD_POS"] else "SELL_HIGH"
    ai_stats = _ensure_ai_review_stats(sig.code, holding)
    ai_stats["触发买入次数" if act_key == "BUY_LOW" else "触发卖出次数"] += 1
    ai_stats["触发买入股数" if act_key == "BUY_LOW" else "触发卖出股数"] += trade_qty
    
    log.warning(f"\n{'='*70}\n【触发】{action_theme} {action_cn} {sig.name}({sig.code}) 得:{sig.score:.0f}分\n* 原因: {reason_str}\n* 建议: {action_tip}\n{'='*70}")
    _register_signal_outcome(sig, holding)
    
    # V1.11: 记录做T建议日志（包含预计接回/卖出价位）
    if _log_enhancer:
        vwap = float(sig.indicators.get("vwap", sig.price) or sig.price)
        today_ret = float(sig.indicators.get("today_ret", 0) or 0)
        suggested_buyback = vwap * 0.992 if sig.action == "SELL_HIGH" else 0.0
        suggested_resell = vwap * 1.008 if sig.action in ["BUY_LOW", "ADD_POS"] else 0.0
        _log_enhancer.log_t_advice(
            code=sig.code, name=sig.name, action=sig.action,
            trigger_price=sig.price, suggested_buyback=suggested_buyback,
            suggested_resell=suggested_resell, vwap=vwap, today_ret=today_ret,
            factors=sig.reasons or []
        )
    
    _append_jsonl(_trace_path("signal_outcome"), {
        "signal_time": sig.ts.strftime('%Y-%m-%d %H:%M:%S'),
        "code": sig.code,
        "name": sig.name,
        "action": sig.action,
        "signal_price": sig.price,
        "signal_score": sig.score,
        "signal_reasons": sig.reasons,
        "qty": trade_qty,
        "hold_qty": sig.factors.get("hold_qty", 0),
        "net_qty": sig.factors.get("net_qty", 0),
        "vwap_at_signal": sig.indicators.get("vwap", sig.price),
        "market_state_at_signal": sig.indicators.get("market_state", "unknown"),
        "benchmark_state_at_signal": sig.indicators.get("benchmark_state", "unknown"),
        "benchmark_gate_at_signal": sig.indicators.get("benchmark_gate", "neutral"),
        "buy_score": sig.factors.get("buy_score", 0),
        "sell_score": sig.factors.get("sell_score", 0),
        "buy_factors": sig.factors.get("buy_factors", {}),
        "sell_factors": sig.factors.get("sell_factors", {}),
        "hold_qty": sig.factors.get("hold_qty", 0),
        "net_qty": sig.factors.get("net_qty", 0),
        "cycle_count": sig.factors.get("cycle_count", 0),
        "minute_status": MINUTE_FETCH_STATUS.get(sig.code, "unknown"),
        "minute_detail": MINUTE_FETCH_DETAIL.get(sig.code, ""),
        "tracked": True,
        "priority_path": engine.diagnostics.get(sig.code, {}).get("priority_path", "hold") if isinstance(getattr(engine, "diagnostics", None), dict) else "hold",
        "buy_block_reasons": engine.diagnostics.get(sig.code, {}).get("buy_block_reasons", []) if isinstance(getattr(engine, "diagnostics", None), dict) else [],
        "sell_block_reasons": engine.diagnostics.get(sig.code, {}).get("sell_block_reasons", []) if isinstance(getattr(engine, "diagnostics", None), dict) else [],
        "preempted_by_sell_fast_path": engine.diagnostics.get(sig.code, {}).get("preempted_by_sell_fast_path", False) if isinstance(getattr(engine, "diagnostics", None), dict) else False,
    })
    
    if sig.code not in VIRTUAL_TRADES: VIRTUAL_TRADES[sig.code] = {"BUY_LOW": [], "SELL_HIGH": []}
    VIRTUAL_TRADES[sig.code][act_key].append({"price": sig.price, "qty": trade_qty, "hold_qty": sig.factors.get("hold_qty", 0), "net_qty": sig.factors.get("net_qty", 0)})
    
    dec = _ensure_daily_decision_stats(sig.code, holding)
    dec["last_price"] = sig.price
    dec["last_vwap"] = sig.indicators.get("vwap", sig.price)
    dec["last_score"] = sig.score
    signal_rec = {"time": sig.ts.strftime('%H:%M:%S'), "action": sig.action, "price": sig.price, "score": sig.score, "reasons": reason_str, "vwap": sig.indicators.get("vwap", sig.price), "qty": trade_qty, "hold_qty": sig.factors.get("hold_qty", 0), "net_qty": sig.factors.get("net_qty", 0), "notional": round(trade_qty * sig.price, 2)}
    if sig.action == "BUY_LOW":
        dec["buy_signals"].append(signal_rec)
        dec["buy_low_signals"].append(signal_rec)
    elif sig.action == "ADD_POS":
        dec["buy_signals"].append(signal_rec)
        dec["buy_add_signals"].append(signal_rec)
    elif sig.action == "SELL_HIGH":
        dec["sell_signals"].append(signal_rec)
        dec["sell_high_signals"].append(signal_rec)
    else:
        dec["sell_signals"].append(signal_rec)
        dec["panic_sell_signals"].append(signal_rec)
    
    if not _should_push(f"{sig.code}-{sig.action}", sig):
        return
    
    if not FEISHU_WEBHOOK:
        log.debug("⚠️  FEISHU_WEBHOOK 未配置，跳过飞书推送")
        return
    
    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    at_all = feishu_cfg.get("at_all_on_signal", True)
    use_strong = feishu_cfg.get("use_strong_notification", True)
    relay_urgent_alarm = feishu_cfg.get("relay_urgent_alarm_on_feishu", True)
    if sig.action == "SELL_HIGH" and sig.factors.get("sell_stage") != "execute":
        use_strong = False
        relay_urgent_alarm = False
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    title = f"🚨🚨🚨 【加急】{FEISHU_KEYWORD} - 请立即查看 🚨🚨🚨" if use_strong else f"📢 【提醒】{FEISHU_KEYWORD}"
    
    reason_str = " + ".join(sig.reasons) if sig.reasons else "综合指标达标"
    
    # 【防爆修复】：动态获取日期写入独立日志
    with open(os.path.join(LOG_DIR, f"t_signals_{get_today_str()}.log"), "a", encoding="utf-8") as f:
        f.write(f"[{sig.ts.strftime('%H:%M:%S')}] {action_theme} | {action_cn} | {sig.name}({sig.code}) | 现价: {sig.price:.2f} | 强度: {sig.score:.0f}\n")
        f.write(f"  └─ 核心原因: {reason_str}\n  └─ 建议操作: {action_tip}\n")
        if sig.factors:
            f.write(f"  └─ 因子分解: {json.dumps(sig.factors, ensure_ascii=False, default=str)}\n")
    
    act_key = "BUY_LOW" if sig.action in ["BUY_LOW", "ADD_POS"] else "SELL_HIGH"
    AI_REVIEW_STATS[sig.code]["触发买入次数" if act_key == "BUY_LOW" else "触发卖出次数"] += 1
    
    log.warning(f"\n{'='*70}\n【触发】{action_cn} {sig.name}({sig.code}) 得:{sig.score:.0f}分\n* 原因: {reason_str}\n* 建议: {action_tip}\n{'='*70}")
    _register_signal_outcome(sig, holding)
    _append_jsonl(_trace_path("signal_outcome"), {
        "signal_time": sig.ts.strftime('%Y-%m-%d %H:%M:%S'),
        "code": sig.code,
        "name": sig.name,
        "action": sig.action,
        "signal_price": sig.price,
        "signal_score": sig.score,
        "signal_reasons": sig.reasons,
        "vwap_at_signal": sig.indicators.get("vwap", sig.price),
        "market_state_at_signal": sig.indicators.get("market_state", "unknown"),
        "benchmark_state_at_signal": sig.indicators.get("benchmark_state", "unknown"),
        "benchmark_gate_at_signal": sig.indicators.get("benchmark_gate", "neutral"),
        "buy_score": sig.factors.get("buy_score", 0),
        "sell_score": sig.factors.get("sell_score", 0),
        "buy_factors": sig.factors.get("buy_factors", {}),
        "sell_factors": sig.factors.get("sell_factors", {}),
        "hold_qty": sig.factors.get("hold_qty", 0),
        "net_qty": sig.factors.get("net_qty", 0),
        "cycle_count": sig.factors.get("cycle_count", 0),
        "minute_status": MINUTE_FETCH_STATUS.get(sig.code, "unknown"),
        "minute_detail": MINUTE_FETCH_DETAIL.get(sig.code, ""),
        "tracked": True,
        "priority_path": engine.diagnostics.get(sig.code, {}).get("priority_path", "hold") if isinstance(getattr(engine, "diagnostics", None), dict) else "hold",
        "buy_block_reasons": engine.diagnostics.get(sig.code, {}).get("buy_block_reasons", []) if isinstance(getattr(engine, "diagnostics", None), dict) else [],
        "sell_block_reasons": engine.diagnostics.get(sig.code, {}).get("sell_block_reasons", []) if isinstance(getattr(engine, "diagnostics", None), dict) else [],
        "preempted_by_sell_fast_path": engine.diagnostics.get(sig.code, {}).get("preempted_by_sell_fast_path", False) if isinstance(getattr(engine, "diagnostics", None), dict) else False,
    })
    
    if sig.code not in VIRTUAL_TRADES: VIRTUAL_TRADES[sig.code] = {"BUY_LOW": [], "SELL_HIGH": []}
    VIRTUAL_TRADES[sig.code][act_key].append({"price": sig.price, "qty": holding['t_qty']})
    
    dec = _ensure_daily_decision_stats(sig.code, holding)
    dec["last_price"] = sig.price
    dec["last_vwap"] = sig.indicators.get("vwap", sig.price)
    dec["last_score"] = sig.score
    signal_rec = {"time": sig.ts.strftime('%H:%M:%S'), "action": sig.action, "price": sig.price, "score": sig.score, "reasons": reason_str, "vwap": sig.indicators.get("vwap", sig.price)}
    if sig.action in ["BUY_LOW", "ADD_POS"]:
        dec["buy_signals"].append(signal_rec)
    else:
        dec["sell_signals"].append(signal_rec)
    
    if not FEISHU_WEBHOOK:
        log.debug("⚠️  FEISHU_WEBHOOK 未配置，跳过飞书推送")
        return
    
    card_elements = []
    if at_all:
        card_elements.append({
            "tag": "div",
            "text": {"content": at_text, "tag": "lark_md"}
        })
    card_elements.append({
        "tag": "div",
        "text": {"content": title, "tag": "lark_md"}
    })
    card_elements.append({
        "tag": "div",
        "text": {
            "content": (
                f"【{FEISHU_SYSTEM_KEYWORD}】\n"
                f"股票：{sig.name} ({sig.code})\n"
                f"动作：{action_cn}\n"
                f"主题色：{action_theme}\n"
                f"现价：{sig.price:.2f}\n"
                f"评分：{sig.score:.0f}\n"
                f"软确认：{'是' if sig.factors.get('soft_buy') else '否'}\n"
                f"市场状态：{sig.indicators.get('market_state', 'unknown')}\n"
                f"指数：{sig.indicators.get('benchmark_name', '')}({sig.indicators.get('benchmark_code', '')}) / {sig.indicators.get('benchmark_state', 'unknown')}\n"
                f"指数门控：{sig.indicators.get('benchmark_gate', 'neutral')}\n"
                f"集合竞价：{_get_auction_bias_label(sig.code)}\n"
                f"开盘：{_special_low_buy_stage_rule(sig.code, 'open')}\n"
                f"盘中：{_special_low_buy_stage_rule(sig.code, 'intraday')}\n"
                f"尾盘：{_special_low_buy_stage_rule(sig.code, 'eod')}\n"
                f"缩亏：{loss_rule or '暂无'}\n"
                f"阶段：{loss_stage_rule or '暂无'}\n"
                f"数量：{trade_qty} 股 | 该票按阶段专属比例缩放"
            ),
            "tag": "lark_md"
        }
    })
    card_elements.append({"tag": "hr"})
    # V1.11: 增强原因和建议内容，附带详细触发因子和操作建议
    vwap = float(sig.indicators.get("vwap", sig.price) or sig.price)
    today_ret = float(sig.indicators.get("today_ret", 0) or 0)
    market_state = str(sig.indicators.get("market_state", "unknown"))
    reasons_list = sig.reasons or ["综合指标达标"]
    reasons_detail = "\n".join([f"• {r}" for r in reasons_list[:5]])
    
    advice_extra = ""
    if sig.action == "SELL_HIGH":
        advice_extra = f"\n💡 **做T提示**：建议高抛后等回落接回，参考接回价 {vwap * 0.992:.2f}"
        if today_ret > 0.005:
            advice_extra += f"\n📈 早盘已涨 {today_ret*100:.1f}%，冲高是最佳高抛窗口"
    elif sig.action in ["BUY_LOW", "ADD_POS"]:
        advice_extra = f"\n💡 **做T提示**：建议低吸后等反弹高抛，参考卖出价 {vwap * 1.008:.2f}"
    
    card_elements.append({
        "tag": "div",
        "text": {
            "content": f"**触发原因**（按重要性排序）：\n{reasons_detail}\n\n**操作建议**：\n{action_tip}{advice_extra}",
            "tag": "lark_md"
        }
    })
    
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": card_elements
        },
        "notify_type": 1
    }
    send_feishu_payload(
        payload=payload,
        success_log=f"✅ 飞书消息已成功送达: {sig.name}({sig.code}) {sig.action} - 加急通知已发送",
        error_prefix="飞书推送",
        trigger_urgent_alarm_after_success=use_strong and relay_urgent_alarm,
    )

def load_trading_plan() -> Dict[str, Any]:
    """加载交易计划配置"""
    plan_file = os.path.join(BASE_DIR, "trading_plan.json")
    if not os.path.exists(plan_file):
        return {}
    try:
        with open(plan_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"⚠️  交易计划加载失败: {str(e)[:80]}")
        return {}

def _check_plan_trigger(code: str, price: float, plan: Dict[str, Any]) -> Optional[Signal]:
    """检查是否触发交易计划中的规则，返回信号或None"""
    strategy = plan.get("strategy", {})
    if code not in strategy:
        return None

    code_plan = strategy[code]
    rules = code_plan.get("rules", [])
    hold_qty = int(HOLDINGS.get(code, {}).get("t_qty", HOLDINGS.get(code, {}).get("qty", 0)) or 0)
    holding = HOLDINGS.get(code, {})

    for rule in rules:
        trigger = rule.get("trigger", "")
        action_str = rule.get("action", "HOLD")

        # 解析触发条件
        try:
            if "price" in trigger:
                # 支持 "price < 65", "65 <= price <= 66", "price > 70" 等
                trigger_eval = trigger.replace("price", str(price))
                if not eval(trigger_eval):
                    continue
            else:
                continue
        except Exception:
            continue

        # 符合触发条件
        action = str(action_str).upper()
        qty = rule.get("qty", 0)
        reason = rule.get("reason", "")

        if action == "ADD_POS":
            log.info(f"📋 交易计划触发【{code_plan.get('name', code)}】：{reason} (加仓{qty}股)")
            reasons = [f"计划-{reason}"]
            signal = Signal(
                code=code,
                name=holding.get("name", code),
                action="ADD_POS",
                price=price,
                score=85,
                reasons=reasons,
                details=[{"指标": "交易计划", "当前": reason, "加分": 85}],
                indicators={"price": price, "market_state": "plan_triggered"},
                factors={"hold_qty": hold_qty, "plan_action": "ADD_POS", "plan_qty": qty, "plan_reason": reason}
            )
            return signal
        elif action == "REDUCE":
            log.info(f"📋 交易计划触发【{code_plan.get('name', code)}】：{reason} (减仓{qty}股)")
            reasons = [f"计划-{reason}"]
            signal = Signal(
                code=code,
                name=holding.get("name", code),
                action="SELL_HIGH",
                price=price,
                score=85,
                reasons=reasons,
                details=[{"指标": "交易计划", "当前": reason, "加分": 85}],
                indicators={"price": price, "market_state": "plan_triggered"},
                factors={"hold_qty": hold_qty, "plan_action": "REDUCE", "plan_qty": qty, "plan_reason": reason}
            )
            return signal
        elif action == "CLEAR":
            log.info(f"📋 交易计划触发【{code_plan.get('name', code)}】：{reason} (清仓)")
            reasons = [f"计划-清仓{reason}"]
            signal = Signal(
                code=code,
                name=holding.get("name", code),
                action="SELL_HIGH",
                price=price,
                score=99,
                reasons=reasons,
                details=[{"指标": "交易计划清仓", "当前": reason, "加分": 99}],
                indicators={"price": price, "market_state": "plan_triggered"},
                factors={"hold_qty": hold_qty, "plan_action": "CLEAR", "plan_qty": hold_qty, "plan_reason": reason}
            )
            return signal

    # 检查止损规则
    stop_loss = code_plan.get("stop_loss", {})
    if stop_loss:
        stop_price = float(stop_loss.get("price", 0) or 0)
        stop_duration = int(stop_loss.get("duration_minutes", 0) or 0)
        if price < stop_price:
            log.warning(f"⚠️  【{code_plan.get('name', code)}】跌破止损价{stop_price}，触发减仓")
            reasons = [f"计划止损-跌破{stop_price}"]
            signal = Signal(
                code=code,
                name=holding.get("name", code),
                action="SELL_HIGH",
                price=price,
                score=95,
                reasons=reasons,
                details=[{"指标": "交易计划止损", "当前": f"跌破{stop_price}", "加分": 95}],
                indicators={"price": price, "market_state": "stop_loss_triggered"},
                factors={"hold_qty": hold_qty, "plan_action": "STOP_LOSS", "plan_qty": stop_loss.get("qty", 100), "plan_reason": stop_loss.get("reason", "")}
            )
            return signal

    return None

def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except Exception:
        return default


def _minute_status_label(status: str, detail: str = "") -> str:
    status = str(status or "unknown")
    mapping = {
        "ok": "正常",
        "cache_hit": "缓存命中",
        "network_timeout": "网络超时",
        "network_dns": "DNS失败",
        "network_ssl": "SSL失败",
        "network_http": "HTTP错误",
        "network_error": "网络错误",
        "json_empty": "返回空包",
        "json_html": "HTML拦截",
        "json_error": "JSON解析失败",
        "api_empty": "接口空数据",
        "symbol_missing": "标的缺失",
        "parse_no_rows": "无分钟数据",
        "parse_short_rows": "字段过短",
        "parse_type_rows": "类型异常",
        "parse_value_error": "数值异常",
        "parse_zero_placeholder": "占位0行",
        "parse_empty": "解析为空",
    }
    label_text = mapping.get(status, status)
    if detail and status not in {"ok", "cache_hit"}:
        return f"{label_text}:{detail[:18]}"
    return label_text


def _minute_issue_bucket(status: str) -> str:
    status = str(status or "unknown")
    if status in {"cache_hit", "ok", "未拉取"}:
        return "缓存"
    if status.startswith("network_"):
        return "网络"
    if status.startswith("json_"):
        return "接口"
    if status.startswith("api_") or status == "symbol_missing":
        return "接口"
    if status.startswith("parse_"):
        return "解析"
    return "其他"


@dataclass
class PreOpenContext:
    market_score: float = 0.0
    market_bias: str = "unknown"
    breadth: Dict[str, Any] = field(default_factory=dict)
    theme_rank: List[Dict[str, Any]] = field(default_factory=list)
    focus_codes: List[str] = field(default_factory=list)
    active_codes: List[str] = field(default_factory=list)
    watch_codes: List[str] = field(default_factory=list)
    blocked_codes: List[str] = field(default_factory=list)
    favored_sectors: List[str] = field(default_factory=list)
    weak_sectors: List[str] = field(default_factory=list)
    session_note: str = ""
    generated_at: str = ""
    source: str = "offline"
    market_snapshot: Dict[str, Any] = field(default_factory=dict)
    code_snapshots: Dict[str, Any] = field(default_factory=dict)
    auction_summary: Dict[str, Any] = field(default_factory=dict)


class PreOpenEngine:
    def __init__(self, holdings: Dict[str, dict], watchlist: Dict[str, dict]):
        self.holdings = holdings or {}
        self.watchlist = watchlist or {}

    def _sector_text(self, code: str, holding: dict) -> str:
        meta = self.watchlist.get(code, {}) if isinstance(self.watchlist, dict) else {}
        sector = meta.get("sector") or holding.get("sector") or ""
        return str(sector or "")

    def _build_theme_rank(self) -> List[Dict[str, Any]]:
        sector_counter: Dict[str, Dict[str, Any]] = {}
        for code, holding in self.holdings.items():
            sector_text = self._sector_text(code, holding)
            if not sector_text:
                continue
            parts = [p.strip() for p in sector_text.split("/") if p.strip()]
            if not parts:
                parts = [sector_text]
            score_base = 1.0 + float(holding.get("t_qty", 0) > 0)
            for part in parts:
                bucket = sector_counter.setdefault(part, {"sector": part, "count": 0, "score": 0.0, "codes": []})
                bucket["count"] += 1
                bucket["score"] += score_base
                bucket["codes"].append(code)
        ranked = sorted(sector_counter.values(), key=lambda x: (x["score"], x["count"]), reverse=True)
        return ranked

    def _pick_focus(self, theme_rank: List[Dict[str, Any]]) -> tuple[list[str], list[str], list[str], list[str]]:
        focus_codes: List[str] = []
        watch_codes: List[str] = []
        active_codes: List[str] = []
        blocked_codes: List[str] = []
        for item in theme_rank[:2]:
            for code in item.get("codes", [])[:4]:
                if code not in active_codes:
                    active_codes.append(code)
        for item in theme_rank[2:4]:
            for code in item.get("codes", [])[:4]:
                if code not in active_codes and code not in watch_codes:
                    watch_codes.append(code)
        for item in theme_rank[4:6]:
            for code in item.get("codes", [])[:4]:
                if code not in active_codes and code not in watch_codes and code not in focus_codes:
                    focus_codes.append(code)
        if theme_rank:
            weak_pool = theme_rank[-3:]
            for item in weak_pool:
                for code in item.get("codes", [])[:2]:
                    if code not in active_codes and code not in watch_codes and code not in focus_codes and code not in blocked_codes:
                        blocked_codes.append(code)
        return active_codes, watch_codes, focus_codes, blocked_codes

    def _auction_target_codes(self, active_codes: List[str], watch_codes: List[str], focus_codes: List[str]) -> List[str]:
        ordered: List[str] = []
        for code in list(active_codes or []) + list(watch_codes or []) + list(focus_codes or []) + list(self.holdings.keys()):
            code = str(code or "").strip()
            if code and code not in ordered:
                ordered.append(code)
        return ordered[:40]

    def _preopen_qt_symbol(self, code: str) -> str:
        code = str(code or "").strip()
        market = "sh" if code.startswith(("5", "6", "9")) else "sz"
        return f"{market}{code}"

    def _parse_qt_snapshot_line(self, line: str) -> tuple[str, Dict[str, Any]]:
        line = str(line or "").strip()
        if not line or "=\"" not in line:
            return "", {}
        symbol = line.split("=", 1)[0].strip()
        payload = line.split("=", 1)[1].strip().strip(';').strip('"')
        fields = payload.split("~")
        if len(fields) < 8:
            return "", {}
        code = str(fields[2]).strip() or symbol[-6:]
        try:
            price = float(fields[3] or 0)
        except Exception:
            price = 0.0
        try:
            volume = float(fields[6] or 0)
        except Exception:
            volume = 0.0
        amount = 0.0
        turnover_raw = ""
        for field in fields:
            parts = str(field).strip().split("/")
            if len(parts) == 3:
                try:
                    amount = float(parts[2] or 0)
                    turnover_raw = parts[2].strip()
                    break
                except Exception:
                    continue
        if amount <= 0:
            try:
                amount = float(fields[7] or 0) * 10000.0
                turnover_raw = str(fields[7]).strip()
            except Exception:
                amount = 0.0
        return code, {
            "symbol": symbol,
            "name": str(fields[1]).strip(),
            "price": price,
            "volume": volume,
            "amount": amount,
            "turnover_raw": turnover_raw,
            "source": "qt",
        }

    def _fetch_auction_snapshot_map(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        snapshot_map: Dict[str, Dict[str, Any]] = {}
        symbols = [self._preopen_qt_symbol(code) for code in codes if str(code or "").strip()]
        if not symbols:
            return snapshot_map
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.qq.com/",
        }
        for chunk in chunk_list(symbols, 40):
            url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
            text = ""
            try:
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()
                text = response.text or ""
            except Exception as e:
                log.debug(f"⚠️  集合竞价快照抓取失败: {type(e).__name__}: {str(e)[:120]}")
                continue
            if not text.strip():
                continue
            for line in text.splitlines():
                code, data = self._parse_qt_snapshot_line(line)
                if not code or not data:
                    continue
                if data.get("price", 0) <= 0:
                    continue
                snapshot_map[code] = data
        return snapshot_map

    def _build_auction_profile(self, code: str, holding: dict, raw: Dict[str, Any]) -> Dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        price = float(raw.get("price", 0) or 0)
        amount = float(raw.get("amount", 0) or 0)
        volume = float(raw.get("volume", 0) or 0)
        daily_ctx = get_daily_context(code, holding or {}, current_price=price)
        prev_close = float(daily_ctx.get("daily_prev_close", 0) or 0)
        if prev_close <= 0:
            prev_close = float(daily_ctx.get("daily_price_ref", 0) or 0)
        open_gap = (price - prev_close) / prev_close if prev_close else 0.0
        gate = str(daily_ctx.get("daily_gate", "neutral") or "neutral")
        trend_bg = str(daily_ctx.get("daily_trend_bg", "unknown") or "unknown")
        score = 0.0
        if prev_close > 0 and price > 0:
            if 0.0 <= open_gap <= 0.03:
                score += 18.0
            elif 0.03 < open_gap <= 0.07:
                score += 10.0
            elif open_gap < -0.03:
                score -= 15.0
            else:
                score -= 6.0
        else:
            score -= 8.0
        if amount > 0:
            if amount >= 1.5e8:
                score += 12.0
            elif amount >= 5e7:
                score += 7.0
            elif amount >= 1e7:
                score += 2.0
            else:
                score -= 5.0
        if gate in {"supportive", "neutral"}:
            score += 6.0
        elif gate in {"risk", "overheat"}:
            score -= 10.0
        if trend_bg in {"bull", "uptrend"}:
            score += 6.0
        elif trend_bg in {"weak_breakdown", "downtrend"}:
            score -= 10.0
        if bool(daily_ctx.get("daily_ma_clustered")) and open_gap >= 0:
            score += 4.0
        if bool(daily_ctx.get("daily_hard_breakdown")):
            score -= 12.0
        if bool(daily_ctx.get("daily_breakdown_risk")):
            score -= 8.0
        data_quality = 1.0
        if price <= 0:
            data_quality -= 0.5
        if prev_close <= 0:
            data_quality -= 0.3
        if amount <= 0:
            data_quality -= 0.2
        if volume <= 0:
            data_quality -= 0.1
        data_quality = float(_clamp(data_quality, 0.0, 1.0))
        auction_tag = "flat_open"
        if data_quality < 0.5:
            auction_tag = "stale_or_missing"
        elif score >= 18:
            auction_tag = "strong_open"
        elif score <= -12:
            auction_tag = "weak_open"
        return {
            "code": code,
            "name": raw.get("name", holding.get("name", code)),
            "price": price,
            "prev_close": prev_close,
            "open_gap": open_gap,
            "volume": volume,
            "amount": amount,
            "auction_score": score,
            "auction_tag": auction_tag,
            "data_quality": data_quality,
            "daily_gate": gate,
            "daily_trend_bg": trend_bg,
            "daily_pullback_support": bool(daily_ctx.get("daily_pullback_support")),
            "daily_near_support": bool(daily_ctx.get("daily_near_support")),
            "daily_breakdown_risk": bool(daily_ctx.get("daily_breakdown_risk")),
            "daily_hard_breakdown": bool(daily_ctx.get("daily_hard_breakdown")),
            "source": raw.get("source", "qt"),
            "raw": raw,
            "daily_context": daily_ctx,
        }

    def _sort_codes_by_auction_score(self, codes: List[str], profiles: Dict[str, Dict[str, Any]]) -> List[str]:
        return sorted(
            [str(code) for code in codes if str(code or "").strip()],
            key=lambda c: (-float(profiles.get(c, {}).get("auction_score", -999.0) or -999.0), c)
        )

    def _fetch_market_snapshot(self) -> Dict[str, Any]:
        snapshot = {
            "source": "watchlist",
            "market_open": False,
            "index_trend": "unknown",
            "advance_decline": "unknown",
            "hot_theme": [],
            "risk_flag": "unknown",
            "market_sentence": "",
        }
        try:
            spot = pd.DataFrame()
            for fn in ["stock_zh_a_spot_em", "stock_zh_a_spot"]:
                if hasattr(ak, fn):
                    try:
                        spot = getattr(ak, fn)()
                        if isinstance(spot, pd.DataFrame) and not spot.empty:
                            break
                    except Exception:
                        continue
            if isinstance(spot, pd.DataFrame) and not spot.empty:
                snapshot["source"] = "spot"
                cols = set(spot.columns)
                if {"涨跌幅", "名称"}.issubset(cols):
                    up = int((pd.to_numeric(spot["涨跌幅"], errors="coerce") > 0).sum())
                    down = int((pd.to_numeric(spot["涨跌幅"], errors="coerce") < 0).sum())
                    flat = int(len(spot) - up - down)
                    snapshot["advance_decline"] = {"up": up, "down": down, "flat": flat}
                    snapshot["risk_flag"] = "risk_on" if up >= max(1, down * 1.2) else ("risk_off" if down > up else "neutral")
                if {"涨跌幅", "概念板块"}.issubset(cols):
                    concept_df = spot[["名称", "涨跌幅"]].copy()
                    concept_df["涨跌幅"] = pd.to_numeric(concept_df["涨跌幅"], errors="coerce")
                    top = concept_df.sort_values("涨跌幅", ascending=False).head(5)
                    snapshot["hot_theme"] = top["名称"].dropna().astype(str).tolist()
                    if not top.empty:
                        snapshot["index_trend"] = "positive" if float(top.iloc[0]["涨跌幅"] or 0) > 0 else "negative"
                elif "代码" in cols and "名称" in cols:
                    top = spot.head(5)
                    snapshot["hot_theme"] = top["名称"].dropna().astype(str).tolist()
        except Exception:
            pass
        if not snapshot["market_sentence"]:
            adv = snapshot.get("advance_decline", {})
            if isinstance(adv, dict) and adv and adv.get("up") is not None:
                snapshot["market_sentence"] = f"涨{adv.get('up', 0)} / 跌{adv.get('down', 0)} / 平{adv.get('flat', 0)}"
            else:
                snapshot["market_sentence"] = "市场快照不足，按名单结构解读"
        return snapshot

    def evaluate(self) -> PreOpenContext:
        market_snapshot_raw = self._fetch_market_snapshot()
        market_snapshot = market_snapshot_raw if isinstance(market_snapshot_raw, dict) else {}
        theme_rank = self._build_theme_rank()
        active_codes, watch_codes, focus_codes, blocked_codes = self._pick_focus(theme_rank)
        total = max(1, len(self.holdings))
        etf_count = sum(1 for h in self.holdings.values() if h.get("type") == "etf")
        stock_count = total - etf_count
        concentrated = theme_rank[0]["count"] / total if theme_rank else 0.0
        market_score = 40.0 + min(30.0, concentrated * 30.0) + min(10.0, etf_count * 1.5) - min(8.0, stock_count * 0.1)
        if len(theme_rank) >= 2 and theme_rank[0]["count"] > theme_rank[1]["count"]:
            market_score += 4.0
        if theme_rank and theme_rank[0]["count"] >= max(3, total // 3):
            market_score += 6.0
        if theme_rank and theme_rank[0]["count"] <= 1:
            market_score -= 8.0
        market_score = float(_clamp(market_score, 0, 100))
        if market_score >= 72:
            market_bias = "risk_on"
        elif market_score >= 58:
            market_bias = "neutral_to_positive"
        elif market_score <= 38:
            market_bias = "risk_off"
        else:
            market_bias = "neutral"

        favored_sectors = [item["sector"] for item in theme_rank[:3]]
        weak_sectors = [item["sector"] for item in theme_rank[-3:]] if theme_rank else []
        market_open = bool(market_snapshot.get("market_open", False))
        risk_flag = str(market_snapshot.get("risk_flag", "unknown"))
        adv = market_snapshot.get("advance_decline", {}) if isinstance(market_snapshot, dict) else {}
        up = int(adv.get("up", 0) or 0) if isinstance(adv, dict) else 0
        down = int(adv.get("down", 0) or 0) if isinstance(adv, dict) else 0
        hot_theme = market_snapshot.get("hot_theme", []) if isinstance(market_snapshot, dict) else []
        hot_theme_text = "、".join(hot_theme[:3]) if isinstance(hot_theme, list) else str(hot_theme)

        target_codes = self._auction_target_codes(active_codes, watch_codes, focus_codes)
        raw_snapshots = self._fetch_auction_snapshot_map(target_codes)
        code_snapshots: Dict[str, Dict[str, Any]] = {}
        for code in target_codes:
            holding = self.holdings.get(code, {}) if isinstance(self.holdings, dict) else {}
            code_snapshots[code] = self._build_auction_profile(code, holding, raw_snapshots.get(code, {}))

        auction_scores = [float(item.get("auction_score", 0.0) or 0.0) for item in code_snapshots.values() if item.get("data_quality", 0.0) >= 0.4]
        auction_mean_score = float(sum(auction_scores) / max(1, len(auction_scores))) if auction_scores else 0.0
        strong_open_count = sum(1 for item in code_snapshots.values() if item.get("auction_tag") == "strong_open")
        weak_open_count = sum(1 for item in code_snapshots.values() if item.get("auction_tag") == "weak_open")
        missing_open_count = sum(1 for item in code_snapshots.values() if item.get("auction_tag") == "stale_or_missing")
        auction_summary = {
            "target_count": len(target_codes),
            "snapshot_count": len(raw_snapshots),
            "strong_open_count": strong_open_count,
            "weak_open_count": weak_open_count,
            "missing_open_count": missing_open_count,
            "mean_score": round(auction_mean_score, 2),
            "source": "qt",
            "source_ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        market_score += min(6.0, max(-6.0, auction_mean_score / 6.0))
        if strong_open_count >= max(1, len(target_codes) // 8):
            market_score += 2.0
        if weak_open_count >= max(1, len(target_codes) // 6):
            market_score -= 2.5
        market_score += min(10.0, max(-6.0, (up - down) / max(1, total) * 1.2))
        if risk_flag == "risk_on":
            market_score += 4.0
        elif risk_flag == "risk_off":
            market_score -= 5.0
        if market_open and up > down:
            market_score += 3.0
        elif market_open and down > up:
            market_score -= 3.0
        if hot_theme:
            market_score += 2.0 if len(hot_theme) >= 3 else 0.5
        market_score = float(_clamp(market_score, 0, 100))
        if market_score >= 72:
            market_bias = "risk_on"
        elif market_score >= 58:
            market_bias = "neutral_to_positive"
        elif market_score <= 38:
            market_bias = "risk_off"
        else:
            market_bias = "neutral"

        active_codes = self._sort_codes_by_auction_score(active_codes, code_snapshots)
        watch_codes = self._sort_codes_by_auction_score(watch_codes, code_snapshots)
        focus_codes = self._sort_codes_by_auction_score(focus_codes, code_snapshots)
        blocked_codes = self._sort_codes_by_auction_score(blocked_codes, code_snapshots)

        breadth = {
            "total_codes": total,
            "etf_count": etf_count,
            "stock_count": stock_count,
            "theme_count": len(theme_rank),
            "top_theme_share": round(concentrated, 3),
            "advance_decline": adv,
            "hot_theme": hot_theme,
            "risk_flag": risk_flag,
            "market_open": market_open,
            "hot_theme_text": hot_theme_text,
            "auction_summary": auction_summary,
        }
        session_note = (
            f"盘面快照 {market_snapshot.get('market_sentence', '暂无')}"
            if market_open or up or down or hot_theme
            else "盘面偏强，适合顺势低吸" if market_bias in {"risk_on", "neutral_to_positive"} else ("盘面偏弱，优先控仓等待" if market_bias == "risk_off" else "盘面中性，按信号择机")
        )
        if auction_summary["target_count"]:
            session_note += f" | 竞价强{strong_open_count}/弱{weak_open_count}/缺{missing_open_count}"
        return PreOpenContext(
            market_score=market_score,
            market_bias=market_bias,
            breadth=breadth,
            theme_rank=theme_rank[:8],
            focus_codes=focus_codes,
            active_codes=active_codes,
            watch_codes=watch_codes,
            blocked_codes=blocked_codes,
            favored_sectors=favored_sectors,
            weak_sectors=weak_sectors,
            session_note=session_note,
            generated_at=_now().strftime("%Y-%m-%d %H:%M:%S"),
            source=market_snapshot.get("source", "watchlist"),
            market_snapshot=market_snapshot,
            code_snapshots=code_snapshots,
            auction_summary=auction_summary,
        )

    def persist(self, context: PreOpenContext) -> None:
        try:
            os.makedirs(PREOPEN_DIR, exist_ok=True)
            with open(_preopen_path(), "w", encoding="utf-8") as f:
                json.dump(context.__dict__, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _trend_reason_label(path: str) -> str:
    mapping = {
        "sell_fast_path": "卖出快速路径",
        "sell_confirm_path": "卖出确认路径",
        "buy_path": "买入路径",
        "buy_soft_path": "买入软确认路径",
        "hold": "持有",
    }
    return mapping.get(path, path or "持有")


def _buy_soft_support_count(buy_momentum_ok: bool, buy_ema_ok: bool, buy_volume_ok: bool, buy_price_ok: bool, buy_gap_ok: bool, buy_detail_count_ok: bool, buy_time_ready: bool) -> int:
    return sum([buy_momentum_ok, buy_ema_ok, buy_volume_ok, buy_price_ok, buy_gap_ok, buy_detail_count_ok, buy_time_ready])


def _starvation_state_file() -> str:
    return os.path.join(T_IO_DIR, "buy_starvation_state.json")


def _merge_memory(code: str, updates: dict):
    current = STRATEGY_MEMORY.get(code, {}) if isinstance(STRATEGY_MEMORY, dict) else {}
    merged = dict(current)
    sample_count = int(merged.get("sample_count", 0) or 0) + 1
    merged["sample_count"] = sample_count
    if sample_count < 3:
        return
    for key, value in updates.items():
        if isinstance(value, (int, float)):
            base = merged.get(key, value)
            if not isinstance(base, (int, float)):
                base = value
            merged[key] = base * 0.85 + value * 0.15
        else:
            merged[key] = value
    STRATEGY_MEMORY[code] = merged
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(STRATEGY_MEMORY, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _learning_state_file() -> str:
    return os.path.join(T_IO_DIR, "t_trader_learning_state.json")


def load_learning_state() -> Dict[str, dict]:
    path = _learning_state_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_learning_state(state: Dict[str, dict]):
    try:
        with open(_learning_state_file(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _replay_health(stats: dict) -> float:
    return float(
        stats.get("buy_ok", 0) * 2
        + stats.get("sell_ok", 0) * 2
        + stats.get("rebuild_buy_ok", 0)
        - stats.get("buy_candidate_but_rejected", 0)
        - stats.get("preempt_by_sell_fast_path", 0)
        - stats.get("buy_blocked", 0) * 0.2
        - stats.get("sell_blocked", 0) * 0.2
    )


def _dominant_reason(counter: dict) -> tuple[str, int]:
    if not counter:
        return "", 0
    reason, count = max(counter.items(), key=lambda kv: kv[1])
    return str(reason), int(count)


def _learning_patch_for_code(code: str, code_stats: dict) -> tuple[dict, str]:
    buy_reason, buy_count = _dominant_reason(code_stats.get("buy_block_by_reason", {}))
    sell_reason, sell_count = _dominant_reason(code_stats.get("sell_block_by_reason", {}))
    preempt = int(code_stats.get("preempt_by_sell_fast_path", 0) or 0)
    cand_rej = int(code_stats.get("buy_candidate_but_rejected", 0) or 0)

    patch = {}
    reason = ""
    if preempt > 0 and preempt >= max(2, cand_rej // 3):
        patch = {"buy_priority_margin": 3}
        reason = "卖快路径抢占"
    elif buy_reason in {"buy_momentum_fail", "buy_volume_fail", "buy_ema_fail"} and buy_count > 0:
        if buy_reason == "buy_momentum_fail":
            patch = {"buy_confirm_min_seconds": 20}
        elif buy_reason == "buy_volume_fail":
            patch = {"vol_ratio_confirm": 1.7}
        else:
            patch = {"buy_confirm_min_factors": 2}
        reason = buy_reason
    elif buy_reason in {"buy_confirm_wait", "buy_detail_fail", "buy_gap_fail", "buy_price_fail", "post_sell_block"} and buy_count > 0:
        if buy_reason == "buy_confirm_wait":
            patch = {"buy_confirm_min_seconds": 15}
        elif buy_reason == "buy_detail_fail":
            patch = {"buy_confirm_min_factors": 2}
        elif buy_reason == "buy_gap_fail":
            patch = {"buy_rebound_min_score_gap": 2}
        elif buy_reason == "buy_price_fail":
            patch = {"buy_confirm_min_score": 40}
        else:
            patch = {"post_sell_rebuild_min_seconds": 20}
        reason = buy_reason
    elif sell_reason in {"sell_confirm_wait", "sell_detail_fail", "sell_momentum_fail", "sell_ema_fail", "sell_volume_fail"} and sell_count > 0:
        if sell_reason == "sell_confirm_wait":
            patch = {"sell_confirm_min_seconds": 25}
        elif sell_reason == "sell_detail_fail":
            patch = {"sell_confirm_min_factors": 4}
        elif sell_reason == "sell_momentum_fail":
            patch = {"sell_needs_momentum": True}
        elif sell_reason == "sell_ema_fail":
            patch = {"sell_needs_ema": True}
        else:
            patch = {"sell_needs_volume": True}
        reason = sell_reason
    elif cand_rej > 0:
        patch = {"buy_confirm_min_score": 40}
        reason = "买候选未成交"

    return patch, reason


def _apply_replay_learning(today: str):
    replay_file = os.path.join(TRACE_DIR, f"replay_compare_{today}.json")
    if not os.path.exists(replay_file):
        return
    try:
        with open(replay_file, "r", encoding="utf-8") as f:
            replay_data = json.load(f)
    except Exception:
        return

    stats = replay_data.get("stats", {}) if isinstance(replay_data, dict) else {}
    by_code = stats.get("by_code", {}) if isinstance(stats, dict) else {}
    if not isinstance(by_code, dict) or not by_code:
        return

    learning_state = load_learning_state()
    changed = []
    for code, code_stats in by_code.items():
        if not isinstance(code_stats, dict):
            continue
        current_health = _replay_health(code_stats)
        state = learning_state.get(code, {}) if isinstance(learning_state, dict) else {}
        last_health = float(state.get("last_health", -9999) or -9999)
        last_patch = state.get("last_patch", {}) if isinstance(state, dict) else {}
        last_snapshot = state.get("last_snapshot", {}) if isinstance(state, dict) else {}

        if last_patch and current_health < last_health:
            if last_snapshot:
                STRATEGY_MEMORY[code] = dict(last_snapshot)
                changed.append(f"{code} 回滚上次学习补丁")
            learning_state[code] = {
                "last_health": current_health,
                "last_patch": {},
                "last_snapshot": dict(STRATEGY_MEMORY.get(code, {})),
                "last_date": today,
                "rollbacked": True,
                "rollback_reason": "health_down",
            }
            continue

        patch, reason = _learning_patch_for_code(code, code_stats)
        if not patch:
            learning_state[code] = {
                "last_health": current_health,
                "last_patch": {},
                "last_snapshot": dict(STRATEGY_MEMORY.get(code, {})),
                "last_date": today,
                "reason": "",
            }
            continue

        current_memory = dict(STRATEGY_MEMORY.get(code, {}))
        snapshot = dict(current_memory)
        updated = dict(current_memory)
        updated.update(patch)
        STRATEGY_MEMORY[code] = updated
        changed.append(f"{code} {reason} -> {patch}")
        learning_state[code] = {
            "last_health": current_health,
            "last_patch": patch,
            "last_snapshot": snapshot,
            "last_date": today,
            "reason": reason,
        }

    if changed:
        save_learning_state(learning_state)
        try:
            with open(LEARNING_FILE, "w", encoding="utf-8") as f:
                json.dump(STRATEGY_MEMORY, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        log.info("学习更新: " + " | ".join(changed[:10]))


def log_eod_summary():
    global _eod_logged_date
    today = get_today_str()
    if _eod_logged_date == today: return

    lines = ["\n" + "═"*20 + " 【尾盘做T盈亏清算】 " + "═"*20]
    total_net_pnl = 0.0
    misjudge_counter = {"buy_early": {}, "sell_early": {}, "missed_buy": {}, "missed_sell": {}}
    misjudge_factor_counter = {"buy_early": {}, "sell_early": {}, "missed_buy": {}, "missed_sell": {}}
    action_misjudge_phase_factor_counter = {
        "BUY_LOW": {"early": {}, "missed": {}},
        "ADD_POS": {"early": {}, "missed": {}},
        "SELL_HIGH": {"early": {}, "missed": {}},
        "PANIC_SELL": {"early": {}, "missed": {}},
    }
    etf_review = []
    stock_review = []
    minute_issues = []
    minute_issue_stats = {"网络": {}, "解析": {}, "接口": {}, "缓存": {}, "其他": {}}

    def _brief_reasons(text: str, limit: int = 3) -> str:
        parts = [x for x in str(text or "").split(" + ") if x]
        if not parts:
            return "综合指标达标"
        short = " + ".join(parts[:limit])
        return short + ("..." if len(parts) > limit else "")

    def _bump(counter: dict, key: str):
        counter[key] = counter.get(key, 0) + 1

    action_bucket_counts = {
        "buy_low": 0,
        "buy_add": 0,
        "sell_high": 0,
        "panic_sell": 0,
    }
    action_bucket_qty = {
        "buy_low": 0,
        "buy_add": 0,
        "sell_high": 0,
        "panic_sell": 0,
    }
    for code, holding in HOLDINGS.items():
        dec = _ensure_daily_decision_stats(code, holding)
        buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
        sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
        buy_amt = sum(t["qty"] * t["price"] for t in buys)
        sell_amt = sum(t["qty"] * t["price"] for t in sells)
        fees = (buy_amt + sell_amt) * PARAMS["commission_rate"]

        current_price = dec.get("close_price") or dec.get("last_price") or holding["cost"]
        if not current_price or current_price <= 0:
            current_price = float(holding["cost"])

        net_qty = sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells)
        pnl = sell_amt - buy_amt + (net_qty * current_price) - fees
        total_net_pnl += pnl

        buy_signals = dec.get("buy_signals", [])
        buy_low_signals = dec.get("buy_low_signals", [])
        buy_add_signals = dec.get("buy_add_signals", [])
        sell_signals = dec.get("sell_signals", [])
        sell_high_signals = dec.get("sell_high_signals", [])
        panic_sell_signals = dec.get("panic_sell_signals", [])
        buy_count = len(buy_signals)
        sell_count = len(sell_signals)
        last_price = dec.get("last_price", current_price)
        last_vwap = dec.get("last_vwap", current_price)
        last_score = dec.get("last_score", 0)
        decision_bias = "偏买" if buy_count > sell_count else ("偏卖" if sell_count > buy_count else "均衡")
        close_gap = (current_price - last_vwap) / last_vwap if last_vwap else 0.0

        has_signal = buy_count > 0 or sell_count > 0
        has_trade = len(buys) > 0 or len(sells) > 0

        if has_signal or has_trade:
            lines.append(f"► {holding.get('name', code)}({code})")
            lines.append(f"   盘终敞口 {net_qty}股 | 做T净利润 {'+' if pnl>0 else ''}{pnl:.2f} 元 | 决策偏向 {decision_bias}")
            lines.append(f"   盘中信号 买{buy_count}次/卖{sell_count}次 | 分数 {last_score:.0f} | 现价 {current_price:.2f} | 偏离VWAP {close_gap*100:+.2f}%")
            lines.append(f"   指数联动 {dec.get('last_benchmark_name', '')} | 状态 {dec.get('last_benchmark_state', 'unknown')} | 门控 {dec.get('last_benchmark_gate', 'neutral')}")
            if buy_count or sell_count:
                last_buy = buy_signals[-1] if buy_count else None
                last_sell = sell_signals[-1] if sell_count else None
                if last_buy:
                    lines.append(f"   最近买点 {last_buy['time']} {last_buy['action']} 价{last_buy['price']:.2f} 分{last_buy['score']:.0f} | {_brief_reasons(last_buy['reasons'])}")
                if last_sell:
                    lines.append(f"   最近卖点 {last_sell['time']} {last_sell['action']} 价{last_sell['price']:.2f} 分{last_sell['score']:.0f} | {_brief_reasons(last_sell['reasons'])}")

            if buy_signals:
                buy_max_score = max(item["score"] for item in buy_signals)
                buy_best = max(buy_signals, key=lambda item: item["score"])
                buy_hit = sum(1 for item in buy_signals if item["price"] <= current_price)
                buy_success = buy_hit / buy_count if buy_count else 0.0
                lines.append(f"   买点轨迹 分{buy_max_score:.0f} | 有利 {buy_hit}/{buy_count} | 命中 {buy_success*100:.0f}% | 最佳 {buy_best['time']}@{buy_best['price']:.2f}")
            if sell_signals:
                sell_max_score = max(item["score"] for item in sell_signals)
                sell_best = max(sell_signals, key=lambda item: item["score"])
                sell_hit = sum(1 for item in sell_signals if item["price"] >= current_price)
                sell_success = sell_hit / sell_count if sell_count else 0.0
                lines.append(f"   卖点轨迹 分{sell_max_score:.0f} | 有利 {sell_hit}/{sell_count} | 命中 {sell_success*100:.0f}% | 最佳 {sell_best['time']}@{sell_best['price']:.2f}")

            if buy_signals and current_price > last_vwap:
                lines.append("   盘后初判 买点整体偏早，若后续回落则可进一步收紧")
                _bump(misjudge_counter["buy_early"], code)
                for item in buy_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["buy_early"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["early"], factor)
            if sell_signals and current_price < last_vwap * 0.992:
                lines.append("   盘后初判 卖点整体偏早，若后续继续下探且偏离明显，则可保留当前卖法")
                _bump(misjudge_counter["sell_early"], code)
                for item in sell_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["sell_early"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["early"], factor)
            if buy_signals and current_price < last_vwap:
                lines.append("   盘后初判 买点在收盘未占优，买点条件仍可再校准")
                _bump(misjudge_counter["missed_buy"], code)
                for item in buy_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["missed_buy"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["missed"], factor)
            if sell_signals and current_price > last_vwap * 1.002:
                lines.append("   盘后初判 卖点在收盘未占优，卖点条件仍可再校准")
                _bump(misjudge_counter["missed_sell"], code)
                for item in sell_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["missed_sell"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["missed"], factor)

        review_bucket = etf_review if holding.get("type") == "etf" else stock_review
        review_bucket.append(f"{holding.get('name', code)}({code}) {buy_count}/{sell_count} | {current_price:.2f} | VWAP{last_vwap:.2f} | {close_gap*100:+.2f}%")

        m_status = dec.get("minute_status", "未拉取")
        if m_status not in ("ok", "cache_hit", "未拉取"):
            minute_issues.append(f"{holding.get('name', code)}({code})[{_minute_status_label(m_status, dec.get('minute_detail', ''))}] {dec.get('minute_detail', '')[:28]}")

    def _top_keys(counter: dict, n: int = 3):
        return sorted(counter.items(), key=lambda x: x[1], reverse=True)[:n]

    lines.append(f"💰 今日预估做T总净利润: {'+' if total_net_pnl>0 else ''}{total_net_pnl:.2f} 元\n")
    lines.append("═"*20 + " 【动作分类汇总】 " + "═"*20)
    lines.append(f"低吸 BUY_LOW={action_bucket_counts['buy_low']}次/{action_bucket_qty['buy_low']}股 | 加仓 ADD_POS={action_bucket_counts['buy_add']}次/{action_bucket_qty['buy_add']}股")
    lines.append(f"高抛 SELL_HIGH={action_bucket_counts['sell_high']}次/{action_bucket_qty['sell_high']}股 | 减仓 PANIC_SELL={action_bucket_counts['panic_sell']}次/{action_bucket_qty['panic_sell']}股")
    lines.append("═"*20 + " 【ETF复盘小结】 " + "═"*20)
    if etf_review:
        lines.append(f"ETF {len(etf_review)}票 | 有信号 {sum(1 for x in etf_review if not x.endswith(' 0/0 | 0.00 | +0.00%'))}")
        lines.extend(etf_review)
    else:
        lines.append("暂无ETF复盘数据")
    lines.append("═"*20 + " 【普通股复盘小结】 " + "═"*20)
    if stock_review:
        lines.append(f"普通股 {len(stock_review)}票 | 有信号 {sum(1 for x in stock_review if not x.endswith(' 0/0 | 0.00 | +0.00%'))}")
        lines.extend(stock_review)
    else:
        lines.append("暂无普通股复盘数据")
    lines.append("═"*20 + " 【误判因子排行】 " + "═"*20)
    for title, counter in [("买点偏早", misjudge_counter["buy_early"]), ("卖点偏早", misjudge_counter["sell_early"]), ("漏买", misjudge_counter["missed_buy"]), ("漏卖", misjudge_counter["missed_sell"])]:
        top_items = _top_keys(counter)
        if top_items:
            lines.append(f"{title}: " + ", ".join(f"{k}:{v}" for k, v in top_items))
        else:
            lines.append(f"{title}: 暂无")
    lines.append("═"*20 + " 【误判因子细分】 " + "═"*20)
    for title, counter in [("买点偏早因子", misjudge_factor_counter["buy_early"]), ("卖点偏早因子", misjudge_factor_counter["sell_early"]), ("漏买因子", misjudge_factor_counter["missed_buy"]), ("漏卖因子", misjudge_factor_counter["missed_sell"])]:
        top_items = _top_keys(counter)
        if top_items:
            lines.append(f"{title}: " + ", ".join(f"{k}:{v}" for k, v in top_items))
        else:
            lines.append(f"{title}: 暂无")
    lines.append("═"*20 + " 【四类动作误判因子】 " + "═"*20)
    for action, title in [("BUY_LOW", "低吸"), ("ADD_POS", "加仓"), ("SELL_HIGH", "高抛"), ("PANIC_SELL", "跳水")]:
        early_top = _top_keys(action_misjudge_phase_factor_counter[action]["early"], 3)
        missed_top = _top_keys(action_misjudge_phase_factor_counter[action]["missed"], 3)
        if early_top:
            lines.append(f"{title} {action} 偏早: " + ", ".join(f"{k}:{v}" for k, v in early_top))
        else:
            lines.append(f"{title} {action} 偏早: 暂无")
        if missed_top:
            lines.append(f"{title} {action} 未占优: " + ", ".join(f"{k}:{v}" for k, v in missed_top))
        else:
            lines.append(f"{title} {action} 未占优: 暂无")
    if any(minute_issue_stats.values()):
        lines.append("═"*20 + " 【分钟线异常摘要】 " + "═"*20)
        short_minute_lines = []
        for bucket in ["网络", "解析", "接口", "缓存", "其他"]:
            items = minute_issue_stats.get(bucket, {})
            total = sum(items.values()) if isinstance(items, dict) else 0
            if total <= 0:
                continue
            top_items = sorted(items.items(), key=lambda kv: kv[1], reverse=True)[:3]
            short_minute_lines.append(f"{bucket}类 {total}项 | " + ", ".join(f"{k}:{v}" for k, v in top_items))
        lines.extend(short_minute_lines)
    minute_issue_total = sum(sum(items.values()) for items in minute_issue_stats.values() if isinstance(items, dict))
    if minute_issue_total:
        lines.insert(1, f"分钟线异常总计 {minute_issue_total} 项 | 网络 {sum(minute_issue_stats.get('网络', {}).values())} | 解析 {sum(minute_issue_stats.get('解析', {}).values())} | 接口 {sum(minute_issue_stats.get('接口', {}).values())}")
    lines.append("═"*20 + " 【策略最高分复盘数据(喂给AI)】 " + "═"*20)
    for code, holding in HOLDINGS.items():
        stats = AI_REVIEW_STATS.get(code, {})
        dec = DAILY_DECISION_STATS.get(code, {})
        name = stats.get("名称", holding.get("name", code))
        max_buy = stats.get("最大多头分", 0)
        max_sell = stats.get("最大空头分", 0)
        max_amp = stats.get("最大振幅", 0)
        buy_trig = stats.get("触发买入次数", 0)
        sell_trig = stats.get("触发卖出次数", 0)
        close_price = dec.get("close_price", dec.get("last_price", holding["cost"]))
        last_vwap = dec.get("last_vwap", close_price)
        vwap_dev = (close_price - last_vwap) / last_vwap * 100 if last_vwap else 0.0
        buy_qty = int(stats.get("触发买入股数", 0) or 0)
        sell_qty = int(stats.get("触发卖出股数", 0) or 0)
        lines.append(f"{name}({code}): 多={max_buy:.0f} 空={max_sell:.0f} 振={max_amp*100:.2f}% 偏={vwap_dev:+.2f}% 买={buy_trig}/{buy_qty} 卖={sell_trig}/{sell_qty}")
    lines.append("═"*20 + " 【复盘阅读顺序】 " + "═"*20)
    lines.append(f"1. sys日志 -> 2. data_quality -> 3. decision_trace")
    lines.append(f"4. shadow_signals -> 5. signal_outcome -> 6. ai_review")
    lines.append("═"*20 + " 【信号结果追踪汇总】 " + "═"*20)
    outcome_counts = {"correct": 0, "buy_early": 0, "sell_early": 0, "buy_validating": 0, "sell_validating": 0, "hold_pending": 0}
    side_counts = {"BUY_LOW": 0, "ADD_POS": 0, "SELL_HIGH": 0, "PANIC_SELL": 0}
    win_counts = {"win_5m": 0, "win_15m": 0}
    mat_counts = {"maturity_5m": 0, "maturity_15m": 0}
    action_maturity = {"BUY_LOW": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}, "ADD_POS": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}, "SELL_HIGH": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}, "PANIC_SELL": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}}
    action_early = {"BUY_LOW": 0, "ADD_POS": 0, "SELL_HIGH": 0, "PANIC_SELL": 0}
    action_validating = {"BUY_LOW": 0, "ADD_POS": 0, "SELL_HIGH": 0, "PANIC_SELL": 0}
    trace_files = []
    try:
        trace_files = [f for f in os.listdir(TRACE_DIR) if f.startswith("signal_outcome_") and f.endswith(".jsonl")]
    except Exception:
        trace_files = []
    for fname in trace_files:
        try:
            with open(os.path.join(TRACE_DIR, fname), "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    cls = rec.get("final_classification", "correct")
                    outcome_counts[cls] = outcome_counts.get(cls, 0) + 1
                    action = rec.get("action", "")
                    if action in side_counts:
                        side_counts[action] += 1
                    if cls in {"buy_early", "buy_validating"} and action in action_early:
                        action_early[action] += 1
                    if cls in {"sell_early", "sell_validating"} and action in action_validating:
                        action_validating[action] += 1
                    if rec.get("maturity_5m"):
                        mat_counts["maturity_5m"] += 1
                        if rec.get("win_5m"):
                            win_counts["win_5m"] += 1
                        if action in action_maturity:
                            action_maturity[action]["maturity_5m"] += 1
                            if rec.get("win_5m"):
                                action_maturity[action]["win_5m"] += 1
                    if rec.get("maturity_15m"):
                        mat_counts["maturity_15m"] += 1
                        if rec.get("win_15m"):
                            win_counts["win_15m"] += 1
                        if action in action_maturity:
                            action_maturity[action]["maturity_15m"] += 1
                            if rec.get("win_15m"):
                                action_maturity[action]["win_15m"] += 1
        except Exception:
            continue
    win_5m_rate = (win_counts["win_5m"] / mat_counts["maturity_5m"] * 100) if mat_counts["maturity_5m"] else 0.0
    win_15m_rate = (win_counts["win_15m"] / mat_counts["maturity_15m"] * 100) if mat_counts["maturity_15m"] else 0.0
    buy_early_cnt = outcome_counts.get("buy_early", 0)
    sell_early_cnt = outcome_counts.get("sell_early", 0)
    lines.append(f"正确={outcome_counts.get('correct', 0)} | 买早={buy_early_cnt} | 卖早={sell_early_cnt} | 买验证中={outcome_counts.get('buy_validating', 0)} | 卖验证中={outcome_counts.get('sell_validating', 0)} | 待定={outcome_counts.get('hold_pending', 0)}")
    lines.append(f"动作分布 BUY_LOW={side_counts.get('BUY_LOW', 0)} | ADD_POS={side_counts.get('ADD_POS', 0)} | SELL_HIGH={side_counts.get('SELL_HIGH', 0)} | PANIC_SELL={side_counts.get('PANIC_SELL', 0)}")
    lines.append(f"5分钟胜率={win_5m_rate:.1f}% ({win_counts['win_5m']}/{mat_counts['maturity_5m']}) | 15分钟胜率={win_15m_rate:.1f}% ({win_counts['win_15m']}/{mat_counts['maturity_15m']})")
    for action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
        total_action = side_counts.get(action, 0)
        if total_action or action_maturity[action]["maturity_5m"] or action_maturity[action]["maturity_15m"]:
            a5 = (action_maturity[action]["win_5m"] / action_maturity[action]["maturity_5m"] * 100) if action_maturity[action]["maturity_5m"] else 0.0
            a15 = (action_maturity[action]["win_15m"] / action_maturity[action]["maturity_15m"] * 100) if action_maturity[action]["maturity_15m"] else 0.0
            early_rate = (action_early[action] / total_action * 100) if total_action else 0.0
            validating_rate = (action_validating[action] / total_action * 100) if total_action else 0.0
            lines.append(f"{action}: 偏早={action_early[action]} | 验证中={action_validating[action]} | 偏早率={early_rate:.1f}% | 验证率={validating_rate:.1f}% | 5分钟胜率={a5:.1f}% | 15分钟胜率={a15:.1f}%")
    best_outcome = max(outcome_counts.items(), key=lambda x: x[1]) if outcome_counts else ("correct", 0)
    if sell_early_cnt >= buy_early_cnt + 2 and sell_early_cnt > 0:
        worst_outcome = (sell_early_cnt, "卖早")
    elif buy_early_cnt >= sell_early_cnt + 2 and buy_early_cnt > 0:
        worst_outcome = (buy_early_cnt, "买早")
    else:
        worst_outcome = (0, "无明显偏差")
    shadow_count = 0
    try:
        for fname in trace_files:
            with open(os.path.join(TRACE_DIR, fname), "r", encoding="utf-8") as f:
                for _ in f:
                    shadow_count += 1
    except Exception:
        pass
    lines.append(f"一眼摘要：最高频结果={best_outcome[0]}({best_outcome[1]}) | 主要风险={worst_outcome[1]}({worst_outcome[0]}) | 影子机会={shadow_count}")
    lines.append("═"*60)

    summary_text = "\n".join(lines)
    with open(os.path.join(LOG_DIR, f"ai_review_{today}.log"), "w", encoding="utf-8") as f: f.write(summary_text)
    
    # V1.11: 记录EOD复盘日志（用于后续分析最优做T时机）
    if _log_enhancer:
        for code, holding in HOLDINGS.items():
            dec = DAILY_DECISION_STATS.get(code, {})
            stats = AI_REVIEW_STATS.get(code, {})
            buy_signals = dec.get("buy_signals", [])
            sell_signals = dec.get("sell_signals", [])
            close_price = dec.get("close_price", dec.get("last_price", holding["cost"]))
            last_vwap = dec.get("last_vwap", close_price)
            day_ret = dec.get("day_ret", 0.0)
            _log_enhancer.log_eod_review(
                code=code, name=holding.get("name", code),
                high_price=stats.get("最高价格", close_price), low_price=stats.get("最低价格", close_price),
                close_price=close_price, vwap=last_vwap, day_ret=day_ret,
                best_sell_time=sell_signals[-1]["time"] if sell_signals else None,
                best_sell_price=sell_signals[-1]["price"] if sell_signals else None,
                best_buy_time=buy_signals[-1]["time"] if buy_signals else None,
                best_buy_price=buy_signals[-1]["price"] if buy_signals else None,
                signals_triggered=buy_signals + sell_signals,
                profit_potential=stats.get("最大振幅", 0.0)
            )

    learning_summary = {"buy_low": [], "buy_add": [], "sell_high": [], "panic_sell": [], "insufficient": []}
    starvation_state = load_starvation_state()
    starvation_updates: Dict[str, dict] = {}
    for code, holding in HOLDINGS.items():
        stats = AI_REVIEW_STATS.get(code, {})
        dec = DAILY_DECISION_STATS.get(code, {})
        buy_trig = int(stats.get("触发买入次数", 0))
        sell_trig = int(stats.get("触发卖出次数", 0))
        if buy_trig + sell_trig < 2:
            learning_summary["insufficient"].append(f"{holding.get('name', code)}({code}) 样本不足")
            continue
        if buy_trig == 0 and sell_trig > 0:
            record = starvation_state.get(code, {})
            days = int(record.get("days", 0)) + 1
            starvation_updates[code] = {
                "days": days,
                "last_date": today,
                "relax_until": record.get("relax_until", "")
            }
        else:
            if code in starvation_state:
                starvation_updates[code] = {"days": 0, "last_date": today, "relax_until": ""}
        buy_signals = dec.get("buy_signals", [])
        buy_low_signals = dec.get("buy_low_signals", [])
        buy_add_signals = dec.get("buy_add_signals", [])
        sell_signals = dec.get("sell_signals", [])
        sell_high_signals = dec.get("sell_high_signals", [])
        panic_sell_signals = dec.get("panic_sell_signals", [])
        close_price = float(dec.get("close_price", 0) or 0)
        last_vwap = float(dec.get("last_vwap", close_price) or close_price)
        default_qty = _default_trade_qty(holding)
        buy_qty = _sum_signal_qty(buy_signals, default_qty)
        buy_low_qty = _sum_signal_qty(buy_low_signals, default_qty)
        buy_add_qty = _sum_signal_qty(buy_add_signals, default_qty)
        sell_qty = _sum_signal_qty(sell_signals, default_qty)
        sell_high_qty = _sum_signal_qty(sell_high_signals, default_qty)
        panic_sell_qty = _sum_signal_qty(panic_sell_signals, default_qty)
        buy_weight = _qty_weight(buy_qty, default_qty) if buy_signals else 0.0
        sell_weight = _qty_weight(sell_qty, default_qty) if sell_signals else 0.0
        buy_early_w = buy_weight if buy_signals and close_price > last_vwap else 0.0
        sell_early_w = sell_weight if sell_signals and close_price < last_vwap else 0.0
        missed_buy_w = buy_weight if buy_signals and close_price < last_vwap else 0.0
        missed_sell_w = sell_weight if sell_signals and close_price > last_vwap else 0.0
        buy_adj = _clamp(round((sell_early_w + missed_buy_w) - (buy_early_w + missed_sell_w)), -2, 2)
        sell_adj = _clamp(round((buy_early_w + missed_buy_w) - (sell_early_w + missed_sell_w)), -3, 3)
        buy_low_adj = _clamp(round((missed_buy_w * 2) - buy_early_w), -3, 3)
        buy_factor_adj = 0
        sell_factor_adj = 0
        buy_seconds_adj = 0
        sell_seconds_adj = 0
        if buy_early_w and buy_trig:
            buy_factor_adj += min(2, max(1, round(buy_early_w)))
            buy_seconds_adj += int(30 * buy_early_w)
        if missed_buy_w and buy_trig:
            buy_factor_adj += min(2, max(1, round(missed_buy_w)))
            buy_seconds_adj += int(15 * missed_buy_w)
        if sell_early_w and sell_trig:
            sell_factor_adj += min(2, max(1, round(sell_early_w)))
            sell_seconds_adj += int(30 * sell_early_w)
        if missed_sell_w and sell_trig:
            sell_factor_adj += min(2, max(1, round(missed_sell_w)))
            sell_seconds_adj += int(30 * missed_sell_w)
        base_memory = _strategy_memory_for_code(code)
        new_memory = {
            "buy_threshold_adj": _clamp(int(base_memory.get("buy_threshold_adj", 0)) + buy_adj, -3, 3),
            "sell_threshold_adj": _clamp(int(base_memory.get("sell_threshold_adj", 0)) + sell_adj, -3, 3),
            "buy_low_threshold_adj": _clamp(int(base_memory.get("buy_low_threshold_adj", 0)) + buy_low_adj, -3, 3),
            "buy_confirm_min_score": _clamp(int(base_memory.get("buy_confirm_min_score", PARAMS["buy_confirm_min_score"])) + buy_adj, 42, 58),
            "buy_confirm_min_factors": _clamp(int(base_memory.get("buy_confirm_min_factors", PARAMS["buy_confirm_min_factors"])) + buy_factor_adj, 3, 7),
            "buy_confirm_min_seconds": _clamp(int(base_memory.get("buy_confirm_min_seconds", PARAMS["buy_confirm_min_seconds"])) + buy_seconds_adj, 0, 180),
            "buy_rebound_min_score_gap": _clamp(int(base_memory.get("buy_rebound_min_score_gap", PARAMS["buy_rebound_min_score_gap"])) + max(0, buy_factor_adj - 1), 6, 14),
            "sell_confirm_min_factors": _clamp(int(base_memory.get("sell_confirm_min_factors", PARAMS["sell_confirm_min_factors"])) + sell_factor_adj, 5, 8),
            "sell_confirm_min_seconds": _clamp(int(base_memory.get("sell_confirm_min_seconds", PARAMS["sell_confirm_min_seconds"])) + sell_seconds_adj, 45, 180),
            "sell_needs_momentum": True,
            "sell_needs_ema": True,
            "sell_needs_volume": True,
            "buy_needs_momentum": True,
            "buy_needs_ema": True,
            "buy_needs_volume": True,
            "buy_min_time": base_memory.get("buy_min_time", "09:40")
        }
        _merge_memory(code, new_memory)
        if buy_adj != 0:
            if buy_low_signals:
                learning_summary["buy_low"].append(f"{holding.get('name', code)}({code}) 低吸调{buy_adj:+.0f} | 权重{buy_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{buy_low_qty} | 偏早权重{buy_early_w:.2f} | 未占优权重{missed_buy_w:.2f}")
            if buy_add_signals:
                learning_summary["buy_add"].append(f"{holding.get('name', code)}({code}) 加仓调{buy_adj:+.0f} | 权重{buy_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{buy_add_qty} | 偏早权重{buy_early_w:.2f} | 未占优权重{missed_buy_w:.2f}")
        if sell_adj != 0:
            if sell_high_signals:
                learning_summary["sell_high"].append(f"{holding.get('name', code)}({code}) 高抛收紧{sell_adj:+.0f} | 权重{sell_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{sell_high_qty} | 偏早权重{sell_early_w:.2f} | 未占优权重{missed_sell_w:.2f}")
            if panic_sell_signals:
                learning_summary["panic_sell"].append(f"{holding.get('name', code)}({code}) 跳水收紧{sell_adj:+.0f} | 权重{sell_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{panic_sell_qty} | 偏早权重{sell_early_w:.2f} | 未占优权重{missed_sell_w:.2f}")
    if learning_summary["buy_low"]:
        lines.append("═"*20 + " 【学习摘要-低吸修正】 " + "═"*20)
        lines.extend(learning_summary["buy_low"][:8])
    if learning_summary["buy_add"]:
        lines.append("═"*20 + " 【学习摘要-加仓修正】 " + "═"*20)
        lines.extend(learning_summary["buy_add"][:8])
    if learning_summary["sell_high"]:
        lines.append("═"*20 + " 【学习摘要-高抛修正】 " + "═"*20)
        lines.extend(learning_summary["sell_high"][:8])
    if learning_summary["panic_sell"]:
        lines.append("═"*20 + " 【学习摘要-跳水修正】 " + "═"*20)
        lines.extend(learning_summary["panic_sell"][:8])
    if learning_summary["insufficient"]:
        lines.append("═"*20 + " 【学习摘要-样本不足】 " + "═"*20)
        lines.extend(learning_summary["insufficient"][:8])
    if starvation_updates:
        for code, rec in starvation_updates.items():
            if rec.get("days", 0) >= PARAMS["buy_starvation_days"]:
                relax_until = (datetime.now() + timedelta(days=PARAMS["buy_starvation_relax_ttl_days"])).strftime("%Y-%m-%d")
                starvation_updates[code] = {"days": rec["days"], "last_date": today, "relax_until": relax_until}
                lines.append(f"饥饿保护 {HOLDINGS.get(code, {}).get('name', code)}({code}) 连续{rec['days']}日无买入，次日放松确认")
        save_starvation_state({**starvation_state, **starvation_updates})
    log.info(summary_text)
    if FEISHU_WEBHOOK:
        try:
            def _extract_block(start_markers: List[str], stop_markers: List[str], max_lines: int = 8) -> str:
                start_idx = None
                for marker in start_markers:
                    for idx, item in enumerate(lines):
                        if marker in item:
                            start_idx = idx + 1
                            break
                    if start_idx is not None:
                        break
                if start_idx is None:
                    return "暂无"
                stop_idx = len(lines)
                for marker in stop_markers:
                    for idx in range(start_idx, len(lines)):
                        if marker in lines[idx]:
                            stop_idx = min(stop_idx, idx)
                            break
                block = [item for item in lines[start_idx:stop_idx] if item.strip()]
                return "\n".join(block[:max_lines]) if block else "暂无"

            learning_sources = [
                ("低吸", learning_summary["buy_low"]),
                ("加仓", learning_summary["buy_add"]),
                ("高抛", learning_summary["sell_high"]),
                ("跳水", learning_summary["panic_sell"]),
                ("样本", learning_summary["insufficient"]),
            ]
            learning_lines = []
            for title, items in learning_sources:
                if not items:
                    learning_lines.append(f"{title}：暂无")
                    continue
                top_items = items[:3]
                learning_lines.append(f"{title} | {len(items)}项 | " + " ; ".join(top_items))
            learning_text = "\n".join(learning_lines)
            def _section(title: str, body: str) -> List[dict]:
                return [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{title}**\n{body.strip()}"
                        },
                    },
                    {"tag": "hr"},
                ]

            card_elements = [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**日报汇总 {today}**\n盘后摘要"
                    },
                },
                {"tag": "hr"},
            ]
            card_elements.extend(_section("日报·尾盘复盘", overview_text))
            card_elements.extend(_section("日报·分钟线异常", minute_text))
            card_elements.extend(_section("日报·学习摘要", learning_text))
            if card_elements and card_elements[-1].get("tag") == "hr":
                card_elements.pop()

            summary_payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "elements": card_elements,
                },
                "notify_type": 1,
            }
            send_feishu_payload(
                payload=summary_payload,
                success_log="✅ 尾盘复盘已推送飞书",
                error_prefix="尾盘复盘飞书推送",
                trigger_urgent_alarm_after_success=False,
            )
        except Exception as e:
            log.warning(f"⚠️  尾盘复盘飞书推送失败: {str(e)[:120]}")
    _eod_logged_date = today

# ==================== 交易主循环 ====================
_last_idle_log = datetime.min
_scan_count = 0

def build_preopen_context() -> PreOpenContext:
    holdings = load_holdings()
    watchlist = load_watchlist()
    engine = PreOpenEngine(holdings, watchlist)
    context = engine.evaluate()
    engine.persist(context)
    return context


def _preopen_action_label(context: PreOpenContext) -> str:
    if context.market_bias in {"risk_on", "neutral_to_positive"} and context.market_score >= 58:
        return "进攻"
    if context.market_bias == "risk_off" or context.market_score <= 45:
        return "回避"
    return "观察"


def _preopen_card_template(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    if action == "进攻" or context.market_bias == "risk_on" or context.market_score >= 58:
        return "green"
    if action == "回避" or context.market_bias == "risk_off" or context.market_score <= 45:
        return "red"
    return "blue"


def _feishu_card_header(title: str, template: str) -> dict:
    return {"template": template, "title": {"tag": "plain_text", "content": title}}


def _is_preopen_monitor_window(now: datetime) -> bool:
    return now.weekday() < 5 and dtime(9, 15) <= now.time() < dtime(9, 25)


def _preopen_monitor_signature(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    score_bucket = int(max(0.0, float(context.market_score or 0)) // 5) * 5
    hot_theme = _preopen_hot_theme_text(context, 3)
    active = ",".join(_sort_codes_by_holding_priority(context.active_codes)[:3])
    watch = ",".join(_sort_codes_by_holding_priority(context.watch_codes)[:3])
    focus = ",".join(_sort_codes_by_holding_priority(context.focus_codes)[:3])
    blocked = ",".join(_sort_codes_by_holding_priority(context.blocked_codes)[:3])
    adv = _preopen_adv_counts(context)
    up = int(adv.get("up", 0) or 0)
    down = int(adv.get("down", 0) or 0)
    flat = int(adv.get("flat", 0) or 0)
    return "|".join([
        _preopen_action_label(context),
        context.market_bias or "unknown",
        str(score_bucket),
        f"{up}/{down}/{flat}",
        hot_theme,
        active,
        watch,
        focus,
        blocked,
    ])


def _reset_preopen_monitor_state_if_needed(today: str) -> None:
    global _preopen_monitor_date, _preopen_monitor_last_push_at, _preopen_monitor_last_signature, _preopen_monitor_push_count
    if _preopen_monitor_date == today:
        return
    _preopen_monitor_date = today
    _preopen_monitor_last_push_at = None
    _preopen_monitor_last_signature = None
    _preopen_monitor_push_count = 0


def _should_push_preopen_monitor(context: PreOpenContext, now: datetime) -> bool:
    global _preopen_monitor_last_push_at, _preopen_monitor_last_signature, _preopen_monitor_push_count
    if not FEISHU_WEBHOOK or not _is_preopen_monitor_window(now):
        return False
    today = now.strftime("%Y-%m-%d")
    _reset_preopen_monitor_state_if_needed(today)
    if _preopen_monitor_push_count >= 5:
        return False
    signature = _preopen_monitor_signature(context)
    if _preopen_overview_last_push_at is not None and _preopen_monitor_push_count == 0:
        if (now - _preopen_overview_last_push_at).total_seconds() < 60:
            return False
    if _preopen_monitor_last_push_at is None:
        return True
    elapsed = (now - _preopen_monitor_last_push_at).total_seconds()
    if signature != _preopen_monitor_last_signature and elapsed >= 60:
        return True
    if elapsed >= 120:
        return True
    return False


def _format_code_names(codes: List[str], limit: int = 4) -> str:
    names = []
    for code in codes[:limit]:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        names.append(f"{holding.get('name', code)}({code})")
    return "、".join(names) if names else "暂无"


def _preopen_strategy_line(context: PreOpenContext) -> str:
    return (
        f"策略结论：主做{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 2)} | "
        f"观察{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 2)} | "
        f"关注{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 2)} | "
        f"回避{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 2)}"
    )


def _rank_focus_codes(codes: List[str]) -> List[str]:
    scored = []
    for code in codes:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        qty = int(holding.get("qty", 0) or 0)
        cost = float(holding.get("cost", 0) or 0)
        score = qty + (cost * 0.1)
        scored.append((score, code))
    scored.sort(reverse=True)
    return [code for _, code in scored]


def _preopen_focus_text(context: PreOpenContext) -> str:
    ranked = _rank_focus_codes(context.watch_codes or context.focus_codes)
    return _format_code_names(ranked, 4)


def _sort_codes_by_holding_priority(codes: List[str]) -> List[str]:
    scored = []
    for code in codes:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        qty = int(holding.get("qty", 0) or 0)
        cost = float(holding.get("cost", 0) or 0)
        score = qty * 10 + cost
        scored.append((score, code))
    scored.sort(reverse=True)
    return [code for _, code in scored]


def _preopen_followup_text(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    if action == "进攻":
        return "开盘后 5 分钟看强主题承接，15 分钟看是否能站稳分时均线。"
    if action == "观察":
        return "开盘后先看分化，优先等 5~15 分钟确认强弱，再决定是否进。"
    return "开盘后只看核心标的承接，不追高，等量能和情绪同时转强再说。"


def _preopen_turn_strong_rule(context: PreOpenContext) -> str:
    if context.market_score < 50:
        return "若 9:35 后上涨家数回到高于下跌家数，且市场评分回到 55 上方，再把关注升为观察。"
    if context.market_score < 58:
        return "若 9:35 后主题集中度继续抬升，且重点标的出现放量承接，再把观察升为主做。"
    return "若开盘后 5 分钟强主题延续、量能不掉，再优先跟主做组。"


def _special_low_buy_plan(code: str) -> str:
    if code == "688102":
        return "斯瑞新材：优先回踩 VWAP/短均线后低吸，等止跌不再创新低再上。"
    if code == "601698":
        return "中国卫通：必须先止跌确认再低吸，确认站稳 VWAP 后再考虑介入。"
    return ""


def _special_low_buy_stage_rule(code: str, stage: str) -> str:
    stage = str(stage or "").strip()
    if code == "688102":
        if stage == "open":
            return "开盘看能否守住 VWAP 附近，不追第一波拉升。"
        if stage == "intraday":
            return "盘中等回踩不破、分时不创新低后再低吸。"
        if stage == "eod":
            return "尾盘若仍在 VWAP 附近反复且不破位，可小仓观察。"
    if code == "601698":
        if stage == "open":
            return "开盘先看是否止跌，不抢反弹。"
        if stage == "intraday":
            return "盘中必须重新站稳 VWAP 附近，再考虑低吸。"
        if stage == "eod":
            return "尾盘只有确认止跌并靠近均价时才考虑。"
    return ""


def _special_loss_reduction_rule(code: str) -> str:
    if code == "300364":
        return "中文在线：优先等反弹减亏，不追弱反弹；只有重新站稳 VWAP 且分时转强才允许少量加仓。"
    if code == "002639":
        return "雪人集团：优先利用反弹减亏，弱势不补仓；只有放量站回 VWAP 并确认止跌后才允许低吸。"
    return ""


def _special_loss_reduction_stage_rule(code: str, stage: str) -> str:
    stage = str(stage or "").strip()
    if code == "300364":
        if stage == "open":
            return "开盘先看是否高开回落，优先等反弹减亏，不追开盘脉冲。"
        if stage == "intraday":
            return "盘中只在重新站稳 VWAP、分时转强时才考虑减亏或小补。"
        if stage == "eod":
            return "尾盘若仍弱于 VWAP，优先保留减亏思路，不做被动摊平。"
    if code == "002639":
        if stage == "open":
            return "开盘先看承接，弱势不抢反弹，先等减亏窗口。"
        if stage == "intraday":
            return "盘中只有放量站回 VWAP 且止跌确认，才允许小仓修复。"
        if stage == "eod":
            return "尾盘若未收复均价，优先减亏思路，避免继续扩大浮亏。"
    return ""


def _special_loss_threshold_adjustments(code: str, action: str, buy_threshold: int, sell_threshold: int, buy_score: float, sell_score: float, price: float, vwap: float, is_strong_pullback: bool) -> tuple[int, int, float, float]:
    if code == "300364":
        if action in {"BUY_LOW", "ADD_POS"}:
            buy_threshold += 4
            if not is_strong_pullback:
                buy_threshold += 2
            buy_score -= 2
        if action in {"SELL_HIGH", "PANIC_SELL"} or (vwap and price > vwap * 1.002):
            sell_threshold = max(35, sell_threshold - 2)
            sell_score += 3
    elif code == "002639":
        if action in {"BUY_LOW", "ADD_POS"}:
            buy_threshold += 5
            if not is_strong_pullback:
                buy_threshold += 2
            buy_score -= 3
        if action in {"SELL_HIGH", "PANIC_SELL"} or (vwap and price > vwap * 1.0015):
            sell_threshold = max(35, sell_threshold - 3)
            sell_score += 4
    return buy_threshold, sell_threshold, buy_score, sell_score


def _format_preopen_brief(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    market_snapshot = context.market_snapshot if isinstance(context.market_snapshot, dict) else {}
    hot_theme = "、".join(market_snapshot.get("hot_theme", [])[:3]) or breadth.get("hot_theme_text", "") or "暂无"
    auction_summary = breadth.get("auction_summary", {}) if isinstance(breadth, dict) else {}
    auction_text = ""
    if isinstance(auction_summary, dict) and auction_summary:
        auction_text = (
            f"竞价：强{auction_summary.get('strong_open_count', 0)} / 弱{auction_summary.get('weak_open_count', 0)} / 缺{auction_summary.get('missing_open_count', 0)} | "
            f"均分 {float(auction_summary.get('mean_score', 0) or 0):.1f}"
        )
    return (
        f"早盘集合竞价结论：{_preopen_action_label(context)}\n"
        f"市场评分：{context.market_score:.1f} / 100 | 偏向：{context.market_bias} | 风险：{breadth.get('risk_flag', 'unknown')}\n"
        f"{auction_text + chr(10) if auction_text else ''}"
        f"{_preopen_strategy_line(context)}\n"
        f"1. 主做：{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 3)}\n"
        f"2. 观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 3)}\n"
        f"3. 关注：{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 3)}\n"
        f"4. 回避：{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 3)}\n"
        f"执行顺序：先1后2，再3，最后4\n"
        f"升级条件：{_preopen_turn_strong_rule(context)}\n"
        f"开盘后跟踪：{_preopen_followup_text(context)}\n"
        f"热门关注：{hot_theme}"
    )


def _record_preopen_trace(context: PreOpenContext) -> None:
    try:
        _append_jsonl(_trace_path("preopen_trace"), context.__dict__)
    except Exception:
        pass



def _feishu_md_div(content: str) -> dict:
    return {"tag": "div", "text": {"content": content, "tag": "lark_md"}}



def _feishu_hr() -> dict:
    return {"tag": "hr"}



def _preopen_safe_breadth(context: PreOpenContext) -> Dict[str, Any]:
    return context.breadth if isinstance(context.breadth, dict) else {}



def _preopen_adv_counts(context: PreOpenContext) -> Dict[str, int]:
    adv = _preopen_safe_breadth(context).get("advance_decline", {})
    if not isinstance(adv, dict):
        return {"up": 0, "down": 0, "flat": 0}
    return {
        "up": int(adv.get("up", 0) or 0),
        "down": int(adv.get("down", 0) or 0),
        "flat": int(adv.get("flat", 0) or 0),
    }



def _preopen_adv_text(context: PreOpenContext) -> str:
    adv = _preopen_adv_counts(context)
    return f"涨{adv['up']} / 跌{adv['down']} / 平{adv['flat']}"



def _preopen_hot_theme_text(context: PreOpenContext, limit: int = 3) -> str:
    snapshot_hot = context.market_snapshot.get("hot_theme", []) if isinstance(context.market_snapshot, dict) else []
    if isinstance(snapshot_hot, list) and snapshot_hot:
        return "、".join([str(x) for x in snapshot_hot[:limit] if str(x).strip()]) or "暂无"
    breadth_hot = _preopen_safe_breadth(context).get("hot_theme_text", "")
    if breadth_hot:
        return str(breadth_hot)
    return "暂无"



def _preopen_action_hint(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    return {
        "进攻": "优先看强主题中的回踩确认，不追弱票。",
        "观察": "只盯重点标的，等开盘后强弱分化再动。",
        "回避": "优先控仓，弱市不追价，只看核心标的是否有承接。",
    }.get(action, "按信号择机")



def _preopen_group_line(label: str, codes: List[str], limit: int = 4) -> str:
    return f"**{label}**：{_format_code_names(_sort_codes_by_holding_priority(codes), limit)}"



def _preopen_theme_lines(context: PreOpenContext, limit: int = 5, code_limit: int = 4) -> List[str]:
    lines = []
    for item in context.theme_rank[:limit]:
        sector = item.get("sector", "")
        count = item.get("count", 0)
        codes = item.get("codes", [])[:code_limit]
        names = []
        for code in codes:
            h = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
            names.append(f"{h.get('name', code)}({code})")
        lines.append(f"- {sector}：{count} 只 | {'、'.join(names) if names else '暂无'}")
    if not lines:
        lines.append("- 暂无主题聚合数据")
    return lines



def _build_preopen_card_payload(title: str, elements: List[dict], at_all: bool, at_text: str, template: Optional[str] = None) -> dict:
    card_elements = []
    if at_all and at_text:
        card_elements.append(_feishu_md_div(at_text))
    card_elements.append(_feishu_md_div(title))
    card_elements.extend(elements)
    card = {"config": {"wide_screen_mode": True}, "elements": card_elements}
    if template:
        card["header"] = _feishu_card_header(title, template)
    return {
        "msg_type": "interactive",
        "card": card,
        "notify_type": 1,
    }



def _build_preopen_summary_elements(context: PreOpenContext) -> List[dict]:
    breadth = _preopen_safe_breadth(context)
    elements = [
        _feishu_md_div(
            f"**集合竞价总览**\n"
            f"动作建议：{_preopen_action_label(context)} | 评分 {context.market_score:.1f} | 偏向 {context.market_bias} | 风险 {breadth.get('risk_flag', 'unknown')}\n"
            f"盘面判断：{context.session_note}\n"
            f"竞价摘要：强{breadth.get('auction_summary', {}).get('strong_open_count', 0)} / 弱{breadth.get('auction_summary', {}).get('weak_open_count', 0)} / 缺{breadth.get('auction_summary', {}).get('missing_open_count', 0)}\n"
            f"开盘：{_special_low_buy_stage_rule('688102', 'open')} | {_special_low_buy_stage_rule('601698', 'open')}\n"
            f"盘中：{_special_low_buy_stage_rule('688102', 'intraday')} | {_special_low_buy_stage_rule('601698', 'intraday')}\n"
            f"尾盘：{_special_low_buy_stage_rule('688102', 'eod')} | {_special_low_buy_stage_rule('601698', 'eod')}\n"
            f"数量：按剩余资金缩放，优先小仓试错"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**看板分组**\n"
            f"{_preopen_group_line('主做', context.active_codes, 2)}\n"
            f"{_preopen_group_line('观察', context.watch_codes, 2)}\n"
            f"{_preopen_group_line('关注', context.focus_codes, 2)}\n"
            f"{_preopen_group_line('回避', context.blocked_codes, 2)}\n"
            f"**快照** 涨跌 {_preopen_adv_text(context)} | 热点 {_preopen_hot_theme_text(context)} | 集中度 {float(breadth.get('top_theme_share', 0) or 0):.2%}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**开盘执行**\n"
            f"先1后2，再3，最后4\n"
            f"跟踪：{_preopen_followup_text(context)}\n"
            f"转强：{_preopen_turn_strong_rule(context)}"
        ),
    ]
    return elements



def _build_preopen_detail_elements(context: PreOpenContext) -> List[dict]:
    breadth = _preopen_safe_breadth(context)
    elements = [
        _feishu_md_div(
            f"**集合竞价详细结果**\n"
            f"动作建议：{_preopen_action_label(context)} | {_preopen_action_hint(context)}\n"
            f"时间：{context.generated_at} | 数据源：{context.source}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**市场状态**\n"
            f"评分 {context.market_score:.1f} | 偏向 {context.market_bias} | 风险 {breadth.get('risk_flag', 'unknown')} | 开盘 {'是' if breadth.get('market_open', False) else '否'}\n"
            f"盘面：{context.session_note}\n"
            f"快照：{_preopen_adv_text(context)} | 热点：{_preopen_hot_theme_text(context)}\n"
            f"竞价：强{breadth.get('auction_summary', {}).get('strong_open_count', 0)} / 弱{breadth.get('auction_summary', {}).get('weak_open_count', 0)} / 缺{breadth.get('auction_summary', {}).get('missing_open_count', 0)} | 均分 {float(breadth.get('auction_summary', {}).get('mean_score', 0) or 0):.1f}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**标的结构**\n"
            f"覆盖 {breadth.get('total_codes', 0)} | ETF {breadth.get('etf_count', 0)} | 个股 {breadth.get('stock_count', 0)} | 主题 {breadth.get('theme_count', 0)} | 集中度 {float(breadth.get('top_theme_share', 0) or 0):.2%}\n"
            f"强：{'、'.join(context.favored_sectors[:2]) if context.favored_sectors else '暂无'} | 弱：{'、'.join(context.weak_sectors[:2]) if context.weak_sectors else '暂无'}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**看板池**\n"
            f"主做：{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 4)}\n"
            f"观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 4)}\n"
            f"关注：{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 4)}\n"
            f"回避：{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 4)}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**主题聚合 Top5**\n" + "\n".join(_preopen_theme_lines(context, 3, 3))
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**开盘跟踪**\n"
            f"顺序：1->2->3->4 | 跟踪：{_preopen_followup_text(context)}\n"
            f"升级：{_preopen_turn_strong_rule(context)}"
        ),
    ]
    return elements


def _preopen_message_text(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    adv = _preopen_adv_counts(context)
    hot_theme = breadth.get("hot_theme_text", "")
    active_names = _format_code_names(context.active_codes, 6)
    focus_names = _format_code_names(context.focus_codes, 6)
    blocked_names = _format_code_names(context.blocked_codes, 6)
    theme_lines = []
    for item in context.theme_rank[:5]:
        sector = item.get("sector", "")
        count = item.get("count", 0)
        codes = item.get("codes", [])[:4]
        names = []
        for code in codes:
            h = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
            names.append(f"{h.get('name', code)}({code})")
        theme_lines.append(f"- {sector}：{count} 只 | {'、'.join(names) if names else '暂无'}")
    if not theme_lines:
        theme_lines.append("- 暂无主题聚合数据")
    action = _preopen_action_label(context)
    action_hint = {
        "进攻": "优先看强主题中的回踩确认，不追弱票。",
        "观察": "只盯重点标的，等开盘后强弱分化再动。",
        "回避": "优先控仓，弱市不追价，只看核心标的是否有承接。",
    }.get(action, "按信号择机")
    return (
        f"【集合竞价详细结果】\n"
        f"动作建议：{action} | {action_hint}\n"
        f"时间：{context.generated_at}\n"
        f"市场评分：{context.market_score:.1f} / 100\n"
        f"市场偏向：{context.market_bias}\n"
        f"盘面判断：{context.session_note}\n"
        f"市场快照：涨{adv['up']} / 跌{adv['down']} / 平{adv['flat']} | 热主题：{hot_theme or '暂无'}\n"
        f"覆盖标的：{breadth.get('total_codes', 0)} | ETF {breadth.get('etf_count', 0)} | 个股 {breadth.get('stock_count', 0)}\n"
        f"主题集中度：{breadth.get('top_theme_share', 0):.2%} | 风险标记：{breadth.get('risk_flag', 'unknown')}\n"
        f"偏强主题：{'、'.join(context.favored_sectors[:3]) if context.favored_sectors else '暂无'}\n"
        f"偏弱主题：{'、'.join(context.weak_sectors[:3]) if context.weak_sectors else '暂无'}\n"
        f"1. 主做：{active_names}\n"
        f"2. 观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 6)}\n"
        f"3. 关注：{focus_names}\n"
        f"4. 回避：{blocked_names}\n"
        f"执行顺序：先1后2，再3，最后4\n"
        f"主题聚合：\n" + "\n".join(theme_lines)
    )


def _preopen_summary_text(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    action = _preopen_action_label(context)
    return (
        f"【集合竞价总览】\n"
        f"动作建议：{action}\n"
        f"{_preopen_strategy_line(context)}\n"
        f"1. 主做：{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 4)}\n"
        f"2. 观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 4)}\n"
        f"3. 关注：{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 4)}\n"
        f"4. 回避：{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 4)}\n"
        f"开盘后跟踪：{_preopen_followup_text(context)}\n"
        f"转强条件：{_preopen_turn_strong_rule(context)}\n"
        f"覆盖标的：{breadth.get('total_codes', 0)} | 主题集中度：{breadth.get('top_theme_share', 0):.2%} | 风险标记：{breadth.get('risk_flag', 'unknown')}"
    )


def _send_preopen_feishu(context: PreOpenContext) -> bool:
    global _preopen_pushed_date, _preopen_overview_last_push_at
    today = get_today_str()
    if _preopen_pushed_date == today or not FEISHU_WEBHOOK:
        return False
    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    use_strong = feishu_cfg.get("use_strong_notification", True)
    relay_urgent_alarm = feishu_cfg.get("relay_urgent_alarm_on_feishu", True)
    at_all = feishu_cfg.get("at_all_on_signal", True)
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    template = _preopen_card_template(context)

    summary_title = f"🚨 集合竞价总览 - {FEISHU_KEYWORD}" if use_strong else f"📢 集合竞价总览 - {FEISHU_KEYWORD}"
    summary_payload = _build_preopen_card_payload(
        summary_title,
        _build_preopen_summary_elements(context),
        at_all,
        at_text,
        template=template,
    )
    ok = send_feishu_payload(
        payload=summary_payload,
        success_log="✅ 集合竞价总览已推送飞书",
        error_prefix="集合竞价总览飞书推送",
        trigger_urgent_alarm_after_success=False,
    )

    detail_title = f"🚨 集合竞价详细结果 - {FEISHU_KEYWORD}" if use_strong else f"📢 集合竞价详细结果 - {FEISHU_KEYWORD}"
    detail_payload = _build_preopen_card_payload(
        detail_title,
        _build_preopen_detail_elements(context),
        at_all,
        at_text,
        template=template,
    )
    detail_ok = send_feishu_payload(
        payload=detail_payload,
        success_log="✅ 集合竞价详细结果已推送飞书",
        error_prefix="集合竞价详细飞书推送",
        trigger_urgent_alarm_after_success=use_strong and relay_urgent_alarm,
    )
    if ok or detail_ok:
        _preopen_pushed_date = today
        _preopen_overview_last_push_at = _now()
    return ok and detail_ok


def _send_preopen_monitor_feishu(context: PreOpenContext, now: Optional[datetime] = None) -> bool:
    global _preopen_monitor_last_push_at, _preopen_monitor_last_signature, _preopen_monitor_push_count
    now = now or _now()
    if not _should_push_preopen_monitor(context, now):
        return False
    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    at_all = bool(feishu_cfg.get("at_all_on_preopen_monitor", False))
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    title = f"📊 集合竞价监控 - {FEISHU_KEYWORD}"
    payload = _build_preopen_card_payload(
        title,
        _build_preopen_monitor_elements(context, now),
        at_all,
        at_text,
        template=_preopen_card_template(context),
    )
    ok = send_feishu_payload(
        payload=payload,
        success_log="✅ 集合竞价监控已推送飞书",
        error_prefix="集合竞价监控飞书推送",
        trigger_urgent_alarm_after_success=False,
    )
    if ok:
        _preopen_monitor_last_push_at = now
        _preopen_monitor_last_signature = _preopen_monitor_signature(context)
        _preopen_monitor_push_count += 1
    return ok


def _build_preopen_monitor_elements(context: PreOpenContext, now: datetime) -> List[dict]:
    breadth = _preopen_safe_breadth(context)
    active = _format_code_names(_sort_codes_by_holding_priority(context.active_codes), 3)
    watch = _format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 3)
    focus = _format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 3)
    blocked = _format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 3)
    return [
        _feishu_md_div(
            f"**集合竞价监控**\n"
            f"时间：{now.strftime('%H:%M:%S')} | 动作：{_preopen_action_label(context)} | 评分 {context.market_score:.1f} | 偏向 {context.market_bias} | 风险 {breadth.get('risk_flag', 'unknown')}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**盘面快照**\n"
            f"涨跌 {_preopen_adv_text(context)} | 热点 {_preopen_hot_theme_text(context)} | 集中度 {float(breadth.get('top_theme_share', 0) or 0):.2%}\n"
            f"主做：{active}\n"
            f"观察：{watch}\n"
            f"关注：{focus}\n"
            f"回避：{blocked}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**开盘后跟踪**\n"
            f"顺序：1->2->3->4\n"
            f"跟踪：{_preopen_followup_text(context)}\n"
            f"转强：{_preopen_turn_strong_rule(context)}\n"
            f"开盘：{_special_low_buy_stage_rule('688102', 'open')} | {_special_low_buy_stage_rule('601698', 'open')}\n"
            f"盘中：{_special_low_buy_stage_rule('688102', 'intraday')} | {_special_low_buy_stage_rule('601698', 'intraday')}\n"
            f"尾盘：{_special_low_buy_stage_rule('688102', 'eod')} | {_special_low_buy_stage_rule('601698', 'eod')}\n"
            f"数量：按剩余资金缩放，优先小仓试错"
        ),
    ]


def _ensure_preopen_context(force: bool = False) -> Optional[PreOpenContext]:
    global PREOPEN_CONTEXT, SESSION_CONTEXT, _preopen_logged_date
    today = get_today_str()
    if not force and PREOPEN_CONTEXT is not None and _preopen_logged_date == today:
        return PREOPEN_CONTEXT
    try:
        PREOPEN_CONTEXT = build_preopen_context()
        SESSION_CONTEXT = {
            "date": today,
            "market_score": PREOPEN_CONTEXT.market_score,
            "market_bias": PREOPEN_CONTEXT.market_bias,
            "session_note": PREOPEN_CONTEXT.session_note,
            "favored_sectors": PREOPEN_CONTEXT.favored_sectors,
            "weak_sectors": PREOPEN_CONTEXT.weak_sectors,
            "focus_codes": PREOPEN_CONTEXT.focus_codes,
        }
        _preopen_logged_date = today
        _record_preopen_trace(PREOPEN_CONTEXT)
        log.info(_format_preopen_brief(PREOPEN_CONTEXT))
        if force or _preopen_pushed_date != today:
            _send_preopen_feishu(PREOPEN_CONTEXT)
        return PREOPEN_CONTEXT
    except Exception as e:
        log.warning(f"⚠️  早盘解读生成失败: {str(e)[:120]}")
        return PREOPEN_CONTEXT


def scan_once():
    global _last_idle_log, _scan_count, _scan_lock
    if _scan_lock:
        log.warning("⚠️ 上一轮扫描仍在进行，跳过本轮触发")
        return

    _scan_lock = True
    try:
        now = _now()
        t = now.time()

        if _is_preopen_monitor_window(now):
            preopen_context = _ensure_preopen_context(force=True)
            if preopen_context is not None:
                _send_preopen_monitor_feishu(preopen_context, now=now)
            if (_now() - _last_idle_log).total_seconds() >= 120:
                log.info("📡 盘前集合竞价监控已刷新")
                _last_idle_log = _now()

        if dtime(14, 55) <= t <= dtime(15, 5): log_eod_summary()

        if now.weekday() >= 5 or t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
            if (_now() - _last_idle_log).total_seconds() >= PARAMS["idle_log_minutes"] * 60:
                log.info("⏸ 非交易时段，进入低频保活")
                _last_idle_log = _now()
            return

        log.info(f"🫀 扫描心跳 第{_scan_count + 1}轮开始")

        if not HOLDINGS:
            return
        preopen_context = _ensure_preopen_context(force=False)
        _scan_count += 1
        panel_rows = []

        for code, holding in HOLDINGS.items():
            _ensure_ai_review_stats(code, holding)
            dec = _ensure_daily_decision_stats(code, holding)

            try:
                time.sleep(0.5)
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
                    panel_rows.append([label(code, holding), "-", "-", "-", "-", f"分钟线断流({minute_label})"])
                    bucket = _minute_issue_bucket(minute_status)
                    minute_issue_stats.setdefault(bucket, {})
                    minute_issue_stats[bucket][minute_label] = minute_issue_stats[bucket].get(minute_label, 0) + 1
                    log.warning(f"⚠️  {label(code, holding)} 分钟线为空 [{minute_label}]")
                    continue
                if minute_status not in {"ok", "cache_hit"}:
                    dec["last_status"] = f"分钟线异常({minute_label})"
                    dec["last_status_detail"] = minute_detail
                    panel_rows.append([label(code, holding), "-", "-", "-", "-", f"分钟线异常({minute_label})"])
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
                price = float(df.iloc[-1]["close"]) if "close" in df.columns else 0.0
                vwap = float(df.iloc[-1]["vwap"]) if "vwap" in df.columns else price
                amp = float(df.iloc[-1]["day_amplitude"]) if "day_amplitude" in df.columns else 0.0

                dec["last_price"] = price
                dec["last_vwap"] = vwap
                dec["close_price"] = price
                dec["last_amp"] = amp
                if preopen_context is not None:
                    dec["preopen_market_score"] = preopen_context.market_score
                    dec["preopen_market_bias"] = preopen_context.market_bias
                    dec["preopen_note"] = preopen_context.session_note

                if len(df) < 2:
                    dec["last_status"] = "数据预热"
                    panel_rows.append([label(code, holding), f"{price:.2f}", f"{vwap:.2f}", f"{amp*100:.1f}%", "-", "数据预热"])
                    continue

                can_t = holding.get("t_qty", 0) > 0
                daily_ctx = get_daily_context(code, holding, current_price=price)
                dec["daily_status"] = daily_ctx.get("daily_status", "unknown")
                dec["last_daily_gate"] = daily_ctx.get("daily_gate", "neutral")
                dec["last_daily_trend_bg"] = daily_ctx.get("daily_trend_bg", "unknown")
                dec["last_daily_support"] = daily_ctx.get("daily_support_name", "")
                dec["last_daily_support_gap"] = daily_ctx.get("daily_support_gap", 0.0)
                dec["last_daily_overheated"] = daily_ctx.get("daily_overheated", False)
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
                if dec.get("last_stand_down_reason"):
                    stat = f"停手:{dec.get('last_stand_down_reason')}"
                elif engine.cycle_count.get(code, 0) >= PARAMS["max_t_cycles_per_stock"]:
                    stat = "停手:当日轮次已满"
                elif amp < PARAMS['min_amplitude']:
                    stat = "无波待涨"
                elif not can_t:
                    stat = "底仓"
                elif best_score >= 45:
                    stat = "强可T"
                elif best_score >= 25:
                    stat = "可T观察"
                else:
                    stat = "弱机会"
                if sig and sig.action in {"BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"}:
                    stat = f"{stat}|{sig.action}"
                dec["last_status"] = stat
                panel_rows.append([label(code, holding), f"{price:.2f}", f"{vwap:.2f}", f"{amp*100:.1f}%", f"多{buy_score}/空{sell_score}", stat])

                _snapshot_write(code, holding, df, {
                    "price": price,
                    "vwap": vwap,
                    "market_state": sig.indicators.get("market_state", dec.get("last_market_state", "unknown")) if sig else dec.get("last_market_state", "unknown"),
                    "benchmark_code": dec.get("last_benchmark_code", ""),
                    "benchmark_name": dec.get("last_benchmark_name", ""),
                    "benchmark_state": dec.get("last_benchmark_state", "unknown"),
                    "benchmark_gate": dec.get("last_benchmark_gate", "neutral"),
                    "benchmark_reason": dec.get("last_benchmark_reason", ""),
                    "preopen_market_score": dec.get("preopen_market_score", 0),
                    "preopen_market_bias": dec.get("preopen_market_bias", "unknown"),
                    "preopen_note": dec.get("preopen_note", ""),
                }, {
                    "action": sig.action,
                    "score": sig.score,
                    "reasons": sig.reasons,
                    "entry_kind": sig.factors.get("entry_kind", "") if sig else "",
                } if sig else None, daily_context=daily_ctx)

                # 【集合竞价驱动信号检测】
                auction_sig = check_auction_driven_signal(code, holding, df, {
                    "price": price,
                    "vwap": vwap,
                    "range_pos": float(df.iloc[-1].get("range_pos", 0.5)) if pd.notna(df.iloc[-1].get("range_pos")) else 0.5,
                })
                if auction_sig:
                    send_auction_alert(auction_sig, holding)

                if sig and can_t:
                    notify(sig, holding)
                    engine.record_signal(code, sig.action, sig.price, sig.score)
                    engine.record_trade_action(code, sig.action, sig.hold_qty)
                    if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                        engine.cycle_count[code] = engine.cycle_count.get(code, 0) + 1

            except Exception as e:
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

        if _scan_count % 4 == 1:
            lines = [f"\n📊 护城河防御面板 第{_scan_count}轮\n" + "─"*70]
            for r in panel_rows:
                lines.append(f"{r[0]:<16}{r[1]:>8}{r[2]:>10}{r[3]:>8} {r[4]:>10}  {r[5]:<8}")
            log.info("\n".join(lines))
    finally:
        _scan_lock = False

def replay_today():
    today = get_today_str()
    snapshot_files = []
    snapshot_days = set()
    for root, _, files in os.walk(SNAPSHOT_DIR):
        for name in files:
            if not name.endswith(".json") or "_" not in name:
                continue
            day_part = name.rsplit("_", 1)[-1].removesuffix(".json")
            snapshot_days.add(day_part)
            if day_part == today:
                snapshot_files.append(os.path.join(root, name))
    if not snapshot_files:
        if not snapshot_days:
            log.info(f"未找到当日快照: {today}")
            return
        today = sorted(snapshot_days)[-1]
        snapshot_files = []
        for root, _, files in os.walk(SNAPSHOT_DIR):
            for name in files:
                if name.endswith(f"_{today}.json"):
                    snapshot_files.append(os.path.join(root, name))
        log.info(f"未找到今日快照，改用最近快照日: {today}")

    HOLDINGS_LOCAL = load_holdings()
    stats = {"total": 0, "buy_ok": 0, "sell_ok": 0, "rebuild_buy_ok": 0, "buy_blocked": 0, "sell_blocked": 0, "buy_block_by_reason": {}, "sell_block_by_reason": {}, "preempt_by_sell_fast_path": 0, "buy_candidate_but_rejected": 0, "buy_candidate_preheat": 0, "buy_candidate_preheat_rejected": 0, "by_code": {}}
    global SIM_NOW
    prev_sim_now = SIM_NOW
    try:
        for path in sorted(snapshot_files):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    snap = json.load(f)
            except Exception as e:
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

            code = str(snap.get("code", "")).strip()
            if not code:
                continue
            bars = snap.get("bars", []) if isinstance(snap, dict) else []
            if not bars:
                continue

            holding = HOLDINGS_LOCAL.get(code, {"name": snap.get("name", code), "t_qty": 0, "qty": 0, "type": "stock", "cost": 0})
            state = {
                "name": snap.get("name", code),
                "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
                "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
                "type": holding.get("type", "stock"),
                "cost": float(holding.get("cost") or 0),
            }

            engine_local = SignalEngine()
            engine_local.state_reset_date = today
            engine_local.buy_count_per_stock[code] = 0
            engine_local.sell_count_per_stock[code] = 0
            engine_local.post_sell_block_until[code] = None
            got_buy = False
            got_sell = False
            code_stats = {"buy_ok": 0, "sell_ok": 0, "rebuild_buy_ok": 0, "buy_blocked": 0, "sell_blocked": 0, "buy_block_by_reason": {}, "sell_block_by_reason": {}, "preempt_by_sell_fast_path": 0, "buy_candidate_but_rejected": 0, "buy_candidate_preheat": 0, "buy_candidate_preheat_rejected": 0}
            stats["total"] += 1
            MINUTE_FETCH_STATUS[code] = "ok"

            for i in range(25, len(bars) + 1):
                df = pd.DataFrame(bars[:i])
                if df.empty:
                    continue
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["time", "open", "high", "low", "close"]).reset_index(drop=True)
                if df.empty or len(df) < 25:
                    continue

                try:
                    current_time = df.iloc[-1]["time"]
                    if hasattr(current_time, "to_pydatetime"):
                        SIM_NOW = current_time.to_pydatetime()
                    elif isinstance(current_time, datetime):
                        SIM_NOW = current_time
                    daily_ctx = snap.get("daily_context") if isinstance(snap, dict) else None
                    if not isinstance(daily_ctx, dict):
                        daily_ctx = _default_daily_context(code, status="replay_missing", reason="snapshot missing daily_context")
                    buy_score, sell_score, sig = engine_local.evaluate(code, snap.get("name", code), add_indicators(df), state, daily_ctx=daily_ctx)
                except Exception:
                    continue

                if sig and sig.action in ["BUY_LOW", "ADD_POS"]:
                    got_buy = True
                    stats["buy_ok"] += 1
                    code_stats["buy_ok"] += 1
                    if engine_local.post_sell_block_until.get(code):
                        stats["rebuild_buy_ok"] += 1
                        code_stats["rebuild_buy_ok"] += 1
                    engine_local.record_trade_action(code, sig.action, sig.hold_qty)
                elif sig and sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    got_sell = True
                    stats["sell_ok"] += 1
                    code_stats["sell_ok"] += 1
                    engine_local.record_trade_action(code, sig.action, sig.hold_qty)
                else:
                    diag = getattr(engine_local, "diagnostics", {}).get(code, {}) if isinstance(getattr(engine_local, "diagnostics", None), dict) else {}
                    if diag.get("buy_candidate_preheat") and sig is None:
                        stats["buy_candidate_preheat_rejected"] += 1
                        code_stats["buy_candidate_preheat_rejected"] += 1
                    if diag.get("buy_candidate") and sig is None:
                        stats["buy_candidate_but_rejected"] += 1
                        code_stats["buy_candidate_but_rejected"] += 1
                        for reason in diag.get("buy_block_reasons", []) or ["unknown"]:
                            stats["buy_block_by_reason"][reason] = stats["buy_block_by_reason"].get(reason, 0) + 1
                            code_stats["buy_block_by_reason"][reason] = code_stats["buy_block_by_reason"].get(reason, 0) + 1
                    if diag.get("sell_candidate") and sig is None:
                        for reason in diag.get("sell_block_reasons", []) or ["unknown"]:
                            stats["sell_block_by_reason"][reason] = stats["sell_block_by_reason"].get(reason, 0) + 1
                            code_stats["sell_block_by_reason"][reason] = code_stats["sell_block_by_reason"].get(reason, 0) + 1
                    if diag.get("preempted_by_sell_fast_path"):
                        stats["preempt_by_sell_fast_path"] += 1
                        code_stats["preempt_by_sell_fast_path"] += 1

            if not got_buy:
                stats["buy_blocked"] += 1
                code_stats["buy_blocked"] += 1
            if not got_sell:
                stats["sell_blocked"] += 1
                code_stats["sell_blocked"] += 1
            stats["by_code"][code] = code_stats
    finally:
        SIM_NOW = prev_sim_now

    out = os.path.join(TRACE_DIR, f"replay_compare_{today}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats}, f, ensure_ascii=False, indent=2)
    log.info(f"回放完成: {out}")
    log.info(f"总快照={stats['total']} 买触发={stats['buy_ok']} 卖触发={stats['sell_ok']} 卖后可买回={stats['rebuild_buy_ok']} 买被挡={stats['buy_blocked']} 卖被挡={stats['sell_blocked']} 买候选预热未成={stats['buy_candidate_preheat_rejected']} 买候选未成交={stats['buy_candidate_but_rejected']} 卖快路径抢占={stats['preempt_by_sell_fast_path']}")
    if stats["buy_block_by_reason"]:
        log.info("买阻塞原因: " + ", ".join(f"{k}:{v}" for k, v in sorted(stats["buy_block_by_reason"].items(), key=lambda kv: -kv[1])[:8]))
    if stats["sell_block_by_reason"]:
        log.info("卖阻塞原因: " + ", ".join(f"{k}:{v}" for k, v in sorted(stats["sell_block_by_reason"].items(), key=lambda kv: -kv[1])[:8]))
    if stats.get("by_code"):
        try:
            with open(out, "r", encoding="utf-8") as f:
                replay_doc = json.load(f)
        except Exception:
            replay_doc = {"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats}
        replay_doc["stats"]["by_code"] = stats["by_code"]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(replay_doc, f, ensure_ascii=False, indent=2)
    else:
        log.info("回放未产生按标的统计，自动学习跳过")
    _apply_replay_learning(today)


def run_watch():
    global HOLDINGS, engine
    HOLDINGS = load_holdings()
    _ensure_preopen_context(force=True)
    engine = SignalEngine()
    log.info("========= 做T终极护城河防御版 (V1.8 确认型收敛版) 启动 =========")
    if PREOPEN_CONTEXT is not None:
        log.info(_format_preopen_brief(PREOPEN_CONTEXT))
    log.info(f"飞书推送: {'✓ 已启用' if FEISHU_WEBHOOK else '✗ 未配置'}")
    log.info(f"飞书关键词: {FEISHU_KEYWORD}")
    if FEISHU_WEBHOOK:
        log.info(f"飞书Webhook: {FEISHU_WEBHOOK[:55]}...")

    # 【2026-06-12 加仓策略】显示在启动日志
    log.info("═" * 70)
    log.info("【2026-06-12 加仓策略执行指南】")
    log.info("  1️⃣ 中国巨石 600176 — ❌ 不追 (已涨5.62%, 资金不足)")
    log.info("  2️⃣ 科创50ETF 588000 — ⚡ 分批加仓 (先加¥15,000)")
    log.info("     └─ 第一批: ¥15,000 (约8,200份)")
    log.info("     └─ 第二批: ¥15,000 (等待盘中回落)")
    log.info("  3️⃣ 中信银行 601998 — ✅ 按计划加 (¥15,000, 约1,900股)")
    log.info("  4️⃣ 特变电工 600089 — ⚡ 加仓 (¥25,000, 约1,090股)")
    log.info("═" * 70)

    if SYS_ALERT_AVAILABLE:
        init_alert(enabled=True)
        log.info("🔊 系统高级报警音效已成功【全自动挂载】！准备执行听声辨位！")
    else:
        log.warning("⚠️ 目录下未检测到 system_alert_v17.3.py，高级报警音效静默禁用。")

    cleanup_expired_minute_cache()
    if should_run_startup_self_test():
        send_startup_self_test()
    log.info(f"⏱ 采用顺序轮询模式：每轮扫描结束后再等待 {PARAMS['poll_interval']} 秒（V1.8 确认型收敛）")

    try:
        while True:
            cycle_start = _now()
            scan_once()
            elapsed = (_now() - cycle_start).total_seconds()
            sleep_seconds = max(0, PARAMS["poll_interval"])
            log.debug(f"⏳ 本轮耗时 {elapsed:.1f}s，等待 {sleep_seconds}s 后进入下一轮")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        log.info("已停止盯盘")
    except Exception as e:
        log.error(f"❌ 盯盘主循环异常: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--replay-today":
        replay_today()
    else:
        run_watch()
