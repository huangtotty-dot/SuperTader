# -*- coding: utf-8 -*-
"""
多周期数据获取器 MultiTimeframeFetcher
基于腾讯快照QT接口，获取日线/周线/月线数据，构建多周期上下文
用于 signal_engine 做T决策时的趋势判断与风险评估

V1.0 - 2026-07-06
"""
import os
import json
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

try:
    import pandas as pd
except ImportError:
    pd = None

# 兼容共享命名空间模式
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class MultiTimeframeFetcher:
    """多周期数据获取器：基于腾讯快照QT接口，获取日线/周线/月线数据"""
    
    BASE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    
    def __init__(self, cache_dir: str = None, cache_ttl_hours: int = 4):
        self.cache_dir = cache_dir or os.path.join(BASE_DIR, "t_io", "cache", "multi_tf")
        self.cache_ttl_hours = cache_ttl_hours
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def _qt_code(self, code: str) -> str:
        """将A股代码转换为腾讯接口格式"""
        if code.startswith(("5", "6", "9", "588")):
            return f"sh{code}"
        else:
            return f"sz{code}"
    
    def fetch_kline(self, code: str, period: str = "day", count: int = 60) -> pd.DataFrame:
        """获取历史K线数据
        
        Args:
            code: A股代码（如 588170, 300666）
            period: 周期（day/week/month）
            count: 获取条数
        
        Returns:
            DataFrame: columns=[date, open, close, high, low, volume]
        """
        if pd is None:
            return pd.DataFrame()
        
        # 1. 检查缓存
        cache_file = os.path.join(self.cache_dir, f"{code}_{period}_{count}.json")
        if os.path.exists(cache_file):
            try:
                age = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_file))).total_seconds() / 3600
                if age < self.cache_ttl_hours:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return pd.DataFrame(data)
            except Exception:
                pass
        
        # 2. 调用腾讯接口
        qt_code = self._qt_code(code)
        
        # 计算日期范围
        now = datetime.now()
        if period == "day":
            start = (now - timedelta(days=count * 2)).strftime("%Y-%m-%d")
        elif period == "week":
            start = (now - timedelta(weeks=count * 2)).strftime("%Y-%m-%d")
        else:  # month
            start = (now - timedelta(days=count * 60)).strftime("%Y-%m-%d")
        
        end = now.strftime("%Y-%m-%d")
        param = f"{qt_code},{period},{start},{end},{count},qfq"
        
        try:
            r = requests.get(self.BASE_URL, params={"param": param}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            data = r.json()
            bars = data.get("data", {}).get(qt_code, {}).get(period, [])
            
            if not bars:
                return pd.DataFrame()
            
            # 解析腾讯格式 [date, open, close, high, low, volume]
            df = pd.DataFrame(bars, columns=["date", "open", "close", "high", "low", "volume"])
            for col in ["open", "close", "high", "low", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = df["date"].astype(str).str.slice(0, 10)
            df = df.dropna().sort_values("date").reset_index(drop=True)
            
            # 3. 写入缓存
            try:
                df.to_json(cache_file, orient="records", force_ascii=False)
            except Exception:
                pass
            
            return df
        except Exception as e:
            return pd.DataFrame()
    
    def build_context(self, code: str) -> "MultiTimeframeContext":
        """构建多周期上下文"""
        return MultiTimeframeContext(code, self)


@dataclass
class MultiTimeframeContext:
    """多周期上下文：封装日线/周线/月线的关键指标"""
    
    code: str
    fetcher: MultiTimeframeFetcher
    
    # 日线数据
    daily_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    daily_prev_ret: float = 0.0          # 前日涨跌
    daily_prev2_ret: float = 0.0         # 前两日涨跌
    daily_ma5: float = 0.0
    daily_ma10: float = 0.0
    daily_ma20: float = 0.0
    daily_trend: str = "unknown"          # up/down/neutral
    
    # 周线数据
    weekly_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    weekly_prev_ret: float = 0.0         # 前周涨跌
    weekly_ma5: float = 0.0              # 周线MA5（约5周）
    weekly_ma10: float = 0.0             # 周线MA10
    weekly_trend: str = "unknown"
    weekly_position: str = "unknown"     # 当前价格相对于周线MA的位置
    
    # 月线数据
    monthly_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_prev_ret: float = 0.0        # 前月涨跌
    monthly_ma3: float = 0.0             # 月线MA3
    monthly_ma5: float = 0.0             # 月线MA5
    monthly_trend: str = "unknown"
    monthly_position: str = "unknown"
    
    # 综合判断
    trend_alignment: int = 0             # 多周期共振得分（0-5）
    trend_direction: str = "unknown"       # 综合趋势方向
    risk_level: str = "low"              # 风险等级（low/medium/high/critical）
    
    def __post_init__(self):
        self._load_data()
        self._compute_indicators()
        self._assess_trend()
        self._assess_risk()
    
    def _load_data(self):
        self.daily_df = self.fetcher.fetch_kline(self.code, "day", 60)
        self.weekly_df = self.fetcher.fetch_kline(self.code, "week", 20)
        self.monthly_df = self.fetcher.fetch_kline(self.code, "month", 12)
    
    def _compute_indicators(self):
        # 日线指标
        if not self.daily_df.empty and len(self.daily_df) >= 2:
            latest = self.daily_df.iloc[-1]
            prev = self.daily_df.iloc[-2]
            self.daily_prev_ret = (latest["close"] - prev["close"]) / prev["close"] if prev["close"] > 0 else 0
            
            if len(self.daily_df) >= 3:
                prev2 = self.daily_df.iloc[-3]
                self.daily_prev2_ret = (prev["close"] - prev2["close"]) / prev2["close"] if prev2["close"] > 0 else 0
            
            self.daily_ma5 = self.daily_df["close"].tail(5).mean()
            self.daily_ma10 = self.daily_df["close"].tail(10).mean()
            self.daily_ma20 = self.daily_df["close"].tail(20).mean()
            self.daily_trend = "up" if latest["close"] > latest["open"] else "down"
        
        # 周线指标
        if not self.weekly_df.empty and len(self.weekly_df) >= 2:
            latest = self.weekly_df.iloc[-1]
            prev = self.weekly_df.iloc[-2]
            self.weekly_prev_ret = (latest["close"] - prev["close"]) / prev["close"] if prev["close"] > 0 else 0
            
            self.weekly_ma5 = self.weekly_df["close"].tail(5).mean()
            self.weekly_ma10 = self.weekly_df["close"].tail(10).mean()
            self.weekly_trend = "up" if latest["close"] > latest["open"] else "down"
            
            if latest["close"] > self.weekly_ma5:
                self.weekly_position = "above_ma5"
            elif latest["close"] > self.weekly_ma5 * 0.95:
                self.weekly_position = "near_ma5"
            else:
                self.weekly_position = "below_ma5"
        
        # 月线指标
        if not self.monthly_df.empty and len(self.monthly_df) >= 2:
            latest = self.monthly_df.iloc[-1]
            prev = self.monthly_df.iloc[-2]
            self.monthly_prev_ret = (latest["close"] - prev["close"]) / prev["close"] if prev["close"] > 0 else 0
            
            self.monthly_ma3 = self.monthly_df["close"].tail(3).mean()
            self.monthly_ma5 = self.monthly_df["close"].tail(5).mean()
            self.monthly_trend = "up" if latest["close"] > latest["open"] else "down"
            
            if latest["close"] > self.monthly_ma3:
                self.monthly_position = "above_ma3"
            elif latest["close"] > self.monthly_ma3 * 0.95:
                self.monthly_position = "near_ma3"
            else:
                self.monthly_position = "below_ma3"
    
    def _assess_trend(self):
        """多周期趋势共振评估"""
        alignment = 0
        
        # 1. 日线与周线MA共振
        if not self.daily_df.empty and not self.weekly_df.empty:
            latest_close = self.daily_df.iloc[-1]["close"]
            if latest_close > self.weekly_ma5:
                alignment += 1
        
        # 2. 周线与月线MA共振
        if not self.weekly_df.empty and not self.monthly_df.empty:
            latest_week_close = self.weekly_df.iloc[-1]["close"]
            if latest_week_close > self.monthly_ma3:
                alignment += 1
        
        # 3. 日线趋势
        if self.daily_trend == "up":
            alignment += 1
        
        # 4. 周线趋势
        if self.weekly_trend == "up":
            alignment += 1
        
        # 5. 月线趋势
        if self.monthly_trend == "up":
            alignment += 1
        
        self.trend_alignment = alignment
        
        # 综合趋势判断
        if alignment >= 4:
            self.trend_direction = "strong_up"
        elif alignment >= 3:
            self.trend_direction = "up"
        elif alignment <= 1:
            self.trend_direction = "strong_down"
        elif alignment == 2:
            self.trend_direction = "down" if self.daily_trend == "down" else "neutral"
        else:
            self.trend_direction = "neutral"
    
    def _assess_risk(self):
        """风险评估"""
        risk_score = 0
        
        # 前日大跌
        if self.daily_prev_ret < -0.05:
            risk_score += 2
        elif self.daily_prev_ret < -0.03:
            risk_score += 1
        
        # 前周大跌
        if self.weekly_prev_ret < -0.05:
            risk_score += 2
        elif self.weekly_prev_ret < -0.03:
            risk_score += 1
        
        # 前月大跌
        if self.monthly_prev_ret < -0.10:
            risk_score += 2
        elif self.monthly_prev_ret < -0.05:
            risk_score += 1
        
        # 连续下跌
        if self.daily_prev_ret < -0.03 and self.daily_prev2_ret < -0.03:
            risk_score += 2
        
        # 月线破位
        if self.monthly_position == "below_ma3":
            risk_score += 1
        
        if risk_score >= 6:
            self.risk_level = "critical"
        elif risk_score >= 4:
            self.risk_level = "high"
        elif risk_score >= 2:
            self.risk_level = "medium"
        else:
            self.risk_level = "low"
    
    def to_dict(self) -> dict:
        """导出为字典，供 signal_engine 使用"""
        return {
            # 日线
            "daily_prev_ret": self.daily_prev_ret,
            "daily_prev2_ret": self.daily_prev2_ret,
            "daily_ma5": self.daily_ma5,
            "daily_ma10": self.daily_ma10,
            "daily_ma20": self.daily_ma20,
            "daily_trend": self.daily_trend,
            # 周线
            "weekly_prev_ret": self.weekly_prev_ret,
            "weekly_ma5": self.weekly_ma5,
            "weekly_ma10": self.weekly_ma10,
            "weekly_trend": self.weekly_trend,
            "weekly_position": self.weekly_position,
            # 月线
            "monthly_prev_ret": self.monthly_prev_ret,
            "monthly_ma3": self.monthly_ma3,
            "monthly_ma5": self.monthly_ma5,
            "monthly_trend": self.monthly_trend,
            "monthly_position": self.monthly_position,
            # 综合
            "trend_alignment": self.trend_alignment,
            "trend_direction": self.trend_direction,
            "risk_level": self.risk_level,
        }


# 便捷函数：直接获取多周期上下文字典
def get_multi_timeframe_context(code: str) -> dict:
    """快速获取多周期上下文字典"""
    fetcher = MultiTimeframeFetcher()
    ctx = fetcher.build_context(code)
    return ctx.to_dict()
