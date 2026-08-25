"""Common helper functions"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

def ensure_data_available(symbol: str, date: str) -> bool:
    """Verify data is available for a given symbol and date"""
    pass

def calculate_returns(prices: list) -> float:
    """Calculate percentage returns from price list"""
    if len(prices) < 2:
        return 0.0
    return ((prices[-1] - prices[0]) / prices[0]) * 100

def format_signal_output(signal: dict) -> str:
    """Format signal data for output/logging"""
    pass
