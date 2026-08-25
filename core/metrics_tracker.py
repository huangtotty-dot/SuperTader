# -*- coding: utf-8 -*-
"""
指标跟踪器 - MetricsTracker

记录和分析交易系统的性能指标。
"""

from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
import json
from collections import defaultdict


@dataclass
class DailyMetrics:
    """日度指标"""
    date: date

    # 信号统计
    signal_count: int = 0
    buy_signals: int = 0
    sell_signals: int = 0

    # 订单统计
    order_count: int = 0
    filled_orders: int = 0
    cancelled_orders: int = 0
    failed_orders: int = 0

    # 成交统计
    total_buy_amount: float = 0.0
    total_sell_amount: float = 0.0

    # 盈亏统计
    daily_pnl: float = 0.0
    total_pnl: float = 0.0

    # 持仓统计
    final_holdings: Dict[str, int] = field(default_factory=dict)

    # 执行效果
    execution_latency_ms: float = 0.0
    fill_rate: float = 0.0

    # 系统健康度
    errors_count: int = 0
    warnings_count: int = 0


@dataclass
class PeriodMetrics:
    """周期指标聚合"""
    period_name: str  # e.g., "2026-08-25", "week_34", "month_08"
    start_date: date
    end_date: date

    # 交易统计
    total_signals: int = 0
    win_trades: int = 0
    lose_trades: int = 0
    win_rate: float = 0.0

    # 盈亏统计
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharp_ratio: float = 0.0

    # 执行效果
    avg_execution_latency_ms: float = 0.0
    avg_fill_rate: float = 0.0

    # 详细数据
    daily_metrics: List[DailyMetrics] = field(default_factory=list)


class MetricsTracker:
    """指标跟踪器"""

    def __init__(self, data_dir: str = "t_io/metrics"):
        """
        初始化指标跟踪器

        Args:
            data_dir: 数据存储目录
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 当前日期的指标
        self.current_date = date.today()
        self.daily_metrics = DailyMetrics(date=self.current_date)

        # 历史指标缓存
        self.daily_history: Dict[date, DailyMetrics] = {}
        self.period_history: Dict[str, PeriodMetrics] = {}

        # 统计数据
        self.signals: List[Dict[str, Any]] = []
        self.orders: List[Dict[str, Any]] = []

    def record_signal(self, code: str, signal_type: str, price: float,
                     strength: float, reason: str = ""):
        """
        记录交易信号

        Args:
            code: 股票代码
            signal_type: 信号类型 (BUY_LOW / SELL_HIGH / HOLD)
            price: 信号价格
            strength: 信号强度 (0-100)
            reason: 信号原因
        """
        signal_record = {
            "timestamp": datetime.now().isoformat(),
            "code": code,
            "type": signal_type,
            "price": price,
            "strength": strength,
            "reason": reason,
        }

        self.signals.append(signal_record)

        # 更新日度指标
        self.daily_metrics.signal_count += 1
        if signal_type == "BUY_LOW":
            self.daily_metrics.buy_signals += 1
        elif signal_type == "SELL_HIGH":
            self.daily_metrics.sell_signals += 1

    def record_order(self, code: str, direction: str, qty: int, price: float,
                    order_id: str = "", status: str = "SUBMITTED"):
        """
        记录订单

        Args:
            code: 股票代码
            direction: 方向 (BUY / SELL)
            qty: 数量
            price: 价格
            order_id: 订单ID
            status: 订单状态
        """
        order_record = {
            "timestamp": datetime.now().isoformat(),
            "code": code,
            "direction": direction,
            "qty": qty,
            "price": price,
            "order_id": order_id,
            "status": status,
            "amount": qty * price,
        }

        self.orders.append(order_record)

        # 更新日度指标
        self.daily_metrics.order_count += 1
        if status == "FILLED":
            self.daily_metrics.filled_orders += 1
            if direction == "BUY":
                self.daily_metrics.total_buy_amount += qty * price
            else:
                self.daily_metrics.total_sell_amount += qty * price
        elif status == "CANCELLED":
            self.daily_metrics.cancelled_orders += 1
        elif status == "FAILED":
            self.daily_metrics.failed_orders += 1

    def update_position(self, holdings: Dict[str, int]):
        """
        更新持仓信息

        Args:
            holdings: 持仓字典 {code: qty}
        """
        self.daily_metrics.final_holdings = holdings.copy()

    def update_pnl(self, daily_pnl: float, total_pnl: float = None):
        """
        更新盈亏

        Args:
            daily_pnl: 日度盈亏
            total_pnl: 累计盈亏
        """
        self.daily_metrics.daily_pnl = daily_pnl
        if total_pnl is not None:
            self.daily_metrics.total_pnl = total_pnl

    def update_execution_metrics(self, latency_ms: float, fill_rate: float):
        """
        更新执行指标

        Args:
            latency_ms: 执行延迟（毫秒）
            fill_rate: 成交率 (0-1)
        """
        self.daily_metrics.execution_latency_ms = latency_ms
        self.daily_metrics.fill_rate = fill_rate

    def record_error(self, error_msg: str, severity: str = "ERROR"):
        """
        记录错误

        Args:
            error_msg: 错误信息
            severity: 严重等级 (ERROR / WARNING)
        """
        if severity == "ERROR":
            self.daily_metrics.errors_count += 1
        else:
            self.daily_metrics.warnings_count += 1

    def save_daily_report(self) -> str:
        """
        保存日报告

        Returns:
            报告文件路径
        """
        report_file = self.data_dir / f"daily_{self.current_date}.json"

        report_data = {
            "date": str(self.current_date),
            "daily_metrics": {
                "signal_count": self.daily_metrics.signal_count,
                "buy_signals": self.daily_metrics.buy_signals,
                "sell_signals": self.daily_metrics.sell_signals,
                "order_count": self.daily_metrics.order_count,
                "filled_orders": self.daily_metrics.filled_orders,
                "daily_pnl": self.daily_metrics.daily_pnl,
                "total_pnl": self.daily_metrics.total_pnl,
                "execution_latency_ms": self.daily_metrics.execution_latency_ms,
                "fill_rate": self.daily_metrics.fill_rate,
                "errors_count": self.daily_metrics.errors_count,
            },
            "signals": self.signals[-10:],  # 最后10个信号
            "orders": self.orders[-10:],    # 最后10个订单
        }

        with open(report_file, 'w') as f:
            json.dump(report_data, f, indent=2, default=str)

        return str(report_file)

    def generate_period_report(self, start_date: date, end_date: date) -> PeriodMetrics:
        """
        生成周期报告

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            周期指标
        """
        period_name = f"{start_date}_to_{end_date}"
        daily_list = []

        # 从存储的报告中读取数据
        total_pnl = 0.0
        total_signals = 0
        win_trades = 0

        for report_file in sorted(self.data_dir.glob("daily_*.json")):
            with open(report_file, 'r') as f:
                data = json.load(f)

            report_date = data['date']
            if start_date <= date.fromisoformat(report_date) <= end_date:
                metrics = data['daily_metrics']
                total_pnl += metrics.get('daily_pnl', 0)
                total_signals += metrics.get('signal_count', 0)
                if metrics.get('daily_pnl', 0) > 0:
                    win_trades += 1

        win_rate = win_trades / total_signals if total_signals > 0 else 0

        period_metrics = PeriodMetrics(
            period_name=period_name,
            start_date=start_date,
            end_date=end_date,
            total_signals=total_signals,
            win_trades=win_trades,
            win_rate=win_rate,
            total_pnl=total_pnl,
        )

        self.period_history[period_name] = period_metrics
        return period_metrics

    def get_summary(self) -> Dict[str, Any]:
        """
        获取当前摘要

        Returns:
            摘要数据
        """
        return {
            "date": str(self.current_date),
            "signals": {
                "total": self.daily_metrics.signal_count,
                "buy": self.daily_metrics.buy_signals,
                "sell": self.daily_metrics.sell_signals,
            },
            "orders": {
                "total": self.daily_metrics.order_count,
                "filled": self.daily_metrics.filled_orders,
                "cancelled": self.daily_metrics.cancelled_orders,
                "failed": self.daily_metrics.failed_orders,
            },
            "pnl": {
                "daily": self.daily_metrics.daily_pnl,
                "total": self.daily_metrics.total_pnl,
            },
            "execution": {
                "latency_ms": self.daily_metrics.execution_latency_ms,
                "fill_rate": self.daily_metrics.fill_rate,
            },
            "system": {
                "errors": self.daily_metrics.errors_count,
                "warnings": self.daily_metrics.warnings_count,
            },
        }
