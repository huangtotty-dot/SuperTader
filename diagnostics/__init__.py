# -*- coding: utf-8 -*-
"""
诊断工具集 - 向后兼容导入
"""

try:
    from .deep_analyzer import *
except ImportError:
    pass

try:
    from .resonance_analyzer import *
except ImportError:
    pass

try:
    from .macd_analyzer import *
except ImportError:
    pass

__all__ = ['diagnose_deep', 'diagnose_index_resonance', 'macd_diagnose']
