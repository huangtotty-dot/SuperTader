# -*- coding: utf-8 -*-
"""
做T日志增强模块 (v1.11 日志系统)
负责：早盘冲高事件日志、信号错过分析、做T建议日志、收盘复盘数据
"""
import os
import json
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# 日志路径
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t_io", "logs")
TRACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t_io", "traces")
REVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "t_io", "reviews")

for d in [LOG_DIR, TRACE_DIR, REVIEW_DIR]:
    os.makedirs(d, exist_ok=True)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _append_jsonl(path: str, record: dict):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _get_path(kind: str, day: Optional[str] = None) -> str:
    day = day or _today_str()
    return os.path.join(TRACE_DIR, f"{kind}_{day}.jsonl")


# ==================== 早盘冲高事件日志 ====================

def log_morning_surge(
    code: str, name: str, stage: str, price: float, vwap: float,
    today_ret: float, sell_score: int, sell_threshold: int,
    factors: List[str], is_triggered: bool = False
):
    """记录早盘冲高事件（检测/触发/错过）
    
    stage: "detected"=检测到了但未触发, "triggered"=触发信号, "missed"=完全错过(未检测)
    """
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "morning_surge",
        "code": code,
        "name": name,
        "stage": stage,
        "price": price,
        "vwap": vwap,
        "today_ret": today_ret,
        "sell_score": sell_score,
        "sell_threshold": sell_threshold,
        "distance_to_threshold": sell_threshold - sell_score,
        "factors": factors,
        "is_triggered": is_triggered,
    }
    _append_jsonl(_get_path("morning_surge_events"), record)


def log_afternoon_pullback(
    code: str, name: str, stage: str, price: float, vwap: float,
    rsi: float, buy_score: int, buy_threshold: int, factors: List[str]
):
    """记录下午回落接回事件"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "afternoon_pullback",
        "code": code,
        "name": name,
        "stage": stage,
        "price": price,
        "vwap": vwap,
        "rsi": rsi,
        "buy_score": buy_score,
        "buy_threshold": buy_threshold,
        "distance_to_threshold": buy_threshold - buy_score,
        "factors": factors,
    }
    _append_jsonl(_get_path("afternoon_pullback_events"), record)


# ==================== 信号错过分析日志 ====================

def log_missed_signal(
    code: str, name: str, signal_type: str, price: float, vwap: float,
    score: int, threshold: int, miss_reason: str, detail_reasons: List[str]
):
    """记录信号错过原因（用于后续分析为什么没触发）"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "missed_signal",
        "code": code,
        "name": name,
        "signal_type": signal_type,  # "BUY_LOW" / "SELL_HIGH"
        "price": price,
        "vwap": vwap,
        "score": score,
        "threshold": threshold,
        "distance": threshold - score,
        "miss_reason": miss_reason,  # 主原因
        "detail_reasons": detail_reasons,  # 详细原因列表
    }
    _append_jsonl(_get_path("missed_signals"), record)


# ==================== 做T建议日志 ====================

def log_t_advice(
    code: str, name: str, action: str, trigger_price: float,
    suggested_buyback: float, suggested_resell: float,
    vwap: float, today_ret: float, factors: List[str]
):
    """记录做T建议（包含预计接回/卖出价位）"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "t_advice",
        "code": code,
        "name": name,
        "action": action,
        "trigger_price": trigger_price,
        "suggested_buyback": suggested_buyback,
        "suggested_resell": suggested_resell,
        "vwap": vwap,
        "today_ret": today_ret,
        "factors": factors,
    }
    _append_jsonl(_get_path("t_advice"), record)


# ==================== 参数变更日志 ====================

def log_param_change(
    param_name: str, old_value: Any, new_value: Any, reason: str
):
    """记录参数变更（用于回退时知道改了什么）"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "param_change",
        "param_name": param_name,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    }
    _append_jsonl(_get_path("param_changes"), record)


# ==================== 信号延迟分析日志 ====================

def log_signal_latency(
    code: str, name: str, action: str, optimal_time: str,
    actual_time: str, optimal_price: float, actual_price: float,
    latency_seconds: int, price_slippage_pct: float, reason: str
):
    """记录信号延迟/提前情况（用于评估信号质量）"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "signal_latency",
        "code": code,
        "name": name,
        "action": action,
        "optimal_time": optimal_time,
        "actual_time": actual_time,
        "optimal_price": optimal_price,
        "actual_price": actual_price,
        "latency_seconds": latency_seconds,
        "price_slippage_pct": price_slippage_pct,
        "reason": reason,
    }
    _append_jsonl(_get_path("signal_latency"), record)


# ==================== EOD 收盘复盘日志 ====================

def log_eod_review(
    code: str, name: str, high_price: float, low_price: float,
    close_price: float, vwap: float, day_ret: float,
    best_sell_time: Optional[str], best_sell_price: Optional[float],
    best_buy_time: Optional[str], best_buy_price: Optional[float],
    signals_triggered: List[dict], profit_potential: float
):
    """记录收盘复盘数据（用于评估今日做T机会）"""
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_type": "eod_review",
        "code": code,
        "name": name,
        "high_price": high_price,
        "low_price": low_price,
        "close_price": close_price,
        "vwap": vwap,
        "day_ret": day_ret,
        "best_sell_time": best_sell_time,
        "best_sell_price": best_sell_price,
        "best_buy_time": best_buy_time,
        "best_buy_price": best_buy_price,
        "signals_triggered": signals_triggered,
        "profit_potential": profit_potential,
    }
    _append_jsonl(_get_path("eod_review"), record)


# ==================== 日志读取工具（供分析脚本使用） ====================

def read_jsonl(path: str) -> List[dict]:
    """读取jsonl文件，返回记录列表"""
    records = []
    if not os.path.exists(path):
        return records
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return records


def get_event_records(event_type: str, day: Optional[str] = None) -> List[dict]:
    """获取指定类型的事件记录"""
    path = _get_path(event_type, day)
    return read_jsonl(path)


def get_decision_trace(day: Optional[str] = None) -> List[dict]:
    """获取决策trace记录"""
    return read_jsonl(_get_path("decision_trace", day))


def get_shadow_signals(day: Optional[str] = None) -> List[dict]:
    """获取shadow_signals记录"""
    return read_jsonl(_get_path("shadow_signals", day))
