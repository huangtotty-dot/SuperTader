#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Renko 砖形图生成器
论文来源: "Real-Time Intraday Trading Using Renko-MACD Strategy" (2025, Springer)
核心优势: 自动过滤日内噪音、清晰显示价格趋势

砖高参数建议 (基于 ATR 或百分比):
  - 保守 (0.2%): 信号多、胜率低
  - 中等 (0.3%): 信号适中、胜率中等、推荐
  - 激进 (0.5%): 信号少、胜率高
"""
import numpy as np
import pandas as pd
from typing import List, Dict, Tuple, Optional


class RenkoBuilder:
    """Renko K线构建器"""

    def __init__(self, brick_size_pct: float = 0.003, brick_size_absolute: Optional[float] = None):
        """
        初始化 Renko 构建器

        Args:
            brick_size_pct: 砖高（百分比，如 0.003 = 0.3%）
            brick_size_absolute: 砖高（绝对值，优先级 > brick_size_pct）
        """
        self.brick_size_pct = brick_size_pct
        self.brick_size_absolute = brick_size_absolute
        self.bricks: List[Dict] = []
        self.last_brick_top = None
        self.last_brick_bottom = None
        self.brick_direction = None  # "up" or "down"

    def _get_brick_size(self, price: float) -> float:
        """获取当前砖高"""
        if self.brick_size_absolute is not None:
            return self.brick_size_absolute
        return price * self.brick_size_pct

    def update(self, timestamp, close_price: float, high_price: float, low_price: float, volume: float = 0) -> bool:
        """
        更新 Renko 砖（返回是否产生新砖）

        Args:
            timestamp: 时间戳
            close_price: 收盘价
            high_price: 最高价
            low_price: 最低价
            volume: 成交量

        Returns:
            是否产生新砖
        """
        if not self.bricks:
            # 初始化第一块砖
            brick_size = self._get_brick_size(close_price)
            self.last_brick_top = close_price + brick_size
            self.last_brick_bottom = close_price - brick_size
            self.bricks.append({
                "timestamp": timestamp,
                "price": close_price,
                "direction": None,
                "volume": volume,
                "brick_top": self.last_brick_top,
                "brick_bottom": self.last_brick_bottom,
            })
            return False

        brick_size = self._get_brick_size(close_price)
        new_brick_created = False

        # 判断是否突破砖顶（产生向上新砖）
        if close_price > self.last_brick_top:
            self.brick_direction = "up"
            self.last_brick_bottom = self.last_brick_top
            self.last_brick_top = self.last_brick_bottom + brick_size
            new_brick_created = True

        # 判断是否跌破砖底（产生向下新砖）
        elif close_price < self.last_brick_bottom:
            self.brick_direction = "down"
            self.last_brick_top = self.last_brick_bottom
            self.last_brick_bottom = self.last_brick_top - brick_size
            new_brick_created = True

        if new_brick_created:
            self.bricks.append({
                "timestamp": timestamp,
                "price": close_price,
                "direction": self.brick_direction,
                "volume": volume,
                "brick_top": self.last_brick_top,
                "brick_bottom": self.last_brick_bottom,
            })

        return new_brick_created

    def batch_update(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        批量处理分钟数据，生成 Renko K线

        Args:
            df: DataFrame，需要包含 time/close/high/low/vol 列

        Returns:
            带有 Renko 信号的 DataFrame
        """
        renko_signals = []

        for idx, row in df.iterrows():
            timestamp = row.get("time") or row.get("trade_time")
            close = float(row.get("close", 0))
            high = float(row.get("high", close))
            low = float(row.get("low", close))
            volume = float(row.get("vol", row.get("volume", 0)))

            brick_created = self.update(timestamp, close, high, low, volume)
            renko_signals.append({
                "brick_created": brick_created,
                "brick_direction": self.brick_direction,
                "brick_top": self.last_brick_top,
                "brick_bottom": self.last_brick_bottom,
            })

        renko_df = pd.DataFrame(renko_signals)
        result_df = pd.concat([df.reset_index(drop=True), renko_df.reset_index(drop=True)], axis=1)
        return result_df

    def get_signal(self) -> Dict:
        """获取当前 Renko 状态信号"""
        if not self.bricks or len(self.bricks) < 2:
            return {"action": "HOLD", "confidence": 0.0}

        last_brick = self.bricks[-1]
        prev_brick = self.bricks[-2] if len(self.bricks) >= 2 else last_brick

        if last_brick["direction"] == "up":
            # 向上砖 → 可能卖点（价格触及上轨）
            return {
                "action": "SELL_OPPORTUNITY",
                "direction": "up",
                "resistance": last_brick["brick_top"],
                "confidence": 0.8,
            }
        elif last_brick["direction"] == "down":
            # 向下砖 → 可能买点（价格触及下轨）
            return {
                "action": "BUY_OPPORTUNITY",
                "direction": "down",
                "support": last_brick["brick_bottom"],
                "confidence": 0.8,
            }
        else:
            return {"action": "HOLD", "confidence": 0.0}


def renko_macd_signal(df: pd.DataFrame, brick_size_pct: float = 0.003,
                       macd_column: str = "macd_hist_15m") -> Tuple[str, float]:
    """
    Renko + MACD 联合信号（论文核心）

    Args:
        df: 分钟数据 DataFrame
        brick_size_pct: 砖高
        macd_column: MACD 柱状体列名

    Returns:
        (action, confidence)
        action: "BUY_LOW", "SELL_HIGH", "HOLD"
        confidence: 0.0 ~ 1.0
    """
    if df.empty or len(df) < 5:
        return "HOLD", 0.0

    builder = RenkoBuilder(brick_size_pct=brick_size_pct)
    renko_df = builder.batch_update(df)

    # 获取最新 Renko 信号
    renko_sig = builder.get_signal()

    # 获取最新 MACD 信号
    last_row = renko_df.iloc[-1]
    macd_hist = float(last_row.get(macd_column, 0))
    macd_direction = "up" if macd_hist > 0 else "down" if macd_hist < 0 else "neutral"

    # 联合判断
    if renko_sig["action"] == "BUY_OPPORTUNITY" and macd_direction == "up":
        # Renko 向下砖 + MACD 金叉 → 强买信号
        return "BUY_LOW", 0.85

    elif renko_sig["action"] == "SELL_OPPORTUNITY" and macd_direction == "down":
        # Renko 向上砖 + MACD 死叉 → 强卖信号
        return "SELL_HIGH", 0.85

    elif renko_sig["action"] == "BUY_OPPORTUNITY":
        # 单一 Renko 买机会（MACD 中立或反向）
        return "BUY_LOW", 0.5

    elif renko_sig["action"] == "SELL_OPPORTUNITY":
        # 单一 Renko 卖机会（MACD 中立或反向）
        return "SELL_HIGH", 0.5

    else:
        return "HOLD", 0.0


if __name__ == "__main__":
    # 演示用法
    print("=" * 60)
    print("Renko 砖形图演示")
    print("=" * 60)

    # 生成模拟数据
    n = 100
    base_price = 10.0
    np.random.seed(42)
    prices = base_price + np.cumsum(np.random.normal(0.01, 0.02, n))

    df = pd.DataFrame({
        "time": pd.date_range(start="09:30", periods=n, freq="1min"),
        "close": prices,
        "high": prices + 0.01,
        "low": prices - 0.01,
        "vol": np.random.poisson(1000, n),
    })

    # 构建 Renko
    builder = RenkoBuilder(brick_size_pct=0.003)
    renko_df = builder.batch_update(df)

    print("\n📊 Renko 砖统计:")
    print(f"   总砖数: {len(builder.bricks)}")
    print(f"   向上砖: {sum(1 for b in builder.bricks if b['direction'] == 'up')}")
    print(f"   向下砖: {sum(1 for b in builder.bricks if b['direction'] == 'down')}")
    print(f"   当前方向: {builder.brick_direction}")

    print("\n🎯 最后5个砖:")
    for i, brick in enumerate(builder.bricks[-5:]):
        print(f"   {i}: {brick['direction']:5s} @ {brick['price']:.4f} "
              f"(范围: {brick['brick_bottom']:.4f} - {brick['brick_top']:.4f})")

    print("\n✅ 演示完成")
