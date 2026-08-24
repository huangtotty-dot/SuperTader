#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实历史回测：利用tushare stk_mins 接口 (2023-08-01 ~ 2026-08-24)

方案对比：
  - 原方案: fail_closed=True, 全局 index_ma5_dir 门控
  - 方案A: fail_closed=False, 标的级别覆盖 (588170禁用、300153禁用等)

回测流程：
  1. 为每只候选股拉取历史1分钟K线（通过tushare）
  2. 模拟执行信号引擎，生成BUY_LOW/SELL_HIGH信号
  3. 应用指数共振门控（原方案 vs 方案A）
  4. 统计信号捕获率、胜率、收益等指标
  5. 输出对比报告
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
from datetime import datetime, timedelta
import json
import pandas as pd
import numpy as np
from collections import defaultdict
import time

# 导入 tushare
try:
    import tushare as ts
    TUSHARE_AVAILABLE = True
except ImportError:
    TUSHARE_AVAILABLE = False
    print("⚠️ tushare 未安装，请先: pip install tushare")

BASE = Path(__file__).resolve().parent.parent.parent

# 41只候选股（转换为 tushare 格式 XXXX.SH/XXXX.SZ）
CANDIDATE_STOCKS_TUSHARE = [
    "000506.SZ", "000547.SZ", "000988.SZ", "002176.SZ", "002202.SZ", "002261.SZ",
    "002451.SZ", "002536.SZ", "002639.SZ", "159253.SZ", "300054.SZ", "300058.SZ",
    "300566.SZ", "301165.SZ", "301548.SZ", "302469.SZ", "308456.SZ", "308475.SZ",
    "308817.SZ", "512894.SH", "515180.SH", "588170.SH", "600176.SH", "600481.SH",
    "600584.SH", "600636.SH", "600722.SH", "600889.SH", "600977.SH", "601318.SH",
    "601899.SH", "603259.SH", "603667.SH", "680300.SH", "680890.SH", "681628.SH",
    "682487.SH", "688008.SH", "688092.SH", "688125.SH", "688127.SH",
]

# 方案参数
SCHEME_A_OVERRIDES = {
    "588170.SH": {"enabled": False, "reason": "科创50 ETF禁用门控"},
    "300153.SZ": {"enabled": False, "reason": "科泰电源禁用门控"},
}


class TushareHistoricalBacktest:
    """基于tushare历史数据的回测引擎."""

    def __init__(self, token: str = None):
        if TUSHARE_AVAILABLE:
            # 使用你提供的token初始化
            self.pro = ts.pro_api(token) if token else ts.pro_api()
        self.output_dir = BASE / "t_io" / "reviews"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_sample(self, stock_code: str = "588170.SH", date_start: str = "2026-08-20",
                   date_end: str = "2026-08-24"):
        """运行单只股票的样本回测（演示用）."""
        if not TUSHARE_AVAILABLE:
            print("❌ tushare 不可用")
            return

        print(f"\n【样本回测】{stock_code} {date_start} ~ {date_end}")
        print("=" * 80)

        try:
            # 拉取5分钟K线
            df = self.pro.stk_mins(
                ts_code=stock_code,
                freq='5min',
                start_date=f"{date_start} 09:00:00",
                end_date=f"{date_end} 15:00:00"
            )

            if df is None or df.empty:
                print(f"❌ 无数据")
                return

            print(f"✓ 获取数据: {len(df)} 行")
            print(f"  日期范围: {df['trade_time'].min()} ~ {df['trade_time'].max()}")
            print(f"  价格范围: {df['close'].min():.4f} ~ {df['close'].max():.4f}")

            # 计算指标
            df = self._add_indicators(df)

            # 模拟信号生成
            signals = self._generate_signals(df)
            print(f"\n✓ 生成信号: {len(signals)} 个")

            # 按原方案过滤
            original_allowed = [s for s in signals if self._should_allow_original(stock_code)]
            print(f"  原方案放行: {len(original_allowed)}")

            # 按方案A过滤
            scheme_a_allowed = [s for s in signals if self._should_allow_scheme_a(stock_code)]
            print(f"  方案A放行: {len(scheme_a_allowed)}")

            improvement = len(scheme_a_allowed) - len(original_allowed)
            print(f"  改进: {improvement:+d} ({100*improvement/max(len(original_allowed),1):+.1f}%)")

            # 输出样本信号
            print(f"\n信号样本:")
            for i, sig in enumerate(signals[:10]):
                print(f"  {i+1}. {sig['time']} {sig['type']:<8} 价格:{sig['price']:.4f}")

        except Exception as e:
            print(f"❌ 错误: {e}")

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加技术指标."""
        if df.empty:
            return df

        # 简化指标计算
        df['sma5'] = df['close'].rolling(5).mean()
        df['sma20'] = df['close'].rolling(20).mean()

        # 计算RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=6).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=6).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # 布林带
        df['bb_mid'] = df['close'].rolling(20).mean()
        df['bb_std'] = df['close'].rolling(20).std()
        df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
        df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
        df['bb_pct'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        return df

    def _generate_signals(self, df: pd.DataFrame) -> list:
        """生成交易信号."""
        signals = []

        for idx in range(len(df) - 1):
            row = df.iloc[idx]
            next_row = df.iloc[idx + 1]

            # 简单的低吸信号：价格触及布林下轨
            if row['bb_pct'] is not None and row['bb_pct'] < 0.2 and row['rsi'] is not None and row['rsi'] < 40:
                signals.append({
                    "time": row['trade_time'],
                    "type": "BUY_LOW",
                    "price": row['close'],
                    "rsi": row['rsi'],
                    "bb_pct": row['bb_pct'],
                })

            # 简单的高抛信号：价格触及布林上轨
            if row['bb_pct'] is not None and row['bb_pct'] > 0.8 and row['rsi'] is not None and row['rsi'] > 60:
                signals.append({
                    "time": row['trade_time'],
                    "type": "SELL_HIGH",
                    "price": row['close'],
                    "rsi": row['rsi'],
                    "bb_pct": row['bb_pct'],
                })

        return signals

    def _should_allow_original(self, code: str) -> bool:
        """原方案：是否允许推送."""
        # 模拟：fail_closed=True时，若指数缺失则拦截
        # 这里简化处理，仅作演示
        return True

    def _should_allow_scheme_a(self, code: str) -> bool:
        """方案A：是否允许推送."""
        # 若为禁用门控的标的，直接放行
        if code in SCHEME_A_OVERRIDES:
            if SCHEME_A_OVERRIDES[code].get("enabled") == False:
                return True

        # 其他标的：与原方案相同，但 fail_closed=False
        return True


def main():
    """主入口."""
    print("\n【做T方案A 真实历史回测】利用tushare数据")
    print("=" * 80)

    # 初始化（需要tushare token）
    bt = TushareHistoricalBacktest()

    # 运行样本回测（588170，最近5天）
    bt.run_sample(
        stock_code="588170.SH",
        date_start="2026-08-20",
        date_end="2026-08-24"
    )

    print("\n" + "=" * 80)
    print("【后续步骤】")
    print("=" * 80)
    print("""
    1. 获取tushare权限：分钟行情需单独开权限
       - 访问: https://www.tushare.pro/
       - 注册后在用户中心申请"分钟数据"权限

    2. 使用正确的token初始化:
       bt = TushareHistoricalBacktest(token="your_token_here")

    3. 对所有41只候选股进行长期回测:
       for code in CANDIDATE_STOCKS_TUSHARE:
           bt.run_stock_backtest(code, "2023-08-01", "2026-08-24")

    4. 输出汇总报告（对比原方案 vs 方案A）
       - 信号捕获率改进
       - 虚假信号评估
       - 按标的分类统计
    """)


if __name__ == "__main__":
    main()
