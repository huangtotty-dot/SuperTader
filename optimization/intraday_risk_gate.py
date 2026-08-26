# -*- coding: utf-8 -*-
"""
intraday_risk_gate.py - 盤中实时风险门控系统

核心思路: 在timing_gate GO成立的基础上，加入三道盤中关卡
  L1: 追高风险评分
  L2: 缩量支撑确认
  L3: 日内右侧确认 (w35逻辑)

用途: 在position_builder.scan_stock()后补充，给交易员实时决策支持
"""

import json
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

BASE = Path(__file__).resolve().parent


def calc_l1_risk_score(daily_ctx: dict, intraday_price: float = None) -> Tuple[int, str]:
    """
    L1追高风险评分 (0-100分，高分=高风险)

    三个分量:
      1. 单日涨幅风险 (0-40分)
      2. 放量风险 (0-30分)
      3. 技术极端风险 (0-30分)

    Args:
        daily_ctx: 日线上下文 {
            "price": 当前价,
            "close_prev": 前日收盘,
            "daily_vol_today": 当日成交量,
            "daily_vol_ma5": 5日均量,
            "daily_rsi": RSI(14)
        }
        intraday_price: 盤中实时价 (可选，不提供则用daily_ctx["price"])

    Returns:
        (risk_score, verdict) - 评分和判定
    """

    # 数据提取
    current_price = intraday_price or daily_ctx.get("price", 0)
    close_prev = daily_ctx.get("close_prev", 0)
    vol_today = daily_ctx.get("daily_vol_today", 0)
    vol_ma5 = daily_ctx.get("daily_vol_ma5", 1)
    rsi = daily_ctx.get("daily_rsi", 50)

    if close_prev <= 0 or vol_ma5 <= 0:
        return 0, "LOW_RISK"

    # 1. 单日涨幅风险
    daily_gain = (current_price - close_prev) / close_prev
    if daily_gain > 0.10:
        gain_risk = 40
    elif daily_gain > 0.08:
        gain_risk = 30
    elif daily_gain > 0.05:
        gain_risk = 20
    elif daily_gain > 0.02:
        gain_risk = 10
    else:
        gain_risk = 0

    # 2. 放量风险
    vol_ratio = vol_today / vol_ma5 if vol_ma5 > 0 else 1.0
    if vol_ratio > 2.0:
        vol_risk = 30
    elif vol_ratio > 1.5:
        vol_risk = 20
    elif vol_ratio > 1.2:
        vol_risk = 10
    else:
        vol_risk = 0

    # 3. 技术极端风险 (RSI)
    if rsi > 75:
        extreme_risk = 30
    elif rsi > 70:
        extreme_risk = 20
    elif rsi > 65:
        extreme_risk = 10
    else:
        extreme_risk = 0

    # 总分
    total_risk = gain_risk + vol_risk + extreme_risk

    # 判定
    if total_risk >= 70:
        verdict = "AVOID"
    elif total_risk >= 50:
        verdict = "CAUTIOUS"
    else:
        verdict = "LOW_RISK"

    return total_risk, verdict


def calc_l2_consolidation(code: str, daily_ctx: dict, df_1min: pd.DataFrame = None) -> Dict:
    """
    L2缩量支撑确认

    三个条件:
      1. 冲高回踩支撑不破 (支撑 >= 前日收盘 × 0.99)
      2. 缩量确认 (回踩体量 < 冲高体量 × 0.72)
      3. 趋势向上 (高点有连续抬升迹象)

    Returns:
        {
            "is_consolidating": bool,
            "stage": "waiting_pullback" | "pullback_ongoing" | "consolidated" | "failed",
            "support_level": float,
            "volume_shrink_ratio": float,
            "detail": str
        }
    """

    # 从daily_ctx获取数据
    price = daily_ctx.get("price", 0)
    close_prev = daily_ctx.get("close_prev", 0)

    # 如果有1分钟线，更精确的计算
    if df_1min is not None and not df_1min.empty:
        df = df_1min.copy()
        df["time"] = pd.to_datetime(df["time"], errors="coerce")

        # 找冲高点
        spike_idx = df["high"].idxmax()
        if spike_idx is None or spike_idx < 10:
            return {
                "is_consolidating": False,
                "stage": "waiting_pullback",
                "support_level": close_prev,
                "volume_shrink_ratio": 1.0,
                "detail": "无明显冲高"
            }

        spike_high = df.loc[spike_idx, "high"]

        # 冲高后的数据
        after_spike = df.iloc[spike_idx + 1:]
        if after_spike.empty:
            return {
                "is_consolidating": False,
                "stage": "pullback_ongoing",
                "support_level": close_prev,
                "volume_shrink_ratio": 1.0,
                "detail": "冲高后数据不足"
            }

        # 最低支撑
        support_low = after_spike["low"].min()
        support_hold = support_low >= close_prev * 0.99

        # 缩量
        baseline_vol = df.iloc[:spike_idx].tail(10)["volume"].mean()
        after_vol = after_spike["volume"].mean()
        shrink_ratio = after_vol / baseline_vol if baseline_vol > 0 else 1.0
        volume_shrink = shrink_ratio < 0.72

        # 趋势 (高点是否有抬升)
        recent_highs = after_spike.tail(5)["high"].values
        trend_up = len([h for h in recent_highs if h >= spike_high * 0.995]) >= 3

        is_consolidating = support_hold and volume_shrink and trend_up
        stage = "consolidated" if is_consolidating else (
            "pullback_ongoing" if len(after_spike) < 30 else "failed"
        )

    else:
        # 无1分钟线，用日线数据估计
        support_low = daily_ctx.get("daily_low", close_prev)
        support_hold = support_low >= close_prev * 0.99

        # 用收盘量vs均量估计
        vol_today = daily_ctx.get("daily_vol_today", 0)
        vol_ma5 = daily_ctx.get("daily_vol_ma5", 1)
        shrink_ratio = vol_today / vol_ma5 if vol_ma5 > 0 else 1.0
        volume_shrink = shrink_ratio < 0.72

        # 无法判定趋势，返回等待
        is_consolidating = False
        stage = "waiting_pullback" if not support_hold else "pullback_ongoing"

    detail = f"support{support_low:.2f}[{'ok' if support_hold else 'fail'}] " \
             f"volume{shrink_ratio:.2f}x[{'ok' if volume_shrink else 'fail'}]"

    return {
        "is_consolidating": is_consolidating,
        "stage": stage,
        "support_level": support_low,
        "volume_shrink_ratio": shrink_ratio,
        "detail": detail
    }


def calc_l3_intraday_confirm(df_1min: pd.DataFrame, vol_min: float = 1.2) -> Dict:
    """
    L3日内右侧确认 (参考w35验证的逻辑)

    三个条件:
      1. 15m收盘价 > EMA8
      2. 15m放量 > vol_min倍
      3. 收盘价 >= 当日累计VWAP

    Returns:
        {
            "passed": bool,
            "stage": "insufficient" | "pending" | "confirmed",
            "detail": str,
            "entry_price": float (if confirmed)
        }
    """

    if df_1min is None or df_1min.empty or len(df_1min) < 30:
        return {
            "passed": False,
            "stage": "insufficient",
            "detail": "1分钟数据不足",
            "entry_price": None
        }

    try:
        from analysis.indicators import resample_to_15min, add_15min_indicators
    except ImportError:
        return {
            "passed": False,
            "stage": "insufficient",
            "detail": "指标模块加载失败",
            "entry_price": None
        }

    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    # 计算VWAP
    if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
        cum_amt = d["amount"].fillna(0).cumsum()
    else:
        cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
    cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
    d["vwap_cum"] = cum_amt / cum_vol

    # 转15分钟
    try:
        df15 = resample_to_15min(d)
        df15 = add_15min_indicators(df15)
    except Exception:
        return {
            "passed": False,
            "stage": "insufficient",
            "detail": "15m转换失败",
            "entry_price": None
        }

    if df15 is None or df15.empty:
        return {
            "passed": False,
            "stage": "insufficient",
            "detail": "15m数据为空",
            "entry_price": None
        }

    df15["time"] = pd.to_datetime(df15["time"], errors="coerce")
    last_min_ts = d["time"].iloc[-1]

    # 找最新已收盘的15m bar
    closed = df15[(df15["time"] + pd.Timedelta(minutes=15)) <= (last_min_ts + pd.Timedelta(minutes=1))]

    if closed.empty:
        return {
            "passed": False,
            "stage": "pending",
            "detail": "15m bar未收盘",
            "entry_price": None
        }

    bar = closed.iloc[-1]
    c15 = bar.get("close")
    ema8_15m = bar.get("ema_fast_15m")
    volr = bar.get("vol_ratio_15m")

    if any(pd.isna(x) for x in (c15, ema8_15m, volr)):
        return {
            "passed": False,
            "stage": "pending",
            "detail": "15m指标NaN",
            "entry_price": None
        }

    # VWAP
    close_ts = bar["time"] + pd.Timedelta(minutes=15)
    vw_rows = d[d["time"] <= close_ts]
    vwap = float(vw_rows["vwap_cum"].iloc[-1]) if (not vw_rows.empty and pd.notna(vw_rows["vwap_cum"].iloc[-1])) else None

    # 三个条件
    ema_ok = float(c15) > float(ema8_15m)
    vol_ok = float(volr) > vol_min
    vwap_ok = (vwap is None) or (float(c15) >= vwap)

    passed = ema_ok and vol_ok and vwap_ok

    detail = f"EMA8{'✓' if ema_ok else '✗'} 放量{'✓' if vol_ok else '✗'} VWAP{'✓' if vwap_ok else '✗'}"

    return {
        "passed": passed,
        "stage": "confirmed" if passed else "pending",
        "detail": detail,
        "entry_price": float(c15) if passed else None
    }


def intraday_risk_gate(code: str, daily_ctx: dict, df_1min: pd.DataFrame = None) -> Dict:
    """
    综合盤中风险门控

    整合L1/L2/L3三个关卡，给出综合行动建议

    Returns:
        {
            "code": str,
            "timestamp": str,
            "action": "AVOID" | "WAIT" | "BUY",
            "reason": str,
            "l1": {...},
            "l2": {...},
            "l3": {...}
        }
    """

    # L1
    l1_score, l1_verdict = calc_l1_risk_score(daily_ctx, intraday_price=None)
    l1_detail = f"{l1_score}分 {l1_verdict}"

    # L2
    l2_result = calc_l2_consolidation(code, daily_ctx, df_1min)
    l2_ok = l2_result["is_consolidating"]

    # L3
    l3_result = calc_l3_intraday_confirm(df_1min) if df_1min is not None else {
        "passed": False, "stage": "insufficient", "detail": "无1分钟线", "entry_price": None
    }
    l3_ok = l3_result["passed"]

    # 综合决策
    if l1_verdict == "AVOID":
        action = "AVOID"
        reason = f"L1风险太高({l1_detail})"
    elif l2_result["stage"] == "waiting_pullback":
        action = "WAIT"
        reason = "等待缩量支撑形成"
    elif l2_ok and not l3_ok:
        action = "WAIT"
        reason = f"L2✓ L3未确认 - {l3_result['detail']}"
    elif l2_ok and l3_ok:
        action = "BUY"
        reason = f"L2/L3全过"
    else:
        action = "AVOID"
        reason = f"L2失败 - {l2_result['detail']}"

    return {
        "code": code,
        "timestamp": str(pd.Timestamp.now()),
        "action": action,
        "reason": reason,
        "l1": {
            "score": l1_score,
            "verdict": l1_verdict,
            "detail": l1_detail
        },
        "l2": {
            "ok": l2_ok,
            "stage": l2_result["stage"],
            "support_level": round(l2_result["support_level"], 2),
            "shrink_ratio": round(l2_result["volume_shrink_ratio"], 2),
            "detail": l2_result["detail"]
        },
        "l3": {
            "ok": l3_ok,
            "stage": l3_result["stage"],
            "detail": l3_result["detail"],
            "entry_price": round(l3_result["entry_price"], 2) if l3_result["entry_price"] else None
        }
    }


if __name__ == "__main__":
    # 测试: 摩恩电气
    test_ctx = {
        "price": 8.13,
        "close_prev": 7.39,
        "daily_vol_today": 120000000,  # 假设12亿成交量
        "daily_vol_ma5": 60000000,     # 假设5日均3亿
        "daily_rsi": 68.3,
        "daily_low": 7.11,
        "daily_high": 8.13
    }

    result = intraday_risk_gate("002451", test_ctx, df_1min=None)

    print(f"\n【盘中风险门控】摩恩电气 002451")
    print(f"时间: {result['timestamp']}")
    print(f"\nL1 追高风险: {result['l1']['verdict']} ({result['l1']['score']}分)")
    print(f"  {result['l1']['detail']}")
    print(f"\nL2 缩量支撑: {'ok' if result['l2']['ok'] else result['l2']['stage']}")
    print(f"  {result['l2']['detail']}")
    print(f"\nL3 日内确认: {result['l3']['stage']}")
    print(f"  {result['l3']['detail']}")
    print(f"\n【决策】{result['action']}")
    print(f"原因: {result['reason']}")

    print("\n\n" + json.dumps(result, indent=2, ensure_ascii=False))
