# Wrapper for regime modules
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from market_regime import *
from trend_regime import *
