# -*- coding: utf-8 -*-
"""
market_regime.py — 市场状态识别器（V1.14 新架构）

功能：
1. 集合竞价阶段识别当天抛压/情绪（09:15-09:25）
2. 基于最近2-3日日线识别主力出货模式
3. 基于盘中分钟线实时识别趋势转折

输出：MarketRegime 枚举 + 原因说明

集成方式：
  在 main.py 的模块加载顺序中，signal_engine.py 之后加载本模块
  通过共享命名空间中的 detect_regime() 调用
"""

from enum import Enum
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import json
import os


class MarketRegime(Enum):
    """市场状态枚举"""
    NORMAL = "normal"               # 正常做T
    HEAVY_SELL = "heavy_sell"       # 集合竞价大抛压 / 主力出货
    DISTRIBUTION = "distribution"   # 连续出货模式（放量+长上影）
    MORNING_SURGE = "morning_surge" # 早盘急拉后跳水
    RECOVERY = "recovery"           # 超跌反弹（昨日大跌后今日低开）
    BREAKOUT = "breakout"           # 强势突破（可顺势加仓）


class RegimeDetector:
    """市场状态识别器"""

    def __init__(self, trace_dir: str = None, preopen_dir: str = None):
        self.trace_dir = trace_dir or os.path.join(os.path.dirname(__file__), "t_io", "traces")
        self.preopen_dir = preopen_dir or self.trace_dir
        self._cache = {}  # 按日期缓存的preopen数据

    # ==================== 集合竞价识别（基于 open_gap 简化版） ====================

    def detect_from_preopen(self, code: str, date: str) -> tuple:
        """
        基于集合竞价数据识别当天状态（V2 简化版：基于 open_gap）

        返回: (MarketRegime, reason_str)
        """
        # 无竞价数据时的默认状态（由 detect() 回退到日线识别）
        return MarketRegime.NORMAL, "无集合竞价数据（V2简化）"

    # ==================== 日线历史识别（出货模式） ====================

    def detect_from_recent_days(self, code: str, daily_bars: List[dict]) -> tuple:
        """
        基于最近2-3日日线识别主力出货模式

        参数:
            daily_bars: [{date, open, high, low, close, volume}, ...]
                        按日期升序排列

        返回: (MarketRegime, reason_str)
        """
        if not daily_bars or len(daily_bars) < 2:
            return MarketRegime.NORMAL, "日线数据不足"

        today = daily_bars[-1]
        yesterday = daily_bars[-2]
        prev_close = float(yesterday.get("close", 0))

        if prev_close <= 0:
            return MarketRegime.NORMAL, "前日收盘价无效"

        # 计算昨日K线形态
        y_high = float(yesterday.get("high", 0))
        y_low = float(yesterday.get("low", 0))
        y_close = float(yesterday.get("close", 0))
        y_open = float(yesterday.get("open", 0))
        y_body = abs(y_close - y_open)
        y_upper = y_high - max(y_close, y_open)
        y_lower = min(y_close, y_open) - y_low
        y_range = y_high - y_low if y_high > y_low else 1e-5

        # 今日数据
        t_high = float(today.get("high", 0))
        t_low = float(today.get("low", 0))
        t_close = float(today.get("close", 0))
        t_open = float(today.get("open", 0))
        t_volume = float(today.get("volume", 0))

        # 识别规则1：连续2日长上影线（主力出货）
        has_long_upper_yesterday = y_upper / y_range > 0.4 and y_body / y_range < 0.5
        if len(daily_bars) >= 3:
            day_before = daily_bars[-3]
            db_high = float(day_before.get("high", 0))
            db_low = float(day_before.get("low", 0))
            db_close = float(day_before.get("close", 0))
            db_open = float(day_before.get("open", 0))
            db_range = db_high - db_low if db_high > db_low else 1e-5
            db_upper = db_high - max(db_close, db_open)
            db_body = abs(db_close - db_open)
            has_long_upper_db = db_upper / db_range > 0.4 and db_body / db_range < 0.5
        else:
            has_long_upper_db = False

        if has_long_upper_yesterday and has_long_upper_db:
            return MarketRegime.DISTRIBUTION, "连续2日长上影线（主力出货确认）"

        # 识别规则2：昨日大阴线+今日低开（下跌加速）
        y_change = (y_close - prev_close) / prev_close if prev_close > 0 else 0
        if y_change < -0.03 and t_open < y_close * 0.99:
            return MarketRegime.HEAVY_SELL, f"昨日大跌{y_change*100:.1f}%且今日低开（抛压延续）"

        # 识别规则3：昨日冲高回落+今日放量下跌
        if has_long_upper_yesterday and t_close < t_open and t_volume > 0:
            return MarketRegime.DISTRIBUTION, "昨日冲高回落+今日下跌（出货确认）"

        # 识别规则4：超跌反弹（昨日大跌5%以上，今日低开）
        if y_change < -0.05 and t_open < y_close * 0.98:
            return MarketRegime.RECOVERY, f"昨日大跌{y_change*100:.1f}%，今日低开（超跌反弹机会）"

        return MarketRegime.NORMAL, "日线无出货信号"

    # ==================== 盘中实时识别（分钟线） ====================

    def detect_from_intraday(self, code: str, minute_bars: List[dict]) -> tuple:
        """
        基于盘中分钟线实时识别趋势转折

        参数:
            minute_bars: [{time, open, high, low, close, volume}, ...]
                        按时间升序排列

        返回: (MarketRegime, reason_str)
        """
        if not minute_bars or len(minute_bars) < 15:
            return MarketRegime.NORMAL, "分钟数据不足"

        # 提取开盘后30分钟数据
        morning = [b for b in minute_bars if b.get("time", "")[11:16] < "10:00"]
        if not morning:
            return MarketRegime.NORMAL, "早盘数据不足"

        prices = [float(b.get("close", 0)) for b in morning if float(b.get("close", 0)) > 0]
        if not prices:
            return MarketRegime.NORMAL, "早盘价格无效"

        morning_high = max(prices)
        morning_low = min(prices)
        first_price = prices[0]
        last_price = prices[-1]

        # 识别规则1：开盘后急拉然后跳水（冲高回落）
        surge_pct = (morning_high - first_price) / first_price if first_price > 0 else 0
        drop_pct = (morning_high - last_price) / morning_high if morning_high > 0 else 0
        if surge_pct > 0.02 and drop_pct > 0.015:
            return MarketRegime.MORNING_SURGE, f"早盘急拉{surge_pct*100:.1f}%后回落{drop_pct*100:.1f}%"

        # 识别规则2：开盘即跳水（集合竞价抛压确认）
        if first_price > 0 and last_price < first_price * 0.98:
            drop_from_open = (first_price - last_price) / first_price
            if drop_from_open > 0.02:
                return MarketRegime.HEAVY_SELL, f"开盘30分钟下跌{drop_from_open*100:.1f}%（抛压确认）"

        # 识别规则3：强势突破（开盘即涨且不回落）
        if surge_pct > 0.015 and drop_pct < 0.005 and last_price > first_price * 1.01:
            return MarketRegime.BREAKOUT, f"早盘强势上涨{surge_pct*100:.1f}%且无回落"

        return MarketRegime.NORMAL, "盘中趋势正常"

    # ==================== 综合识别（主入口） ====================

    def detect(self, code: str, date: str, 
               preopen_data: dict = None, 
               daily_bars: List[dict] = None,
               minute_bars: List[dict] = None) -> tuple:
        """
        综合识别市场状态（主入口）

        优先级：
        1. 集合竞价（最高优先级，如果数据可用）
        2. 日线历史（次优先级）
        3. 盘中实时（最低优先级，用于确认）

        返回: (MarketRegime, reason_str)
        """
        # 1. 集合竞价识别
        if preopen_data:
            regime, reason = self._detect_from_preopen_data(code, preopen_data)
            if regime != MarketRegime.NORMAL:
                return regime, reason
        else:
            regime, reason = self.detect_from_preopen(code, date)
            if regime != MarketRegime.NORMAL:
                return regime, reason

        # 2. 日线历史识别
        if daily_bars:
            regime, reason = self.detect_from_recent_days(code, daily_bars)
            if regime != MarketRegime.NORMAL:
                return regime, reason

        # 3. 盘中实时识别
        if minute_bars:
            regime, reason = self.detect_from_intraday(code, minute_bars)
            if regime != MarketRegime.NORMAL:
                return regime, reason

        return MarketRegime.NORMAL, "综合判断：正常状态"

    def _detect_from_preopen_data(self, code: str, preopen_data: dict) -> tuple:
        """基于已传入的preopen_data识别（V2简化版：基于open_gap）"""
        snapshots = preopen_data.get("code_snapshots", {}) if isinstance(preopen_data, dict) else {}
        snap = snapshots.get(code, {}) if isinstance(snapshots, dict) else {}
        open_gap = float(snap.get("open_gap", 0) or 0)

        if open_gap < -0.02:
            return MarketRegime.HEAVY_SELL, f"竞价低开{abs(open_gap)*100:.1f}%（抛压沉重）"
        if open_gap > 0.02:
            return MarketRegime.BREAKOUT, f"竞价高开{open_gap*100:.1f}%（强势开盘）"

        return MarketRegime.NORMAL, ""

    # ==================== 辅助方法 ====================

    def _load_preopen(self, date: str) -> Optional[dict]:
        """加载指定日期的preopen数据（取最新一条）"""
        if date in self._cache:
            return self._cache[date]

        path = os.path.join(self.preopen_dir, f"preopen_trace_{date}.jsonl")
        if not os.path.exists(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return None
            # 取最后一条（最新的）
            data = json.loads(lines[-1].strip())
            self._cache[date] = data
            return data
        except Exception:
            return None


# ==================== 便捷函数（供共享命名空间调用） ====================

_detector = None

def get_detector() -> RegimeDetector:
    global _detector
    if _detector is None:
        _detector = RegimeDetector()
    return _detector


def detect_regime(code: str, date: str, **kwargs) -> tuple:
    """
    便捷入口函数

    用法（在 main.py 或 signal_engine.py 中调用）：
        regime, reason = detect_regime("000988", "2026-07-01")
        regime, reason = detect_regime("000988", "2026-07-01", preopen_data=preopen_ctx)
        regime, reason = detect_regime("000988", "2026-07-01", daily_bars=[...], minute_bars=[...])
    """
    return get_detector().detect(code, date, **kwargs)


def regime_name(regime) -> str:
    """获取状态中文名（用于飞书通知）"""
    names = {
        MarketRegime.NORMAL: "正常",
        MarketRegime.HEAVY_SELL: "⚠️ 集合竞价重压",
        MarketRegime.DISTRIBUTION: "🔴 主力出货",
        MarketRegime.MORNING_SURGE: "📉 早盘冲高回落",
        MarketRegime.RECOVERY: "🟢 超跌反弹",
        MarketRegime.BREAKOUT: "🚀 强势突破",
    }
    return names.get(regime, "未知")


def should_clear_all(regime) -> bool:
    """判断是否应该全仓清仓"""
    return regime in (MarketRegime.HEAVY_SELL, MarketRegime.DISTRIBUTION)


def should_reduce(regime) -> bool:
    """判断是否应该减仓（但不全清）"""
    return regime in (MarketRegime.MORNING_SURGE,)
