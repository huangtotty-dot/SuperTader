# -*- coding: utf-8 -*-
"""
demo_surge_defense.py - 日内冲高防御系统演示

演示场景：
  1. 摩恩电气式"集合竞价涨停+冲高回落"
  2. 健康日内涨停场景
  3. 持续回落风险
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from intraday_surge_defense import (
    classify_daily_limit,
    detect_pullback_from_high,
    check_intraday_buypoint_quality,
    intraday_surge_defense
)
from intraday_surge_monitor import format_surge_alert


def demo_scenario_1():
    """场景1：摩恩电气式冲高回落"""
    print("\n" + "="*70)
    print("场景1: 摩恩电气 - 集合竞价涨停 + 冲高回落(11%)")
    print("="*70)

    # 模拟1分钟线数据
    import pandas as pd
    import numpy as np

    times = pd.date_range("2026-08-25 09:30", periods=60, freq="1min")

    # 构造：09:30 开盘即涨停，10:15 冲到高点8.13，后续持续回落至7.24
    prices = []
    for i, t in enumerate(times):
        if i <= 5:  # 09:30-09:35 快速涨停到 8.13
            price = 7.39 + (8.13 - 7.39) * i / 5
        elif i <= 35:  # 09:36-10:05 保持高位
            price = 8.13 - 0.02 + np.random.rand() * 0.05
        else:  # 10:06+ 持续回落
            price = 8.13 - 0.08 * (i - 35) / 25
        prices.append(price)

    df_1min = pd.DataFrame({
        "time": times,
        "open": prices,
        "high": [p + 0.02 for p in prices],
        "low": [p - 0.02 for p in prices],
        "close": prices,
        "volume": np.random.randint(1000, 5000, len(times)),
        "amount": [p * v for p, v in zip(prices, np.random.randint(1000, 5000, len(times)))],
        "close_prev": [7.39] * len(times),  # 前日收盘
    })

    # 分类
    is_limit, limit_type, reason = classify_daily_limit(df_1min)
    print(f"\n1. 涨停分类")
    print(f"   涨停: {is_limit}, 类型: {limit_type}")
    print(f"   原因: {reason}")

    # 回落检测
    pullback = detect_pullback_from_high(df_1min)
    print(f"\n2. 冲高回落")
    print(f"   高点: {pullback['high_price']} (于 {pullback['high_time']})")
    print(f"   当前: {pullback['current_price']}")
    print(f"   回落: {pullback['pullback_ratio']*100:.1f}% [{pullback['alert_level']}]")
    print(f"   说明: {pullback['reason']}")

    # 综合防御
    result = intraday_surge_defense("002451", "摩恩电气", df_1min)
    print(f"\n3. 综合防御")
    print(f"   行动: {result.action}")
    print(f"   等级: {result.alert_level}")
    print(f"   原因: {result.reason}")


def demo_scenario_2():
    """场景2：健康日内涨停"""
    print("\n" + "="*70)
    print("场景2: 健康日内涨停 - 有明确上升趋势")
    print("="*70)

    import pandas as pd
    import numpy as np

    times = pd.date_range("2026-08-25 09:30", periods=60, freq="1min")

    # 构造：09:30 开盘 5.80，逐步上升，11:50 涨停 6.39，后续缩量保持
    prices = []
    for i, t in enumerate(times):
        if i <= 30:  # 09:30-10:00 缓步上升
            price = 5.80 + (6.39 - 5.80) * i / 30
        else:  # 10:01+ 涨停并保持
            price = 6.39 - np.random.rand() * 0.01  # 略低保持
        prices.append(price)

    df_1min = pd.DataFrame({
        "time": times,
        "open": prices,
        "high": [p + 0.01 for p in prices],
        "low": [max(p - 0.02, 5.80) for p in prices],
        "close": prices,
        "volume": np.random.randint(1000, 3000, len(times)),
        "amount": [p * v for p, v in zip(prices, np.random.randint(1000, 3000, len(times)))],
        "close_prev": [5.80] * len(times),  # 前日收盘
    })

    # 分类
    is_limit, limit_type, reason = classify_daily_limit(df_1min)
    print(f"\n1. 涨停分类")
    print(f"   涨停: {is_limit}, 类型: {limit_type}")
    print(f"   原因: {reason}")

    # 回落检测
    pullback = detect_pullback_from_high(df_1min)
    print(f"\n2. 冲高回落")
    print(f"   高点: {pullback['high_price']} (于 {pullback['high_time']})")
    print(f"   当前: {pullback['current_price']}")
    print(f"   回落: {pullback['pullback_ratio']*100:.1f}% [{pullback['alert_level']}]")

    # 综合防御
    result = intraday_surge_defense("600481", "双良节能", df_1min)
    print(f"\n3. 综合防御")
    print(f"   行动: {result.action}")
    print(f"   等级: {result.alert_level}")
    print(f"   原因: {result.reason}")


def demo_scenario_3():
    """场景3：持续下跌风险"""
    print("\n" + "="*70)
    print("场景3: 持续下跌 - 无底部支撑")
    print("="*70)

    import pandas as pd
    import numpy as np

    times = pd.date_range("2026-08-25 09:30", periods=60, freq="1min")

    # 构造：开盘 6.00，快速跳水至 5.40，持续下跌
    prices = []
    for i, t in enumerate(times):
        price = 6.00 - 0.6 * i / 60  # 线性下跌
        prices.append(price)

    df_1min = pd.DataFrame({
        "time": times,
        "open": prices,
        "high": prices,
        "low": [p - 0.02 for p in prices],
        "close": prices,
        "volume": np.random.randint(1000, 5000, len(times)),
        "amount": [p * v for p, v in zip(prices, np.random.randint(1000, 5000, len(times)))],
        "close_prev": [6.00] * len(times),
    })

    # 分类
    is_limit, limit_type, reason = classify_daily_limit(df_1min)
    print(f"\n1. 涨停分类")
    print(f"   涨停: {is_limit}, 类型: {limit_type}")
    print(f"   原因: {reason}")

    # 回落检测 - 虽然无涨停，但回落本身是相对于开盘
    pullback = detect_pullback_from_high(df_1min)
    print(f"\n2. 冲高回落")
    print(f"   高点: {pullback['high_price']} (于 {pullback['high_time']})")
    print(f"   当前: {pullback['current_price']}")
    print(f"   回落: {pullback['pullback_ratio']*100:.1f}% [{pullback['alert_level']}]")

    # 综合防御
    result = intraday_surge_defense("000988", "样本下跌", df_1min)
    print(f"\n3. 综合防御")
    print(f"   行动: {result.action}")
    print(f"   等级: {result.alert_level}")
    print(f"   原因: {result.reason}")


if __name__ == "__main__":
    print("\n" + "="*70)
    print("日内冲高防御系统 - 演示")
    print("="*70)

    demo_scenario_1()
    demo_scenario_2()
    demo_scenario_3()

    print("\n" + "="*70)
    print("演示完成")
    print("="*70)
    print("\n💡 使用建议:")
    print("  1. 集合竞价涨停 → 不追高，等待冲高回落+缩量确认")
    print("  2. 日内涨停 + 有上升趋势 → 相对安全")
    print("  3. 冲高回落 > 10% → 建议止损或减仓")
    print("  4. 每5-10分钟监控一次持仓风险")
    print()
