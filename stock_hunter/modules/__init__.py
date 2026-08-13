# -*- coding: utf-8 -*-
"""
模块导入快捷入口
可通过 from modules import DataLoader, ConceptScorer, ... 一次性导入
"""
from .data_loader import DataLoader, DataLoaderBase, WatchlistLoader
from .market_data import MarketDataFetcher, TencentDataFetcher
from .scorer import ConceptScorer, ScorerBase
from .ranker import Top5Ranker, EnhancedTop5Ranker, SectorRanker, DetailRanker, RankerBase
from .styler import ExcelStyler, V8Style, StyleBase
from .reporter import ExcelReporter, ReportBuilder, ReporterBase
from .validator import ReportValidator, ValidatorBase

__version__ = "8.1.0"
__all__ = [
    "DataLoader", "DataLoaderBase", "WatchlistLoader",
    "MarketDataFetcher", "TencentDataFetcher",
    "ConceptScorer", "ScorerBase",
    "Top5Ranker", "EnhancedTop5Ranker", "SectorRanker", "DetailRanker", "RankerBase",
    "ExcelStyler", "V8Style", "StyleBase",
    "ExcelReporter", "ReportBuilder", "ReporterBase",
    "ReportValidator", "ValidatorBase",
]
