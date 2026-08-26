# Wrapper for regime modules
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from core.market_regime import *
from analysis.trend_regime import *
