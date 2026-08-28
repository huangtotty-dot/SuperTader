# -*- coding: utf-8 -*-
"""MarketDataProvider 抽象基类（合并实施方案 §0.2）。
任何 provider 必须产出同样形态的 DataFrame/字典契约，单位统一"手"。
"""
from abc import ABC, abstractmethod

import pandas as pd


class MarketDataProvider(ABC):
    """数据源抽象。实现类须设置 self.source ∈ {"gm","tencent","cache"}。"""

    source = "abstract"

    @abstractmethod
    def daily(self, code: str, days: int = 800) -> pd.DataFrame:
        """日线，前复权。返回列: date(str YYYY-MM-DD), open, high, low, close, volume(手)。"""

    @abstractmethod
    def minute(self, code: str, date: str) -> pd.DataFrame:
        """当日 1 分钟线。返回列: time(str "HH:MM"), open, high, low, close, volume(手), amount(元)。"""

    @abstractmethod
    def snapshot(self, codes: list) -> dict:
        """实时快照。返回 {code: {price, open, high, low, volume(手), ts_date(YYYY-MM-DD)}}。"""

    @abstractmethod
    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        """指数日线，前复权。返回列: date, open, high, low, close, volume(手)。
        end_date 给定时按该日截止（回测/历史 regime 用）。"""
