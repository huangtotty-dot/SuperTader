# -*- coding: utf-8 -*-
"""
Quick test for the new _should_stand_down logic
"""
import sys, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import numpy as np, pandas as pd
from datetime import datetime, timedelta

# Mock the minimum needed for SignalEngine to work
import numpy as np, pandas as pd
import config
import signal_engine
from signal_engine import SignalEngine

# Create a mock df with dead_water characteristics but early morning
def test_stand_down():
    engine = SignalEngine()
    
    # Mock data frame that simulates 特变电工 at 09:44 (14 minutes since open)
    dates = [datetime(2026, 7, 7, 9, 30) + timedelta(minutes=i) for i in range(15)]
    df = pd.DataFrame({
        "date": dates,
        "open": [21.35] * 15,
        "high": [21.35] * 15,
        "low": [21.35] * 15,
        "close": [21.35] * 15,
        "volume": [1000] * 15,
        "vwap": [21.166] * 15,
        "range_pos": [0.26] * 15,
    })
    
    holding = {"type": "stock", "hold_qty": 1200}
    
    # Test with today_ret = -0.008 (below -0.005), minutes = 14
    result = engine._should_stand_down(
        "600089", holding, df, 
        buy_score=0, sell_score=45, 
        market_state="dead_water", can_sell=True,
        today_ret=-0.008, minutes_since_open=14
    )
    print(f"Test 1 (特变电工型, 09:44): stand_down={result[0]}, reason={result[1]}")
    assert result[0] == False, "Should NOT stand down in early morning with decline"
    
    # Test with today_ret = -0.003 (above -0.005), minutes = 14
    result2 = engine._should_stand_down(
        "600089", holding, df,
        buy_score=0, sell_score=45,
        market_state="dead_water", can_sell=True,
        today_ret=-0.003, minutes_since_open=14
    )
    print(f"Test 2 (early morning, small decline): stand_down={result2[0]}, reason={result2[1]}")
    assert result2[0] == True, "Should stand down if decline is small"
    
    # Test with minutes = 35 (>30), today_ret = -0.008
    result3 = engine._should_stand_down(
        "600089", holding, df,
        buy_score=0, sell_score=45,
        market_state="dead_water", can_sell=True,
        today_ret=-0.008, minutes_since_open=35
    )
    print(f"Test 3 (after 30 min, decline): stand_down={result3[0]}, reason={result3[1]}")
    assert result3[0] == True, "Should stand down after 30 minutes"
    
    print("All tests passed!")

if __name__ == "__main__":
    test_stand_down()
