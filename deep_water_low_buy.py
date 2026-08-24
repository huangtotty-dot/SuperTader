#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P24-02: 深水低吸模式 (Deep Water Low-Buy)

问题背景 (2026-08-24复盘):
  600481 (双良节能) 振幅 3.87% (4.29→4.13)
  但全天0信号，原因：
    - 价格缓跌（非急跌），不触及布林下轨（bb_pct ≤ 0.15）
    - 15分MACD可能持续死叉（m15 ≤ 0）
    - RSI 从71降至60，未进入超卖区(<40)

解决方案 (Deep Water Low-Buy):
  当主策略（纯两点：bb_pct_5m触轨 + rsi_5m_p6）无法生成BUY_LOW信号时，
  作为fallback模式，检查"深水低吸"条件：
    1. 日跌幅 > 3% （缓跌但有深度）
    2. 价格接近当日低点（±1%内）
    3. RSI(14) < 45 （进入弱势区）
    4. 距离MA5 < -2% （偏离短均线下方）

  满足所有条件 → 生成 BUY_LOW_DEEP_WATER 信号（评分 70/100）

实现方式:
  - 作为 signal_engine.py 的补充模块
  - 在 evaluate_swing 返回 HOLD 时触发
  - 生成的信号评分低于主策略（70 vs 100），区分来源
  - 可通过 config.py 的 PARAMS["enable_deep_water_mode"] 控制

参数配置（config.py）:
  "enable_deep_water_mode": True,
  "deep_water_daily_drop": 0.03,       # 日跌幅阈值 3%
  "deep_water_low_proximity": 0.01,    # 接近低点 ±1%
  "deep_water_rsi_max": 45.0,          # RSI<45
  "deep_water_ma5_deviation": -0.02,   # 距MA5 < -2%
  "deep_water_signal_score": 70.0,     # 评分 70
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
from datetime import datetime
import json
import pandas as pd
import numpy as np
from collections import defaultdict

BASE = Path(__file__).resolve().parent


class DeepWaterLowBuyMode:
    """深水低吸模式——缓跌型标的补充策略."""

    def __init__(self):
        self.enabled = False
        self.params = self._load_params()

    def _load_params(self) -> dict:
        """从config.py加载参数."""
        try:
            from config import PARAMS
            return {
                "enabled": PARAMS.get("enable_deep_water_mode", False),
                "daily_drop": PARAMS.get("deep_water_daily_drop", 0.03),
                "low_proximity": PARAMS.get("deep_water_low_proximity", 0.01),
                "rsi_max": PARAMS.get("deep_water_rsi_max", 45.0),
                "ma5_deviation": PARAMS.get("deep_water_ma5_deviation", -0.02),
                "signal_score": PARAMS.get("deep_water_signal_score", 70.0),
            }
        except Exception:
            return {
                "enabled": False,
                "daily_drop": 0.03,
                "low_proximity": 0.01,
                "rsi_max": 45.0,
                "ma5_deviation": -0.02,
                "signal_score": 70.0,
            }

    def check_deep_water_signal(self, code: str, df_daily: pd.DataFrame,
                               df_5min: pd.DataFrame) -> dict:
        """
        检查是否满足深水低吸条件.

        参数:
          code: 股票代码
          df_daily: 日K线数据
          df_5min: 5分钟K线数据

        返回:
          {
            "trigger": bool,          # 是否触发深水模式
            "reason": str,            # 触发原因或未触发原因
            "daily_drop": float,      # 当日跌幅
            "low_proximity": float,   # 距低点百分比
            "rsi_14": float,          # RSI(14)值
            "ma5_deviation": float,   # 距MA5偏离
            "score": float,           # 建议评分
          }
        """
        result = {
            "trigger": False,
            "reason": "未满足深水低吸条件",
            "daily_drop": 0,
            "low_proximity": 0,
            "rsi_14": 50,
            "ma5_deviation": 0,
            "score": 0,
        }

        if not self.params["enabled"]:
            result["reason"] = "深水模式未启用"
            return result

        if df_daily is None or df_daily.empty:
            result["reason"] = "日线数据不足"
            return result

        if df_5min is None or df_5min.empty:
            result["reason"] = "5分钟数据不足"
            return result

        try:
            # 获取当日数据
            today = df_daily.iloc[-1]
            open_price = float(today.get("open", 0))
            close_price = float(today.get("close", 0))
            low_price = float(today.get("low", 0))
            high_price = float(today.get("high", 0))

            if open_price == 0 or close_price == 0 or low_price == 0:
                result["reason"] = "价格数据缺失"
                return result

            # 1. 日跌幅检查
            daily_drop = (open_price - close_price) / open_price if open_price > 0 else 0
            result["daily_drop"] = daily_drop

            if daily_drop < self.params["daily_drop"]:
                result["reason"] = f"日跌幅{daily_drop:.2%} < {self.params['daily_drop']:.2%}，不满足"
                return result

            # 2. 接近低点检查
            low_proximity = (low_price - close_price) / low_price if low_price > 0 else 1.0
            result["low_proximity"] = low_proximity

            if low_proximity > self.params["low_proximity"]:
                result["reason"] = f"距低点{low_proximity:.2%} > {self.params['low_proximity']:.2%}，尚未到底"
                return result

            # 3. RSI(14) 检查
            if len(df_daily) >= 14:
                rsi_14 = self._calculate_rsi(df_daily["close"], 14).iloc[-1]
                result["rsi_14"] = rsi_14 if not pd.isna(rsi_14) else 50

                if result["rsi_14"] >= self.params["rsi_max"]:
                    result["reason"] = f"RSI(14)={result['rsi_14']:.0f} >= {self.params['rsi_max']:.0f}，未进入弱势"
                    return result
            else:
                result["reason"] = "日线数据<14根，无法计算RSI(14)"
                return result

            # 4. 距MA5偏离检查
            if len(df_daily) >= 5:
                ma5 = df_daily["close"].rolling(5).mean().iloc[-1]
                if not pd.isna(ma5) and ma5 > 0:
                    ma5_deviation = (close_price - ma5) / ma5
                    result["ma5_deviation"] = ma5_deviation

                    if ma5_deviation > self.params["ma5_deviation"]:
                        result["reason"] = f"距MA5偏离{ma5_deviation:.2%} > {self.params['ma5_deviation']:.2%}，未充分下离"
                        return result
                else:
                    result["reason"] = "MA5计算异常"
                    return result
            else:
                result["reason"] = "日线数据<5根，无法计算MA5"
                return result

            # 所有条件均满足
            result["trigger"] = True
            result["reason"] = f"深水低吸触发：跌{daily_drop:.2%}|近低{low_proximity:.2%}|RSI{result['rsi_14']:.0f}|MA5{ma5_deviation:.2%}"
            result["score"] = self.params["signal_score"]

            return result

        except Exception as e:
            result["reason"] = f"计算异常: {str(e)[:50]}"
            return result

    @staticmethod
    def _calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """计算RSI."""
        try:
            delta = prices.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            return rsi
        except Exception:
            return pd.Series([50] * len(prices))


class DeepWaterValidator:
    """深水模式回测验证."""

    @staticmethod
    def validate_600481_history(months_back: int = 6):
        """验证600481的历史是否能被深水模式捕获."""
        print("\n【P24-02 深水低吸模式验证】600481 (双良节能)")
        print("=" * 90)

        try:
            import tushare as ts
            pro = ts.pro_api()

            # 拉取日线数据
            end_date = datetime.now()
            start_date = end_date - pd.Timedelta(days=months_back*30)

            print(f"期间: {start_date.date()} ~ {end_date.date()}")

            df = pro.daily(ts_code="600481.SH",
                         start_date=start_date.strftime("%Y%m%d"),
                         end_date=end_date.strftime("%Y%m%d"))

            if df is None or df.empty:
                print("❌ 数据拉取失败")
                return

            df = df.sort_values("trade_date").reset_index(drop=True)
            print(f"✓ 获取 {len(df)} 个交易日")

            # 初始化模式
            mode = DeepWaterLowBuyMode()
            mode.params["enabled"] = True

            # 检查每一天
            deep_water_signals = []
            large_drop_days = []

            for idx in range(1, len(df)):
                row = df.iloc[idx]
                open_p = float(row["open"])
                close_p = float(row["close"])
                low_p = float(row["low"])

                # 日跌幅
                daily_drop = (open_p - close_p) / open_p if open_p > 0 else 0

                if daily_drop > 0.03:  # 跌幅>3%
                    large_drop_days.append({
                        "date": row["trade_date"],
                        "drop": daily_drop,
                        "close": close_p,
                        "low": low_p,
                    })

                # 检查深水条件
                result = mode.check_deep_water_signal("600481.SH", df.iloc[:idx+1], None)

                if result["trigger"]:
                    deep_water_signals.append({
                        "date": row["trade_date"],
                        "close": close_p,
                        "rsi_14": result["rsi_14"],
                        "daily_drop": result["daily_drop"],
                        "reason": result["reason"],
                    })

            # 输出结果
            print(f"\n超过3%跌幅的交易日: {len(large_drop_days)}")
            for day in large_drop_days[-5:]:
                print(f"  {day['date']}: 跌幅 {day['drop']:.2%} 收{day['close']:.2f} 低{day['low']:.2f}")

            print(f"\n深水低吸信号触发: {len(deep_water_signals)} 次")
            if deep_water_signals:
                print("  最近信号:")
                for sig in deep_water_signals[-5:]:
                    print(f"    {sig['date']}: RSI {sig['rsi_14']:.0f}, "
                          f"跌幅{sig['daily_drop']:.2%} - {sig['reason'][:50]}")

            # 评估效果
            print(f"\n【评估】")
            if len(deep_water_signals) > 0:
                print(f"✓ 深水模式可以捕获 {len(deep_water_signals)} 个机会")
                print(f"  覆盖率: {100*len(deep_water_signals)/max(len(large_drop_days),1):.1f}% "
                      f"(相对{len(large_drop_days)}个大跌日)")
            else:
                print(f"❌ 深水模式未捕获任何机会（6个月内）")
                print(f"  原因: 可能缺乏同时满足所有条件的日期")

        except Exception as e:
            print(f"❌ 验证失败: {e}")


def main():
    """主入口."""
    print("\n" + "=" * 90)
    print("【P24-02 深水低吸模式】设计与验证")
    print("=" * 90)

    print("\n【方案设计】")
    print("""
    问题: 600481(双良节能)缓跌型标的，3.87%振幅但全天0信号

    原因: 主策略(纯两点)无法覆盖缓跌型:
      - 布林下轨触发需要急跌 (bb_pct ≤ 0.15)
      - 缓跌只能降低 bb_pct 但不触及下轨
      - RSI 需要<40 超卖，缓跌中 RSI 从71降至60仍无法触发

    解决: 新增"深水低吸"模式
      1. 日跌幅 > 3% (缓跌但有深度)
      2. 价格接近当日低点 (±1%内)
      3. RSI(14) < 45 (进入弱势区)
      4. 距MA5 < -2% (偏离短均线下方)

      满足所有条件 → BUY_LOW_DEEP_WATER (评分70/100)

    优势:
      ✓ 覆盖缓跌型标的的底部机会
      ✓ 评分低于主策略(70 vs 100)，区分来源
      ✓ 降级推送（二阶段拦截仍可过滤虚假信号）
      ✓ 可通过参数调整灵活控制

    风险:
      ⚠️ 抄底在半山腰（可能继续跌后再跌）
      ⚠️ RSI<45仍为弱势未深度超卖，胜率需验证
      ⚠️ 建议组合使用：低吸后设定止损 3% 以内
    """)

    # 验证600481历史数据
    validator = DeepWaterValidator()
    validator.validate_600481_history(months_back=6)

    print("\n" + "=" * 90)
    print("【实现建议】")
    print("=" * 90)
    print("""
    Step 1: 在 signal_engine.py 中添加 DeepWaterLowBuyMode 模块

    Step 2: 在 evaluate_swing() 返回 HOLD 时触发:

      if decision == "HOLD":
          deep_water = DeepWaterLowBuyMode()
          dw_result = deep_water.check_deep_water_signal(code, df_daily, df_5min)
          if dw_result["trigger"]:
              decision = "BUY_LOW"
              score = dw_result["score"]  # 70 而非 100
              reason = f"深水低吸: {dw_result['reason']}"

    Step 3: 在 config.py 中配置参数:

      "enable_deep_water_mode": True,
      "deep_water_daily_drop": 0.03,
      "deep_water_low_proximity": 0.01,
      "deep_water_rsi_max": 45.0,
      "deep_water_ma5_deviation": -0.02,
      "deep_water_signal_score": 70.0,

    Step 4: 后续可按标的启用/禁用:

      STOCK_PARAMS["600481"] = {
          "enable_deep_water_mode": True,  # 对缓跌型激进
      }
      STOCK_PARAMS["588170"] = {
          "enable_deep_water_mode": False,  # 对ETF保守
      }

    Step 5: 验证与观察:
      - 回测: 检查6个月内有多少个机会被捕获
      - 实盘: 观察3日虚假信号增加量
      - 若虚假信号 > 5%，调整参数或按标的禁用
    """)


if __name__ == "__main__":
    main()
