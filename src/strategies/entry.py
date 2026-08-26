# Wrapper for entry modules
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.precise_entry_framework import *
from universal_precise_entry import *
