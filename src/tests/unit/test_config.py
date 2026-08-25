"""Unit tests for configuration schema"""

import pytest
from src.config.schema import (
    SystemConfig, DataSourceConfig, IndicatorConfig,
    SignalConfig, RiskConfig, BacktestConfig,
    validate_config, MarketRegimeEnum
)


class TestDataSourceConfig:
    """Test DataSourceConfig validation"""
    
    def test_default_values(self):
        config = DataSourceConfig()
        assert config.provider == "akshare"
        assert config.cache_enabled is True
        assert config.cache_ttl_hours == 24
    
    def test_cache_ttl_validation(self):
        with pytest.raises(ValueError):
            DataSourceConfig(cache_ttl_hours=0)
        
        with pytest.raises(ValueError):
            DataSourceConfig(cache_ttl_hours=300)


class TestRiskConfig:
    """Test RiskConfig validation"""
    
    def test_position_size_limits(self):
        with pytest.raises(ValueError):
            RiskConfig(max_position_size=0)
        
        with pytest.raises(ValueError):
            RiskConfig(max_position_size=100)
    
    def test_stop_loss_validation(self):
        config = RiskConfig(stop_loss_percent=2.0)
        assert config.stop_loss_percent == 2.0


class TestSystemConfig:
    """Test SystemConfig validation and methods"""
    
    def test_full_config_creation(self):
        config = SystemConfig(
            version="1.0.0",
            environment="development",
            debug=False,
            data=DataSourceConfig(),
            risk=RiskConfig(),
        )
        assert config.version == "1.0.0"
        assert config.environment == "development"
    
    def test_to_dict(self):
        config = SystemConfig(version="1.0.0")
        config_dict = config.to_dict()
        assert isinstance(config_dict, dict)
        assert "version" in config_dict


class TestConfigValidation:
    """Test config validation function"""
    
    def test_valid_config_dict(self):
        config_dict = {
            "version": "1.0.0",
            "environment": "development",
        }
        config = validate_config(config_dict)
        assert config.version == "1.0.0"
    
    def test_invalid_config_dict(self):
        config_dict = {"invalid_field": "value"}
        with pytest.raises(ValueError):
            validate_config(config_dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
