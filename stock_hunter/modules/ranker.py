# -*- coding: utf-8 -*-
"""
排名与TOP5生成模块
职责：概念排名、个股排名、TOP5 去重与跨板块覆盖
扩展接口：继承 RankerBase 可自定义排名策略
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
import pandas as pd


class RankerBase(ABC):
    """排名策略基类"""

    @abstractmethod
    def rank(self, items: List[dict]) -> List[dict]:
        """
        对项目列表进行排序
        :param items: 字典列表，每个字典必须包含 "总得分" 或指定排序字段
        :return: 排序后的列表
        """
        pass


class ScoreRanker(RankerBase):
    """按总得分降序排名，得分相同则按涨停家数 > 成交额 > 细分数量 打破平局"""

    def __init__(self, tie_breakers: List[str] = None):
        self.tie_breakers = tie_breakers or ["涨停家数", "成交额", "细分数量"]

    def rank(self, items: List[dict]) -> List[dict]:
        def sort_key(item):
            score = item.get("总得分", 0)
            breaks = []
            for key in self.tie_breakers:
                val = item.get(key, 0)
                if isinstance(val, str):
                    try:
                        val = float(val)
                    except ValueError:
                        val = 0
                breaks.append(-val)  # 降序
            return (-score, *breaks)

        return sorted(items, key=sort_key)


class Top5Ranker:
    """
    TOP5 生成器
    规则：
    1. 去重：同代码仅保留 1 条（取最高得分记录）
    2. 跨板块覆盖：同板块最多保留 2 个，超出则顺延至不同板块的标的
    3. 强制填充：若不足 5 条，从去重后备列表中补齐（允许同板块超限）
    """

    def __init__(self, max_same_sector: int = 2, min_diversity: int = 3, top_count: int = 5):
        self.max_same_sector = max_same_sector
        self.min_diversity = min_diversity
        self.top_count = top_count

    def _deduplicate(self, stock_list: List[dict]) -> List[dict]:
        """去重：同代码保留最高得分记录"""
        code_map = {}
        for stock in stock_list:
            code = stock.get("代码", "")
            if not code:
                continue
            if code not in code_map or stock.get("总得分", 0) > code_map[code].get("总得分", 0):
                code_map[code] = stock
        return list(code_map.values())

    def select(self, stock_list: List[dict]) -> List[dict]:
        """
        从全市场标的中选出 TOP5
        :param stock_list: 全部股票评分结果（已含 "总得分"）
        :return: 恰好 5 条的 TOP5 列表
        """
        if not stock_list:
            return []

        # 1. 去重
        deduped = self._deduplicate(stock_list)

        # 2. 按总得分排序
        ranker = ScoreRanker()
        sorted_stocks = ranker.rank(deduped)

        # 3. 按板块覆盖规则筛选
        selected = []
        sector_counts = {}
        backup = []  # 被板块覆盖规则过滤掉的备选

        for stock in sorted_stocks:
            sector = stock.get("所属板块", stock.get("板块", "未知"))
            if sector_counts.get(sector, 0) < self.max_same_sector:
                selected.append(stock)
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            else:
                backup.append(stock)

            if len(selected) >= self.top_count:
                break

        # 4. 检查多样性：若覆盖板块数 < min_diversity，从 backup 中补充不同板块
        sectors_in_selected = set(s.get("所属板块", s.get("板块", "未知")) for s in selected)
        if len(sectors_in_selected) < self.min_diversity and backup:
            for stock in backup:
                sector = stock.get("所属板块", stock.get("板块", "未知"))
                if sector not in sectors_in_selected:
                    selected.append(stock)
                    sectors_in_selected.add(sector)
                if len(sectors_in_selected) >= self.min_diversity or len(selected) >= self.top_count:
                    break

        # 5. 强制填充至 5 条（若不足）
        if len(selected) < self.top_count and backup:
            for stock in backup:
                if stock not in selected:
                    selected.append(stock)
                if len(selected) >= self.top_count:
                    break

        # 6. 截取前 5 条，重新编号
        result = selected[:self.top_count]
        for idx, stock in enumerate(result, 1):
            stock["排名"] = idx
        return result


class SectorRanker:
    """
    板块内排名生成器
    用于各板块 Sheet 的细分概念排名 + 个股排名
    """

    def __init__(self, ranker: RankerBase = None):
        self.ranker = ranker or ScoreRanker()

    def rank_concepts(self, concept_list: List[dict]) -> List[dict]:
        """细分概念排名"""
        return self.ranker.rank(concept_list)

    def rank_stocks(self, stock_list: List[dict]) -> List[dict]:
        """板块内个股排名"""
        return self.ranker.rank(stock_list)

    def rank_sectors(self, sector_summary_list: List[dict]) -> List[dict]:
        """
        概念总排名（Sheet 1）
        按总得分降序，对板块进行排名
        """
        return self.ranker.rank(sector_summary_list)


class DetailRanker:
    """
    详细个股排名（Sheet 2）
    按细分概念最强股得分降序排列
    """

    def __init__(self, ranker: RankerBase = None):
        self.ranker = ranker or ScoreRanker()

    def rank_by_strongest_stock(self, detail_list: List[dict]) -> List[dict]:
        """
        按每个细分概念的 "最强股得分" 降序排列
        """
        def sort_key(item):
            return -item.get("最强股得分", item.get("最高分", 0))
        return sorted(detail_list, key=sort_key)

    def build_detail_rows(self, concept_data: List[dict]) -> pd.DataFrame:
        """
        将概念数据构建为详细排名 DataFrame
        """
        ranked = self.rank_by_strongest_stock(concept_data)
        for idx, item in enumerate(ranked, 1):
            item["排名"] = idx
        return pd.DataFrame(ranked)


# --------------- 增强版 TOP5：热度联动 + 概念稀缺性 ---------------

class EnhancedTop5Ranker(Top5Ranker):
    """
    增强版 TOP5 生成器（v1.0）
    在原有"板块分散+得分排序"基础上引入：
    1. 板块热度动量：优先选择 🔥加速/📈升温 板块的标的
    2. 概念稀缺性：横跨多概念（≥3个分类）的标的获得加分
    3. 大成交额偏好：成交额>50亿的标的轻微加分

    用法：
        ranker = EnhancedTop5Ranker()
        top5 = ranker.select(scored_list, summary_df=heat_summary_df)
    """

    def __init__(self, max_same_sector: int = 2, min_diversity: int = 3, top_count: int = 5,
                 heat_weight: float = 0.3, scarcity_weight: float = 0.15, volume_weight: float = 0.05):
        super().__init__(max_same_sector, min_diversity, top_count)
        self.heat_weight = heat_weight
        self.scarcity_weight = scarcity_weight
        self.volume_weight = volume_weight

    def select(self, stock_list: List[dict],
               summary_df: pd.DataFrame = None,
               category_stats: dict = None) -> List[dict]:
        """
        增强版 TOP5 选择

        参数
        ----
        stock_list : 评分后的股票列表
        summary_df : 概念总排名 DataFrame（含热度分、趋势列）
        category_stats : 各分类标的数统计（用于概念稀缺性计算）
        """
        if not stock_list:
            return []

        # 1. 构建热度分查找表
        heat_map = {}
        if summary_df is not None and not summary_df.empty:
            if "板块" in summary_df.columns and "热度分" in summary_df.columns:
                for _, row in summary_df.iterrows():
                    sector = str(row.get("板块", ""))
                    heat = float(row.get("热度分", 0) or 0)
                    trend = str(row.get("趋势", ""))
                    heat_map[sector] = {"热度分": heat, "趋势": trend}

        # 2. 计算概念稀缺性（跨分类数量）
        if category_stats is None:
            # 从 stock_list 自身推算（基于 韭研分类 字段）
            category_stats = {}
            for s in stock_list:
                cat = s.get("所属板块", s.get("板块", ""))
                category_stats[cat] = category_stats.get(cat, 0) + 1

        # 3. 计算增强得分
        enhanced_list = []
        for stock in stock_list:
            base_score = stock.get("总得分", 0)
            sector = stock.get("所属板块", stock.get("板块", "未知"))

            # 3a. 热度动量分
            heat_info = heat_map.get(sector, {})
            heat_score = heat_info.get("热度分", 30)  # 默认中等热度
            trend = heat_info.get("趋势", "")
            # 热度分归一化到 0-1
            heat_bonus = heat_score / 100.0

            # 3b. 概念稀缺分（横跨多分类）
            concept_count = stock.get("暗线概念数", 1)
            scarcity_bonus = min(concept_count / 10.0, 1.0)  # 最多 10 个概念 → 1.0

            # 3c. 成交额分
            amount = float(stock.get("成交额", 0) or 0)
            volume_bonus = min(amount / 10e8, 1.0)  # 10亿 → 1.0

            # 综合加权
            enhanced_score = (
                base_score * (1 - self.heat_weight - self.scarcity_weight - self.volume_weight) +
                heat_bonus * 37 * self.heat_weight +
                scarcity_bonus * 37 * self.scarcity_weight +
                volume_bonus * 37 * self.volume_weight
            )

            enhanced_stock = dict(stock)
            enhanced_stock["总得分"] = round(enhanced_score, 1)
            enhanced_stock["原始得分"] = base_score
            enhanced_stock["热度加成%"] = round(heat_bonus * 100, 1)
            enhanced_stock["概念加成%"] = round(scarcity_bonus * 100, 1)
            enhanced_stock["板块热度"] = heat_score
            enhanced_stock["板块趋势"] = trend
            enhanced_list.append(enhanced_stock)

        # 4. 复用父类的去重+板块分散逻辑
        deduped = self._deduplicate(enhanced_list)
        sorter = ScoreRanker()
        sorted_stocks = sorter.rank(deduped)

        selected = []
        sector_counts = {}
        backup = []

        for stock in sorted_stocks:
            sector = stock.get("所属板块", stock.get("板块", "未知"))
            if sector_counts.get(sector, 0) < self.max_same_sector:
                selected.append(stock)
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            else:
                backup.append(stock)
            if len(selected) >= self.top_count:
                break

        # 5. 多样性补充
        sectors_in_selected = set(s.get("所属板块", s.get("板块", "未知")) for s in selected)
        if len(sectors_in_selected) < self.min_diversity and backup:
            for stock in backup:
                sector = stock.get("所属板块", stock.get("板块", "未知"))
                if sector not in sectors_in_selected:
                    selected.append(stock)
                    sectors_in_selected.add(sector)
                if len(sectors_in_selected) >= self.min_diversity or len(selected) >= self.top_count:
                    break

        # 6. 填充至 top_count
        if len(selected) < self.top_count and backup:
            for stock in backup:
                if stock not in selected:
                    selected.append(stock)
                if len(selected) >= self.top_count:
                    break

        result = selected[:self.top_count]
        for idx, stock in enumerate(result, 1):
            stock["排名"] = idx
        return result
