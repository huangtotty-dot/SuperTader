"""Unit tests for utility functions"""

import pytest
from src.utils.helpers import calculate_returns, format_signal_output


class TestHelpers:
    """Test helper functions"""
    
    def test_calculate_returns_normal(self):
        prices = [100, 105, 110]
        returns = calculate_returns(prices)
        assert returns == 10.0
    
    def test_calculate_returns_empty(self):
        prices = []
        returns = calculate_returns(prices)
        assert returns == 0.0
    
    def test_calculate_returns_single_price(self):
        prices = [100]
        returns = calculate_returns(prices)
        assert returns == 0.0
    
    def test_calculate_returns_decline(self):
        prices = [100, 90]
        returns = calculate_returns(prices)
        assert returns == -10.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
