"""Pydantic configuration schema for validation"""

from pydantic import BaseModel, Field, validator, root_validator
from typing import Optional, List, Dict, Any
from enum import Enum


class MarketRegimeEnum(str, Enum):
    """Market regime types"""
    BULL = "bull"
    BEAR = "bear"
    RANGE = "range"
    VOLATILE = "volatile"


class RiskProfileEnum(str, Enum):
    """Risk profile types"""
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class DataSourceConfig(BaseModel):
    """Data source configuration"""
    provider: str = Field(default="akshare", description="Data provider")
    cache_enabled: bool = Field(default=True, description="Enable data caching")
    cache_ttl_hours: int = Field(default=24, ge=1, le=240, description="Cache TTL in hours")
    retry_attempts: int = Field(default=3, ge=1, le=10, description="Retry attempts for data fetch")
    
    class Config:
        use_enum_values = True


class IndicatorConfig(BaseModel):
    """Indicator configuration"""
    ma_periods: List[int] = Field(default=[5, 10, 20, 60], description="Moving average periods")
    rsi_period: int = Field(default=14, ge=5, le=50, description="RSI period")
    macd_fast: int = Field(default=12, description="MACD fast EMA")
    macd_slow: int = Field(default=26, description="MACD slow EMA")
    macd_signal: int = Field(default=9, description="MACD signal line")
    bb_period: int = Field(default=20, description="Bollinger Band period")
    bb_std_dev: float = Field(default=2.0, ge=1.0, le=5.0, description="Bollinger Band std dev")


class SignalConfig(BaseModel):
    """Signal generation configuration"""
    enabled: bool = Field(default=True)
    min_strength: float = Field(default=0.5, ge=0.0, le=1.0, description="Minimum signal strength")
    confirmation_periods: int = Field(default=2, ge=1, le=10, description="Periods for confirmation")
    filters: List[str] = Field(default=[], description="Signal filters to apply")


class RiskConfig(BaseModel):
    """Risk management configuration"""
    max_position_size: float = Field(default=10.0, ge=0.1, le=50.0, description="Max position size %")
    stop_loss_percent: float = Field(default=2.0, ge=0.1, le=10.0, description="Stop loss %")
    take_profit_percent: float = Field(default=5.0, ge=0.5, le=50.0, description="Take profit %")
    max_daily_loss: float = Field(default=5.0, ge=1.0, le=20.0, description="Max daily loss %")
    profile: RiskProfileEnum = Field(default=RiskProfileEnum.MODERATE)


class BacktestConfig(BaseModel):
    """Backtest configuration"""
    enabled: bool = Field(default=False)
    start_date: str = Field(description="Start date YYYY-MM-DD")
    end_date: str = Field(description="End date YYYY-MM-DD")
    initial_capital: float = Field(default=100000.0, ge=1000.0)
    commission_percent: float = Field(default=0.001, ge=0.0, le=0.01)
    slippage_percent: float = Field(default=0.0, ge=0.0, le=0.05)
    
    @validator('end_date')
    def end_date_after_start_date(cls, v, values):
        if 'start_date' in values and v <= values['start_date']:
            raise ValueError('end_date must be after start_date')
        return v


class SystemConfig(BaseModel):
    """Main system configuration"""
    version: str = Field(description="Config schema version")
    environment: str = Field(default="development", description="dev/prod/backtest")
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO", description="Log level")
    
    # Sub-configurations
    data: DataSourceConfig = Field(default_factory=DataSourceConfig)
    indicators: IndicatorConfig = Field(default_factory=IndicatorConfig)
    signals: SignalConfig = Field(default_factory=SignalConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default=None)
    
    # Additional settings
    market_regime: Optional[MarketRegimeEnum] = Field(default=None)
    custom_parameters: Dict[str, Any] = Field(default_factory=dict, description="Custom parameters")
    
    class Config:
        use_enum_values = False
        schema_extra = {
            "example": {
                "version": "1.0.0",
                "environment": "development",
                "debug": False,
                "data": {
                    "provider": "akshare",
                    "cache_enabled": True,
                    "cache_ttl_hours": 24
                },
                "risk": {
                    "max_position_size": 10.0,
                    "profile": "moderate"
                }
            }
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary"""
        return self.dict(exclude_none=True)
    
    def to_json(self, filepath: str) -> None:
        """Export config to JSON file"""
        import json
        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
    
    @classmethod
    def from_json(cls, filepath: str) -> "SystemConfig":
        """Load config from JSON file"""
        import json
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(**data)


def validate_config(config_dict: Dict[str, Any]) -> SystemConfig:
    """Validate and create config from dictionary"""
    try:
        return SystemConfig(**config_dict)
    except Exception as e:
        raise ValueError(f"Configuration validation failed: {e}")
