# -*- coding: utf-8 -*-
"""
周线策略模块
"""
from datetime import datetime, timedelta
from typing import Tuple, List, Dict

import pandas as pd
import numpy as np

from config import log, WEEKLY_BOTTOM_MIN_BARS

from utils import sector_strength_score, sector_priority_tag

def build_weekly_from_daily(df_daily: pd.DataFrame) -> pd.DataFrame:
    if df_daily.empty or "date" not in df_daily.columns:
        return pd.DataFrame()

    weekly = df_daily.copy()
    weekly["date"] = pd.to_datetime(weekly["date"])
    for col in ["open", "close", "high", "low", "volume"]:
        if col in weekly.columns:
            weekly[col] = pd.to_numeric(weekly[col], errors="coerce")

    weekly = weekly.dropna(subset=["date", "open", "close", "high", "low", "volume"])
    if weekly.empty:
        return pd.DataFrame()

    weekly = weekly.set_index("date").sort_index()
    agg_map = {
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
    }
    if "amount" in weekly.columns:
        agg_map["amount"] = "sum"
    weekly = weekly.resample("W-FRI", label="right", closed="right").agg(agg_map).dropna(subset=["open", "close", "high", "low"])

    weekly = weekly.reset_index()
    weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")
    return weekly


def normalize_weekly_target(input_date: str) -> str:
    try:
        target_dt = datetime.strptime(input_date, "%Y%m%d")
        offset = (target_dt.weekday() - 4) % 7
        return (target_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def check_weekly_breakout(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    try:
        if weekly_df.empty or "date" not in weekly_df.columns or not target_week:
            return None, ""

        weekly = weekly_df.copy()
        weekly["date"] = pd.to_datetime(weekly["date"], errors="coerce")
        weekly = weekly.dropna(subset=["date", "open", "close", "high", "low"])
        if weekly.empty:
            return None, ""

        target_dt = pd.to_datetime(target_week, errors="coerce")
        if pd.isna(target_dt):
            return None, ""

        weekly = weekly.sort_values("date")
        target_rows = weekly[weekly["date"] == target_dt]
        if target_rows.empty:
            return None, ""

        target_row = target_rows.iloc[-1]

        history = weekly[weekly["date"] < target_dt]
        if history.empty:
            return None, ""

        bullish_history = history[history["close"] > history["open"]]
        if bullish_history.empty:
            return None, ""

        bullish_max_high = bullish_history["high"].max()
        prev_high_row = bullish_history[bullish_history["high"] == bullish_max_high].iloc[-1]

        if float(target_row["close"]) <= float(target_row["open"]):
            return None, ""

        if float(target_row["high"]) > float(prev_high_row["high"]):
            amount_text = f"，周五成交额 {float(target_amount):.0f}" if target_amount is not None else ""
            return (
                "📈 周线突破前高",
                f"目标周 {target_dt.strftime('%Y-%m-%d')} 收阳且最高价 {float(target_row['high']):.2f} 突破前高阳线高点 {float(prev_high_row['high']):.2f}（{prev_high_row['date'].strftime('%Y-%m-%d')}）{amount_text}"
            )
        return None, ""
    except Exception as e:
        log.debug(f"策略检查异常: {str(e)}")
        return None, ""


def check_weekly_bottom_stabilize(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    try:
        if weekly_df.empty or "date" not in weekly_df.columns or not target_week:
            return None, ""

        weekly = weekly_df.copy()
        weekly["date"] = pd.to_datetime(weekly["date"], errors="coerce")
        weekly = weekly.dropna(subset=["date", "open", "close", "high", "low", "volume"])
        if len(weekly) < WEEKLY_BOTTOM_MIN_BARS:
            return None, ""

        weekly = weekly.sort_values("date").reset_index(drop=True)
        target_dt = pd.to_datetime(target_week, errors="coerce")
        if pd.isna(target_dt):
            return None, ""

        target_idx = weekly.index[weekly["date"] == target_dt]
        if len(target_idx) == 0:
            return None, ""
        idx = int(target_idx[-1])
        if idx < WEEKLY_BOTTOM_MIN_BARS - 1:
            return None, ""

        target_row = weekly.iloc[idx]
        prev_row = weekly.iloc[idx - 1]
        history = weekly.iloc[:idx + 1].copy()
        if history.empty:
            return None, ""

        for col in ["close", "volume"]:
            history[col] = pd.to_numeric(history[col], errors="coerce")
        history["ma5"] = history["close"].rolling(5).mean()
        history["ma10"] = history["close"].rolling(10).mean()
        history["ma20"] = history["close"].rolling(20).mean()
        history["ma30"] = history["close"].rolling(30).mean()
        history["vol_ma5"] = history["volume"].rolling(5).mean()
        history["vol_ma12"] = history["volume"].rolling(12).mean()

        today = history.iloc[-1]
        prev = history.iloc[-2]
        last_30 = history.iloc[-min(30, len(history)):]
        last_12 = history.iloc[-min(12, len(history)):]
        last_6 = history.iloc[-min(6, len(history)):]

        price = float(today["close"])
        open_price = float(today["open"])
        high_price = float(today["high"])
        low_price = float(today["low"])
        prev_close = float(prev["close"])
        prev_low = float(prev["low"])
        is_yang = price > open_price
        body_pos = (price - open_price) / max(high_price - low_price, 1e-9)
        vol_ratio = float(today["volume"]) / max(float(today["vol_ma5"]) if pd.notna(today["vol_ma5"]) and float(today["vol_ma5"]) > 0 else float(today["volume"]), 1.0)

        low_30 = float(last_30["low"].min())
        high_30 = float(last_30["high"].max())
        low_12 = float(last_12["low"].min())
        high_12 = float(last_12["high"].max())
        low_6 = float(last_6["low"].min())
        high_6 = float(last_6["high"].max())
        low_pos = (price - low_30) / max(high_30 - low_30, 1e-9)
        rebound_pos = (price - low_6) / max(high_6 - low_6, 1e-9)

        ma5 = float(today["ma5"]) if pd.notna(today["ma5"]) else 0.0
        ma10 = float(today["ma10"]) if pd.notna(today["ma10"]) else 0.0
        ma20 = float(today["ma20"]) if pd.notna(today["ma20"]) else 0.0
        ma30 = float(today["ma30"]) if pd.notna(today["ma30"]) else 0.0
        prev_ma30 = float(prev["ma30"]) if pd.notna(prev["ma30"]) else ma30

        if float(target_row["volume"]) <= 0:
            return None, ""

        # 企稳要求更像“低位收敛后守住短均线”
        if low_pos > 0.45:
            return None, ""
        if price >= high_30 * 0.92:
            return None, ""
        if ma30 > 0 and price < ma30 * 0.88:
            return None, ""
        if ma30 > 0 and prev_ma30 > 0 and ma30 < prev_ma30 * 0.97:
            return None, ""
        if low_12 < low_30 * 0.95 and price < ma10 * 0.98:
            return None, ""

        stabilizing = [
            price >= max(ma5, ma10) * 0.98 if max(ma5, ma10) > 0 else False,
            price > prev_close,
            float(target_row["low"]) >= prev_low * 0.985,
            is_yang,
            body_pos > 0.45,
            vol_ratio <= 1.8,
        ]
        if sum(1 for item in stabilizing if item) < 4:
            return None, ""

        amount_text = f"，周五成交额 {float(target_amount):.0f}" if target_amount is not None else ""
        return (
            "🌱 周线底部企稳",
            f"目标周 {target_dt.strftime('%Y-%m-%d')} 位于30周区间低位({low_pos*100:.1f}%)，收盘 {price:.2f} 站上/靠近短均线，较上周收高且低点未创新低，周线开始企稳{amount_text}"
        )
    except Exception as e:
        log.debug(f"周线底部企稳策略检查异常: {str(e)}")
        return None, ""


def check_weekly_pullback_stabilize(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    try:
        if weekly_df.empty or "date" not in weekly_df.columns or not target_week:
            return None, ""

        weekly = weekly_df.copy()
        weekly["date"] = pd.to_datetime(weekly["date"], errors="coerce")
        weekly = weekly.dropna(subset=["date", "open", "close", "high", "low", "volume"])
        if len(weekly) < WEEKLY_BOTTOM_MIN_BARS:
            return None, ""

        weekly = weekly.sort_values("date").reset_index(drop=True)
        target_dt = pd.to_datetime(target_week, errors="coerce")
        if pd.isna(target_dt):
            return None, ""

        target_idx = weekly.index[weekly["date"] == target_dt]
        if len(target_idx) == 0:
            return None, ""
        idx = int(target_idx[-1])
        if idx < WEEKLY_BOTTOM_MIN_BARS - 1:
            return None, ""

        target_row = weekly.iloc[idx]
        prev_row = weekly.iloc[idx - 1]
        history = weekly.iloc[:idx + 1].copy()
        if len(history) < 8:
            return None, ""

        for col in ["close", "volume"]:
            history[col] = pd.to_numeric(history[col], errors="coerce")
        history["ma5"] = history["close"].rolling(5).mean()
        history["ma10"] = history["close"].rolling(10).mean()
        history["ma20"] = history["close"].rolling(20).mean()
        history["ma30"] = history["close"].rolling(30).mean()
        history["vol_ma5"] = history["volume"].rolling(5).mean()

        today = history.iloc[-1]
        prev = history.iloc[-2]
        last_6 = history.iloc[-min(6, len(history)):]
        last_12 = history.iloc[-min(12, len(history)):]
        last_30 = history.iloc[-min(30, len(history)):]

        price = float(today["close"])
        open_price = float(today["open"])
        high_price = float(today["high"])
        low_price = float(today["low"])
        prev_close = float(prev["close"])
        prev_low = float(prev["low"])
        is_yang = price > open_price
        body_pos = (price - open_price) / max(high_price - low_price, 1e-9)
        vol_ratio = float(today["volume"]) / max(float(today["vol_ma5"]) if pd.notna(today["vol_ma5"]) and float(today["vol_ma5"]) > 0 else float(today["volume"]), 1.0)

        low_6 = float(last_6["low"].min())
        high_6 = float(last_6["high"].max())
        low_12 = float(last_12["low"].min())
        high_12 = float(last_12["high"].max())
        low_30 = float(last_30["low"].min())
        high_30 = float(last_30["high"].max())
        low_pos = (price - low_30) / max(high_30 - low_30, 1e-9)
        rebound_pos = (price - low_6) / max(high_6 - low_6, 1e-9)
        drop_from_12_high = (high_12 - price) / max(high_12, 1e-9)
        drop_from_6_high = (high_6 - price) / max(high_6, 1e-9)

        ma5 = float(today["ma5"]) if pd.notna(today["ma5"]) else 0.0
        ma10 = float(today["ma10"]) if pd.notna(today["ma10"]) else 0.0
        ma20 = float(today["ma20"]) if pd.notna(today["ma20"]) else 0.0
        ma30 = float(today["ma30"]) if pd.notna(today["ma30"]) else 0.0
        prev_ma30 = float(prev["ma30"]) if pd.notna(prev["ma30"]) else ma30

        if float(target_row["volume"]) <= 0:
            return None, ""
        if ma30 <= 0 or ma10 <= 0 or ma5 <= 0:
            return None, ""

        # 允许从“前几周连续走弱后，目标周止跌回收”中捕捉反抽
        if low_pos > 0.72:
            return None, ""
        if price < ma30 * 0.92:
            return None, ""
        if ma30 < prev_ma30 * 0.93:
            return None, ""
        if drop_from_12_high < 0.06:
            return None, ""
        if drop_from_6_high < 0.04 and low_pos > 0.5:
            return None, ""

        low_break_reclaim = float(target_row["low"]) <= prev_low * 0.98
        close_reclaim = price >= prev_close * 1.02
        short_ma_reclaim = price >= ma10 * 0.99 or price >= ma20 * 0.97
        strength_signals = [
            is_yang,
            price > prev_close,
            low_break_reclaim,
            close_reclaim,
            short_ma_reclaim,
            rebound_pos >= 0.28,
            body_pos > 0.18,
            vol_ratio <= 1.6,
        ]
        if sum(1 for item in strength_signals if item) < 6:
            return None, ""

        amount_text = f"，周五成交额 {float(target_amount):.0f}" if target_amount is not None else ""
        return (
            "🌿 周线止跌反抽",
            f"目标周 {target_dt.strftime('%Y-%m-%d')} 位于30周区间低位({low_pos*100:.1f}%)，本周向下刺破前周低点后收回，收盘 {price:.2f} 回收短均线附近并站回前周收盘，出现修复反抽{amount_text}"
        )
    except Exception as e:
        log.debug(f"周线止跌反抽策略检查异常: {str(e)}")
        return None, ""


def check_weekly_strategies(weekly_df: pd.DataFrame, target_week: str, target_amount: float = None) -> Tuple[str, str]:
    sig_type, reason = check_weekly_breakout(weekly_df, target_week, target_amount=target_amount)
    if sig_type:
        return sig_type, reason
    sig_type, reason = check_weekly_bottom_stabilize(weekly_df, target_week, target_amount=target_amount)
    if sig_type:
        return sig_type, reason
    return check_weekly_pullback_stabilize(weekly_df, target_week, target_amount=target_amount)


def sector_top3_brief(grouped: List[tuple]) -> str:
    if not grouped:
        return "暂无"
    top3 = sorted(grouped, key=lambda item: sector_strength_score(item[1]))[:3]
    return " | ".join(
        f"{idx}. {sector}｜热度{len(sigs)}｜最强{sector_priority_tag(sigs)}"
        for idx, (sector, sigs) in enumerate(top3, 1)
    )
