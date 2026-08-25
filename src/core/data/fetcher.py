# Wrapper for root-level data_fetcher.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from data_fetcher import *
