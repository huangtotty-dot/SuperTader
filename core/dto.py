# -*- coding: utf-8 -*-
"""
核心数据结构定义 - DTO (Data Transfer Objects)

定义系统各层间通信的标准数据结构，确保解耦和可维护性。
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime


# ============ 枚举类型 ============

class SignalType(Enum):
    """信号类型"""
    BUY_LOW = "BUY_LOW"       # 低吸买入
    SELL_HIGH = "SELL_HIGH"   # 高抛卖出
    HOLD = "HOLD"             # 持仓
    NEUTRAL = "NEUTRAL"       # 中立


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "PENDING"       # 待提交
    SUBMITTED = "SUBMITTED"   # 已提交
    FILLED = "FILLED"         # 已成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交
    CANCELLED = "CANCELLED"   # 已撤销
    FAILED = "FAILED"         # 失败


class RegimeState(Enum):
    """市场制度状态"""
    UPTREND = "UPTREND"       # 上升
    DOWNTREND = "DOWNTREND"   # 下降
    RANGE = "RANGE"           # 震荡


# ============ 数据层 DTO ============

@dataclass
class MarketData:
    """市场数据"""
    code: str
    timestamp: datetime
    price: float
    high: float
    low: float
    volume: int
    amount: float

    # 技术指标
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    signal: Optional[float] = None
    histogram: Optional[float] = None

    # 扩展字段
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OHLCV:
    """K线数据"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


# ============ 分析层 DTO ============

@dataclass
class IndicatorResult:
    """指标计算结果"""
    name: str
    value: float
    timestamp: datetime

    # 多周期时可能需要
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    rsi: Optional[float] = None


@dataclass
class RegimeAnalysisResult:
    """制度分析结果"""
    code: str
    timestamp: datetime

    # 制度判断
    index_regime: RegimeState
    market_regime: RegimeState
    trend_regime: RegimeState

    # 置信度
    index_confidence: float  # 0-1
    market_confidence: float
    trend_confidence: float

    # 详细分析
    details: Dict[str, Any] = field(default_factory=dict)


# ============ 决策层 DTO ============

@dataclass
class Signal:
    """交易信号"""
    code: str
    signal_type: SignalType
    timestamp: datetime

    # 信号强度 (0-100)
    strength: float

    # 决策原因
    reason: str

    # 关键参数
    price: float
    decision_time: str  # 时刻，如 "10:30"

    # 元数据
    trace: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildSignal:
    """建仓信号"""
    code: str
    timestamp: datetime
    is_signal: bool
    score: float
    suggested_qty: int
    suggested_price: float

    # 建仓通道
    channel: Optional[str] = None  # "ice_point_reversal" / "breakout_follow"

    # 详细分析
    analysis: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SizingAdvice:
    """仓位建议"""
    code: str
    timestamp: datetime
    action: str  # "add" / "reduce" / "first_add" / "cover"
    suggested_qty: int
    suggested_price: float
    reason: str
    current_position: int
    target_position: int


# ============ 执行层 DTO ============

@dataclass
class Order:
    """订单"""
    code: str
    direction: str  # "BUY" / "SELL"
    quantity: int
    price: float
    timestamp: datetime

    # 订单属性
    order_type: str = "LIMIT"  # "LIMIT" / "MARKET"
    order_id: Optional[str] = None

    # 元数据
    source: str = "SIGNAL"  # 来源
    trace: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    """订单结果"""
    order_id: str
    code: str
    status: OrderStatus

    # 成交信息
    filled_qty: int
    filled_price: float
    filled_amount: float

    # 时间
    submit_time: datetime
    fill_time: Optional[datetime] = None

    # 错误信息（如果失败）
    error_msg: Optional[str] = None


@dataclass
class RiskCheck:
    """风险检查结果"""
    order: Order
    timestamp: datetime

    # 是否通过风控
    passed: bool

    # 风险等级 (0-100)
    risk_level: float

    # 失败原因
    reason: Optional[str] = None

    # 详细检查结果
    checks: Dict[str, bool] = field(default_factory=dict)


# ============ 系统级 DTO ============

@dataclass
class SystemState:
    """系统状态快照"""
    timestamp: datetime

    # 持仓信息
    holdings: Dict[str, int]  # code -> qty

    # 当前信号
    active_signals: List[Signal] = field(default_factory=list)

    # 待执行订单
    pending_orders: List[Order] = field(default_factory=list)

    # 系统健康状态
    is_healthy: bool = True
    last_error: Optional[str] = None

    # 性能指标
    metrics: Dict[str, Any] = field(default_factory=dict)


# ============ 配置 DTO ============

@dataclass
class ConfigVersion:
    """配置版本信息"""
    name: str
    version: str
    created_at: datetime
    description: str

    # 参数哈希，用于快速对比
    config_hash: str

    # 配置内容
    config: Dict[str, Any] = field(default_factory=dict)


__all__ = [
    # Enums
    'SignalType',
    'OrderStatus',
    'RegimeState',
    # Market Data
    'MarketData',
    'OHLCV',
    # Analysis
    'IndicatorResult',
    'RegimeAnalysisResult',
    # Decision
    'Signal',
    'BuildSignal',
    'SizingAdvice',
    # Execution
    'Order',
    'OrderResult',
    'RiskCheck',
    # System
    'SystemState',
    'ConfigVersion',
]
