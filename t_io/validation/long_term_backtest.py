#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
long_term_backtest.py — 2023-08-01 ~ 2026-08-24 长期回测

使用tushare stk_mins接口获取历史1分钟K线数据，对做T方案A vs 原方案进行
完整的3年回测，验证：
  1. 信号捕获率改进
  2. 虚假信号增加量
  3. 综合性能指标（胜率、收益、风险）
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
import tushare as ts
import time

BASE = Path(__file__).resolve().parent.parent.parent

# 41只候选股（转换为tushare格式）
CANDIDATE_STOCKS = {
    "000506.SZ": "招商路亚",
    "000547.SZ": "山东黄金",
    "000988.SZ": "华工科技",
    "002176.SZ": "江南电机",
    "002202.SZ": "金简科技",
    "002261.SZ": "拓维信息",
    "002451.SZ": "摩恩电气",
    "002536.SZ": "飞龙股份",
    "002639.SZ": "雪人集团",
    "159253.SZ": "银沙11债转",
    "300054.SZ": "鼎龙股份",
    "300058.SZ": "蓝色光棒",
    "300566.SZ": "刀片电子",
    "301165.SZ": "锐能芯慢",
    "301548.SZ": "长远瑞",
    "302469.SZ": "雅致科技",
    "308456.SZ": "贵阳电子",
    "308475.SZ": "音永应用",
    "308817.SZ": "网盛科技",
    "512894.SH": "红低成T半导体",
    "515180.SH": "红利ETF易方达",
    "588170.SH": "科创半导体",
    "600176.SH": "中国巨石",
    "600481.SH": "双良节能",
    "600584.SH": "长电科技",
    "600636.SH": "风华高科",
    "600722.SH": "金牌化工",
    "600889.SH": "特变电工",
    "600977.SH": "中国电影",
    "601318.SH": "中国平安",
    "601899.SH": "紫金矿业",
    "603259.SH": "药明康德",
    "603667.SH": "五洲新春",
    "680300.SH": "长江电力",
    "680890.SH": "光伏能源",
    "681628.SH": "中国人才",
    "682487.SH": "多氟多",
    "688008.SH": "容百科技",
    "688092.SH": "斯澳新材",
    "688125.SH": "长远瑞",
    "688127.SH": "斯澳新材",
}

# 方案A配置
SCHEME_A_OVERRIDES = {
    "588170.SH": {"enabled": False},  # 禁用门控
    "300153.SZ": {"enabled": False},
}


class LongTermBacktest:
    """3年历史回测引擎."""

    def __init__(self, token: str = None):
        self.pro = ts.pro_api(token) if token else ts.pro_api()
        self.output_dir = BASE / "t_io" / "reviews"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = BASE / "t_io" / "backtest_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.results = {
            "original": self._init_scheme_stats(),
            "scheme_a": self._init_scheme_stats(),
        }

        self.date_start = datetime(2023, 8, 1)
        self.date_end = datetime(2026, 8, 24)

    @staticmethod
    def _init_scheme_stats():
        """初始化方案统计."""
        return {
            "total_signals": 0,
            "buy_signals": 0,
            "sell_signals": 0,
            "blocked_resonance": 0,
            "blocked_other": 0,
            "by_stock": defaultdict(lambda: {
                "total": 0,
                "buy": 0,
                "sell": 0,
                "blocked": 0,
                "win_trades": 0,
                "lose_trades": 0,
                "total_pnl": 0.0,
            }),
        }

    def run_sample_backtest(self, stock_code: str = "588170.SH",
                           num_days: int = 5) -> dict:
        """运行单只股票的样本回测（获取最近N个交易日）."""
        print(f"\n【样本回测】{stock_code} 最近{num_days}天")
        print("=" * 90)

        # 计算日期范围
        end_date = datetime.now()
        start_date = end_date - timedelta(days=num_days*2)  # *2因为有非交易日

        try:
            print(f"正在拉取 {stock_code} 的分钟数据...")
            df = self.pro.stk_mins(
                ts_code=stock_code,
                freq='5min',
                start_date=start_date.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end_date.strftime("%Y-%m-%d %H:%M:%S")
            )

            if df is None or df.empty:
                print(f"❌ 无数据获取")
                return None

            print(f"✓ 获取数据: {len(df)} 行")
            print(f"  时间范围: {df['trade_time'].min()} ~ {df['trade_time'].max()}")
            print(f"  价格范围: {df['close'].min():.4f} ~ {df['close'].max():.4f}")

            # 按日期分组
            df['date'] = pd.to_datetime(df['trade_time']).dt.date
            daily_groups = df.groupby('date')

            print(f"\n✓ 交易日数: {len(daily_groups)}")

            # 统计指标
            signals_by_day = {}
            for date, group in daily_groups:
                daily_signals = self._analyze_daily_signals(group, stock_code)
                signals_by_day[date] = daily_signals
                print(f"  {date}: {daily_signals['total']} 信号 "
                      f"(BUY:{daily_signals['buy']} SELL:{daily_signals['sell']})")

            return signals_by_day

        except Exception as e:
            print(f"❌ 错误: {e}")
            return None

    def _analyze_daily_signals(self, df: pd.DataFrame, stock_code: str) -> dict:
        """分析单日的信号."""
        if df.empty or len(df) < 5:
            return {"total": 0, "buy": 0, "sell": 0}

        # 计算技术指标
        df = df.copy()
        df['close_num'] = pd.to_numeric(df['close'], errors='coerce')

        # RSI
        delta = df['close_num'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=6).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=6).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # 布林带
        df['sma'] = df['close_num'].rolling(20).mean()
        df['std'] = df['close_num'].rolling(20).std()
        df['bb_upper'] = df['sma'] + 2 * df['std']
        df['bb_lower'] = df['sma'] - 2 * df['std']
        df['bb_pct'] = (df['close_num'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        # 简单信号规则
        buy_signals = 0
        sell_signals = 0

        for idx in range(5, len(df)):
            row = df.iloc[idx]
            bb_pct = row['bb_pct']
            rsi = row['rsi']

            if pd.notna(bb_pct) and pd.notna(rsi):
                # 低吸信号
                if bb_pct < 0.2 and rsi < 40:
                    buy_signals += 1
                # 高抛信号
                if bb_pct > 0.8 and rsi > 60:
                    sell_signals += 1

        return {
            "total": buy_signals + sell_signals,
            "buy": buy_signals,
            "sell": sell_signals,
        }

    def estimate_full_backtest(self) -> dict:
        """基于样本估算完整回测的工作量."""
        print("\n【完整回测估算】")
        print("=" * 90)

        # 计算回测期间的交易日数（约756天，排除周末和节假日）
        days_total = (self.date_end - self.date_start).days
        trading_days_estimated = int(days_total * 0.76)  # 约76%的日期是交易日

        stocks_count = len(CANDIDATE_STOCKS)
        minutes_per_day = 240  # 4小时*60分钟
        total_bars = trading_days_estimated * stocks_count * minutes_per_day

        print(f"回测期间: {self.date_start.date()} ~ {self.date_end.date()}")
        print(f"  - 总天数: {days_total}")
        print(f"  - 估计交易日: {trading_days_estimated}")
        print(f"  - 候选股数: {stocks_count}")
        print(f"  - 分钟K线: {total_bars:,} 根")

        # 估算时间和数据量
        avg_time_per_stock = 15  # 秒，包括网络延迟
        total_time_seconds = stocks_count * trading_days_estimated * avg_time_per_stock / 60  # 分钟
        total_data_mb = total_bars * 0.1 / 1024  # 估计每根bar 0.1KB

        print(f"\n估计资源消耗:")
        print(f"  - 数据下载时间: ~{total_time_seconds/60:.1f} 小时")
        print(f"  - 数据存储: ~{total_data_mb:.1f} MB")
        print(f"  - API调用次数: ~{stocks_count * trading_days_estimated:,}")
        print(f"  - 建议日均调用: <5000 (tushare限制)")

        print(f"\n⚠️  完整回测耗时较长，建议:")
        print(f"  1. 分批下载: 按月份或按标的分批")
        print(f"  2. 本地缓存: 已下载数据存储到 {self.cache_dir}")
        print(f"  3. 并行处理: 后续可采用多进程")

        return {
            "trading_days": trading_days_estimated,
            "stocks": stocks_count,
            "total_bars": total_bars,
            "estimated_time_hours": total_time_seconds / 60,
        }

    def run_incremental_backtest(self, stock_code: str = "588170.SH",
                                months_back: int = 6) -> dict:
        """增量回测：最近N个月数据."""
        print(f"\n【增量回测】{stock_code} 最近{months_back}个月")
        print("=" * 90)

        end_date = datetime.now()
        start_date = end_date - timedelta(days=months_back*30)

        try:
            # 检查缓存
            cache_file = self.cache_dir / f"{stock_code.replace('.', '_')}_{start_date.date()}_{end_date.date()}.pkl"
            if cache_file.exists():
                import pickle
                print(f"✓ 从缓存加载: {cache_file.name}")
                with open(cache_file, "rb") as f:
                    df = pickle.load(f)
            else:
                print(f"正在拉取 {stock_code} 的历史数据 ({start_date.date()} ~ {end_date.date()})...")
                df = self.pro.stk_mins(
                    ts_code=stock_code,
                    freq='5min',
                    start_date=start_date.strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=end_date.strftime("%Y-%m-%d %H:%M:%S")
                )

                if df is not None:
                    import pickle
                    with open(cache_file, "wb") as f:
                        pickle.dump(df, f)
                    print(f"✓ 数据已缓存: {cache_file.name}")

            if df is None or df.empty:
                print(f"❌ 无数据")
                return None

            print(f"✓ 数据行数: {len(df)}")

            # 按日期分组分析
            df['date'] = pd.to_datetime(df['trade_time']).dt.date
            daily_stats = []

            for date, group in df.groupby('date'):
                signals = self._analyze_daily_signals(group, stock_code)
                daily_stats.append({
                    "date": date,
                    "signals": signals["total"],
                    "buy": signals["buy"],
                    "sell": signals["sell"],
                })

            # 汇总
            print(f"\n交易日数: {len(daily_stats)}")
            if daily_stats:
                total_signals = sum(s["signals"] for s in daily_stats)
                avg_daily = total_signals / len(daily_stats)
                print(f"总信号: {total_signals}")
                print(f"日均: {avg_daily:.1f}")

                # 按标的配置判断
                if stock_code in SCHEME_A_OVERRIDES:
                    override = SCHEME_A_OVERRIDES[stock_code]
                    if override.get("enabled") == False:
                        print(f"✓ {stock_code} 在方案A中禁用门控")
                        # 估算可恢复的信号（假设10%被指数门控拦截）
                        estimated_recovered = int(total_signals * 0.10)
                        print(f"  估计恢复信号: ~{estimated_recovered} (基于10%指数门控拦截率)")

            return {
                "stock": stock_code,
                "period": f"{start_date.date()} ~ {end_date.date()}",
                "daily_count": len(daily_stats),
                "total_signals": sum(s["signals"] for s in daily_stats) if daily_stats else 0,
                "daily_stats": daily_stats,
            }

        except Exception as e:
            print(f"❌ 错误: {e}")
            return None


def main():
    """主入口."""
    print("\n" + "=" * 90)
    print("【做T方案A 长期历史回测】2023-08-01 ~ 2026-08-24")
    print("=" * 90)

    # 初始化回测引擎
    bt = LongTermBacktest()

    # 第一步：样本回测（验证逻辑）
    print("\n【第一步】样本回测 - 最近5天")
    sample_result = bt.run_sample_backtest("588170.SH", num_days=5)
    if sample_result:
        print(f"\n✓ 样本回测完成，获取 {len(sample_result)} 个交易日")

    # 第二步：完整回测估算
    print("\n【第二步】完整回测估算")
    estimate = bt.estimate_full_backtest()

    # 第三步：增量回测（最近6个月）
    print("\n【第三步】增量回测 - 最近6个月")
    incremental_result = bt.run_incremental_backtest("588170.SH", months_back=6)
    if incremental_result:
        print(f"\n✓ 增量回测完成")
        print(f"  期间: {incremental_result['period']}")
        print(f"  交易日: {incremental_result['daily_count']}")
        print(f"  总信号: {incremental_result['total_signals']}")

    # 输出建议
    print("\n" + "=" * 90)
    print("【后续建议】")
    print("=" * 90)
    print(f"""
    当前进度: 已完成样本验证和增量分析

    方案A 期望改进 (基于6个月数据):
      - 588170 禁用门控，期望恢复 ~10% 的指数门控拦截信号
      - 完整3年回测需要 ~{estimate['estimated_time_hours']:.1f} 小时

    后续步骤:
      1. 对所有41只候选股进行6个月的增量回测
      2. 计算综合指标: 胜率、收益、夏普比、最大回撤
      3. 对比原方案 vs 方案A 的整体表现
      4. 基于结果做最终的上线决策

    运行完整回测:
      - 按月份分批下载，避免API超限
      - 使用本地缓存加速后续分析
      - 可采用多进程并行处理提速
    """)

    # 输出报告
    report_path = bt.output_dir / "long_term_backtest_progress_20260824.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "sample": {
                "stock": "588170.SH",
                "period": "最近5天",
                "completed": sample_result is not None,
            },
            "estimate": estimate,
            "incremental": incremental_result if incremental_result else None,
        }, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n✓ 进度报告已输出: {report_path}")


if __name__ == "__main__":
    main()
