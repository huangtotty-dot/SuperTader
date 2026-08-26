# -*- coding: utf-8 -*-
"""
intraday_surge_defense.py - 日内冲高防御系统

核心问题：
  1. 涨停/大幅涨后"冲高回落" → 如果日内追进，容易被套
  2. 集合竞价涨停 vs 日内涨停 → 风险完全不同
  3. 缺少"实时监控高点是否突破"的机制

解决方案：
  1. 涨停分类：集合竞价涨停、日内涨停、尾盘涨停 → 风险递增
  2. 冲高回落检测：高点回踩 >50% → 发出"回落预警"
  3. 日内分时买点检测：防止"虚假突破后回落"
"""

import json
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

BASE = Path(__file__).resolve().parent


@dataclass
class SurgeDefenseResult:
    """冲高防御结果"""
    code: str
    name: str
    timestamp: str

    # 1. 涨停分类
    is_daily_limit: bool
    limit_type: str  # "auction_limit" | "intraday_limit" | "none"
    limit_classification_reason: str

    # 2. 冲高监控
    high_reached: float
    high_time: Optional[str]
    current_price: float
    pullback_ratio: float  # (high - current) / high，0=无回落，>0.05=明显回落

    # 3. 防御建议
    action: str  # "SAFE" | "WARNING" | "AVOID" | "EXIT"
    reason: str
    alert_level: str  # "normal" | "warning" | "critical"

    # 4. 详细诊断
    detail: Dict


def classify_daily_limit(df_1min: pd.DataFrame) -> Tuple[bool, str, str]:
    """
    分类涨停类型：

    1. 集合竞价涨停 (09:30 开盘就已是涨停)
       → 极高风险，不应该追高

    2. 日内涨停 (09:30-14:50 间涨停)
       → 相对安全，有明确日内上升趋势

    3. 尾盘涨停 (14:50 后涨停)
       → 较安全，但流动性差

    Returns:
        (is_limit, limit_type, reason)
    """
    if df_1min is None or df_1min.empty or len(df_1min) < 5:
        return False, "none", "数据不足"

    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    if d.empty:
        return False, "none", "时间戳异常"

    # 取前日收盘（假设第一条记录的 close_prev）
    if "close_prev" in d.columns:
        prev_close = float(d["close_prev"].iloc[0])
    else:
        # 尝试从日线数据获取
        try:
            from core.position_builder import fetch_daily_kline
            code = d.get("code", "unknown")
            daily = fetch_daily_kline(code)
            if daily is not None and not daily.empty:
                prev_close = float(daily.iloc[-1]["close"])
            else:
                return False, "none", "无前日收盘"
        except Exception:
            return False, "none", "前日收盘获取失败"

    limit_price = prev_close * 1.10  # 涨停价（10%）

    # 检查是否触及涨停
    d["is_at_limit"] = d["high"] >= limit_price * 0.99  # 允许1%误差

    limit_rows = d[d["is_at_limit"]]
    if limit_rows.empty:
        return False, "none", "未涨停"

    first_limit_time = limit_rows["time"].iloc[0]
    first_limit_time_hm = first_limit_time.strftime("%H:%M")

    # 分类
    if first_limit_time_hm <= "09:31":
        return True, "auction_limit", f"集合竞价涨停(09:30-09:31涨停)"
    elif first_limit_time_hm <= "14:50":
        return True, "intraday_limit", f"日内涨停({first_limit_time_hm})"
    else:
        return True, "close_limit", f"尾盘涨停({first_limit_time_hm})"


def detect_pullback_from_high(df_1min: pd.DataFrame, vol_min: float = 1.0) -> Dict:
    """
    冲高回落检测：

    判据：
      1. 找当日最高点
      2. 计算当前价 vs 最高点的回落幅度
      3. 回落幅度 > 5% → 警告
      4. 回落幅度 > 10% → 严重警告
      5. 高点确认：最高点附近必须有足够的成交量

    Returns:
        {
            "high_price": float,
            "high_time": str,
            "current_price": float,
            "pullback_amount": float,
            "pullback_ratio": float,  # 占最高点的百分比
            "alert_level": "none" | "warning" | "critical",
            "reason": str
        }
    """

    if df_1min is None or df_1min.empty:
        return {
            "high_price": 0,
            "high_time": None,
            "current_price": 0,
            "pullback_amount": 0,
            "pullback_ratio": 0,
            "alert_level": "none",
            "reason": "分钟线数据缺失"
        }

    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    if d.empty or len(d) < 10:
        return {
            "high_price": 0,
            "high_time": None,
            "current_price": 0,
            "pullback_amount": 0,
            "pullback_ratio": 0,
            "alert_level": "none",
            "reason": "分钟线不足"
        }

    high_idx = d["high"].idxmax()
    high_price = float(d.loc[high_idx, "high"])
    high_time = d.loc[high_idx, "time"]
    high_time_str = high_time.strftime("%H:%M") if pd.notna(high_time) else None

    current_price = float(d["close"].iloc[-1])
    pullback_amount = high_price - current_price
    pullback_ratio = pullback_amount / high_price if high_price > 0 else 0

    # 判定
    if pullback_ratio < 0.02:
        alert_level = "none"
        reason = f"基本无回落(回落{pullback_ratio*100:.1f}%)"
    elif pullback_ratio < 0.05:
        alert_level = "warning"
        reason = f"轻微回落(回落{pullback_ratio*100:.1f}%，距高点{pullback_amount:.3f})"
    elif pullback_ratio < 0.10:
        alert_level = "warning"
        reason = f"明显回落(回落{pullback_ratio*100:.1f}%，距高点{pullback_amount:.3f})"
    else:
        alert_level = "critical"
        reason = f"严重回落(回落{pullback_ratio*100:.1f}%，距高点{pullback_amount:.3f})"

    return {
        "high_price": round(high_price, 3),
        "high_time": high_time_str,
        "current_price": round(current_price, 3),
        "pullback_amount": round(pullback_amount, 3),
        "pullback_ratio": round(pullback_ratio, 4),
        "alert_level": alert_level,
        "reason": reason
    }


def check_intraday_buypoint_quality(df_1min: pd.DataFrame) -> Dict:
    """
    日内买点质量评估：防止"虚假突破后回落"

    需求：当前价是否处于"真实支撑"而不是"技术反弹"

    判据：
      1. 当前价 > 5min MA5（短期均线）
      2. 5min vol_ratio > 1.2（有量能）
      3. 15min close > EMA8（中期趋势向上）
      4. 距今日最低点 > 1.5%（不要在底部）

    Returns:
        {
            "is_quality_buypoint": bool,
            "5m_above_ma5": bool,
            "5m_volume_confirm": bool,
            "15m_above_ema8": bool,
            "above_daily_low": bool,
            "checklist": {...}
        }
    """

    if df_1min is None or df_1min.empty or len(df_1min) < 50:
        return {
            "is_quality_buypoint": False,
            "reason": "分钟线数据不足",
            "5m_above_ma5": False,
            "5m_volume_confirm": False,
            "15m_above_ema8": False,
            "above_daily_low": False,
        }

    try:
        from core.position_builder import resample_to_5min, add_5min_indicators, resample_to_15min, add_15min_indicators
    except ImportError:
        return {
            "is_quality_buypoint": False,
            "reason": "指标模块加载失败",
            "5m_above_ma5": False,
            "5m_volume_confirm": False,
            "15m_above_ema8": False,
            "above_daily_low": False,
        }

    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    if d.empty:
        return {"is_quality_buypoint": False, "reason": "时间戳解析失败"}

    current_price = float(d["close"].iloc[-1])
    daily_low = float(d["low"].min())

    # 5分钟指标
    d5 = add_5min_indicators(resample_to_5min(d))
    if d5 is None or d5.empty:
        return {"is_quality_buypoint": False, "reason": "5分钟转换失败"}

    d5_latest = d5.iloc[-1]
    ma5_5m = d5_latest.get("ma5_5m")
    vol_ratio_5m = d5_latest.get("vol_ratio_5m", 0)
    above_ma5 = (pd.notna(ma5_5m) and current_price > ma5_5m)
    vol_ok = vol_ratio_5m > 1.2

    # 15分钟指标
    d15 = add_15min_indicators(resample_to_15min(d))
    if d15 is None or d15.empty:
        return {"is_quality_buypoint": False, "reason": "15分钟转换失败"}

    d15_latest = d15.iloc[-1]
    ema8_15m = d15_latest.get("ema_fast_15m")
    above_ema8 = (pd.notna(ema8_15m) and current_price > ema8_15m)

    # 距低点
    low_dist_ratio = (current_price - daily_low) / daily_low if daily_low > 0 else 0
    above_low = low_dist_ratio > 0.015  # 距低点 >1.5%

    is_quality = above_ma5 and vol_ok and above_ema8 and above_low

    return {
        "is_quality_buypoint": is_quality,
        "5m_above_ma5": bool(above_ma5),
        "5m_volume_confirm": bool(vol_ok),
        "15m_above_ema8": bool(above_ema8),
        "above_daily_low": bool(above_low),
        "checklist": {
            "current_price": round(current_price, 3),
            "ma5_5m": round(float(ma5_5m), 3) if pd.notna(ma5_5m) else None,
            "vol_ratio_5m": round(vol_ratio_5m, 2),
            "ema8_15m": round(float(ema8_15m), 3) if pd.notna(ema8_15m) else None,
            "daily_low": round(daily_low, 3),
            "low_dist_ratio": round(low_dist_ratio, 4),
        }
    }


def intraday_surge_defense(
    code: str,
    name: str,
    df_1min: pd.DataFrame,
    daily_ctx: Dict = None
) -> SurgeDefenseResult:
    """
    综合冲高防御评估

    输出行动建议：
      SAFE → 当前无冲高风险
      WARNING → 有回落迹象，谨慎追高
      AVOID → 明显回落，应该回避或减仓
      EXIT → 严重回落，建议止损退出
    """

    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 涨停分类
    is_limit, limit_type, limit_reason = classify_daily_limit(df_1min)

    # 2. 冲高回落
    pullback = detect_pullback_from_high(df_1min)

    # 3. 买点质量
    quality = check_intraday_buypoint_quality(df_1min)

    # 综合决策
    if not is_limit:
        action = "SAFE"
        alert_level = "normal"
        reason = "未涨停，冲高风险低"
    elif limit_type == "auction_limit":
        action = "AVOID"
        alert_level = "critical"
        reason = "集合竞价已涨停，极高风险，强烈建议回避"
    elif pullback["alert_level"] == "critical":
        action = "EXIT"
        alert_level = "critical"
        reason = f"严重回落({pullback['reason']})，建议止损退出"
    elif pullback["alert_level"] == "warning" and not quality["is_quality_buypoint"]:
        action = "AVOID"
        alert_level = "warning"
        reason = f"回落 + 买点质量差({quality.get('reason', '未达标')})"
    elif pullback["alert_level"] == "warning":
        action = "WARNING"
        alert_level = "warning"
        reason = f"有回落迹象({pullback['reason']})，不宜追高"
    else:
        action = "SAFE"
        alert_level = "normal"
        reason = f"{limit_type}且无明显回落，相对安全"

    return SurgeDefenseResult(
        code=code,
        name=name,
        timestamp=now,
        is_daily_limit=is_limit,
        limit_type=limit_type,
        limit_classification_reason=limit_reason,
        high_reached=pullback["high_price"],
        high_time=pullback["high_time"],
        current_price=pullback["current_price"],
        pullback_ratio=pullback["pullback_ratio"],
        action=action,
        reason=reason,
        alert_level=alert_level,
        detail={
            "limit_info": {
                "is_limit": is_limit,
                "type": limit_type,
                "reason": limit_reason,
            },
            "pullback_info": pullback,
            "quality_info": quality,
        }
    )


if __name__ == "__main__":
    # 测试
    test_result = SurgeDefenseResult(
        code="002451",
        name="摩恩电气",
        timestamp="2026-08-25 10:30:00",
        is_daily_limit=True,
        limit_type="auction_limit",
        limit_classification_reason="集合竞价涨停",
        high_reached=8.13,
        high_time="09:30",
        current_price=7.24,
        pullback_ratio=0.1092,
        action="EXIT",
        reason="严重回落(回落10.9%)，建议止损退出",
        alert_level="critical",
        detail={}
    )

    print("\n【冲高防御系统】")
    print(f"代码: {test_result.code} {test_result.name}")
    print(f"时间: {test_result.timestamp}")
    print(f"\n[涨停分类]: {test_result.limit_type}")
    print(f"   {test_result.limit_classification_reason}")
    print(f"\n[冲高监控]:")
    print(f"   高点: {test_result.high_reached} (于 {test_result.high_time})")
    print(f"   当前: {test_result.current_price}")
    print(f"   回落: {test_result.pullback_ratio*100:.1f}%")
    print(f"\n[行动建议] {test_result.action} (等级: {test_result.alert_level})")
    print(f"   {test_result.reason}")
    print()
