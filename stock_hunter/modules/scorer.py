# -*- coding: utf-8 -*-
"""
评分计算模块 v11 — 标准版+P2箱体突破质量维度
满分 = 8+6+3+2+1+5+6+3+2+3 = 39分，+3分 = 42分

打分标准：
  D1: 强势形态且新高（最高>近150日最高）- 8分
  D2: 强势形态（近5日涨幅>20% 且 最高>近20日最高）- 6分
  D4: 首板资金池（首板涨停）- 3分
  D5: 潜在突破10日（最高>近10日最高）- 2分
  D6: 潜在突破5日（最高>近5日最高 且 非涨停，满足D5则不计）- 1分
  D7: 持续性（当日二板及以上，连板>=2）- 5分
  D8: 情绪分数（当日一字板）- 6分
  D9: 活跃程度（近10日有涨停板）- 3分
  D10: 箱体突破质量（P2新增，基于改进的check_box_breakout）- 2分
  大成交额: 当日成交额>=50亿，额外+3分
"""
from abc import ABC, abstractmethod
from typing import Dict, Tuple


class ScorerBase(ABC):
    """评分维度基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def compute(self, stock_data: dict) -> Tuple[int, str]:
        pass


class D1强势形态且新高Scorer(ScorerBase):
    name = "D1强势形态且新高"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        high = stock_data.get("最高", 0) or 0
        high_150 = stock_data.get("近150日最高", 0) or 0
        score = 8 if (high_150 > 0 and high > high_150) else 0
        return score, f"最高={high:.2f}, 近150日最高={high_150:.2f} -> {score}分"


class D2强势形态Scorer(ScorerBase):
    name = "D2强势形态"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        change_5 = stock_data.get("近5日涨幅", 0) or 0
        high = stock_data.get("最高", 0) or 0
        high_20 = stock_data.get("近20日最高", 0) or 0
        score = 6 if (change_5 > 20 and high_20 > 0 and high > high_20) else 0
        return score, f"近5日涨幅={change_5:.1f}%, 最高={high:.2f}, 近20日最高={high_20:.2f} -> {score}分"


class D4首板资金池Scorer(ScorerBase):
    name = "D4首板资金池"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        is_first_limit = stock_data.get("首板涨停", 0)
        score = 3 if is_first_limit else 0
        return score, f"首板涨停={is_first_limit} -> {score}分"


class D5潜在突破10日Scorer(ScorerBase):
    name = "D5潜在突破10日"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        high = stock_data.get("最高", 0) or 0
        high_10 = stock_data.get("近10日最高", 0) or 0
        # 修复：D5只关注是否突破10日最高价这一技术事实，涨停与否由D1/D4/D7/D8负责
        score = 2 if (high_10 > 0 and high > high_10) else 0
        is_limit = stock_data.get("涨停", 0)
        return score, f"最高={high:.2f}, 近10日最高={high_10:.2f}, 涨停={is_limit} -> {score}分"


class D6潜在突破5日Scorer(ScorerBase):
    name = "D6潜在突破5日"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        high = stock_data.get("最高", 0) or 0
        high_5 = stock_data.get("近5日最高", 0) or 0
        high_10 = stock_data.get("近10日最高", 0) or 0
        is_limit = stock_data.get("涨停", 0)
        d5 = stock_data.get("D5潜在突破10日", 0)
        # 满足D5则D6不计分；最高>近5日最高，且非涨停，且不满足D5（最高<=近10日最高）
        score = 0
        if d5 > 0:
            score = 0
        elif high_5 > 0 and high > high_5 and not is_limit and (high_10 <= 0 or high <= high_10):
            score = 1
        return score, f"最高={high:.2f}, 近5日最高={high_5:.2f}, 涨停={is_limit}, D5={d5} -> {score}分"


class D7持续性Scorer(ScorerBase):
    name = "D7持续性"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        consecutive = stock_data.get("连板天数", 0) or 0
        score = 5 if consecutive >= 2 else 0
        return score, f"连板天数={consecutive} -> {score}分"


class D8情绪分数Scorer(ScorerBase):
    name = "D8情绪分数"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        is_word_limit = stock_data.get("一字板涨停", 0)
        score = 6 if is_word_limit else 0
        return score, f"一字板涨停={is_word_limit} -> {score}分"


class D9活跃程度Scorer(ScorerBase):
    name = "D9活跃程度"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        limit_10 = stock_data.get("近10日涨停", 0)
        score = 3 if limit_10 else 0
        return score, f"近10日涨停={limit_10} -> {score}分"


class 大成交额Scorer(ScorerBase):
    name = "大成交额额外加分"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        amount = stock_data.get("成交额", 0) or 0
        score = 3 if amount >= 5000000000 else 0  # 50亿 = 5,000,000,000
        return score, f"成交额={amount/1e8:.2f}亿 -> {score}分"


class D10箱体突破质量Scorer(ScorerBase):
    """P2新增：基于改进的check_box_breakout判定的质量评分"""
    name = "D10箱体突破质量"

    def compute(self, stock_data: dict) -> Tuple[int, str]:
        # 突破等级字段（由GUI提供，基于P0改进的分级体系）
        breakout_level = stock_data.get("breakout_level")  # signal/reliable/strong/None
        quality_score = stock_data.get("box_quality_score", 0) or 0  # 1-10分

        score = 0
        detail = ""

        if breakout_level == "strong":
            score = 2  # 强势突破：高概率后续
            detail = f"强势突破(质量{quality_score:.1f}/10) -> {score}分"
        elif breakout_level == "reliable":
            score = 1  # 可靠突破：推荐参考
            detail = f"可靠突破(质量{quality_score:.1f}/10) -> {score}分"
        elif breakout_level == "signal":
            score = 0  # 信号级：低可靠，不加分
            detail = f"信号级突破(质量{quality_score:.1f}/10，谨慎) -> {score}分"
        else:
            detail = "无箱体突破或突破不足0.5% -> 0分"

        return score, detail


class ConceptScorer:
    def __init__(self, dimensions: list = None):
        self.scorers = [
            D1强势形态且新高Scorer(),
            D2强势形态Scorer(),
            D4首板资金池Scorer(),
            D5潜在突破10日Scorer(),
            D6潜在突破5日Scorer(),
            D7持续性Scorer(),
            D8情绪分数Scorer(),
            D9活跃程度Scorer(),
            D10箱体突破质量Scorer(),  # P2新增
            大成交额Scorer(),
        ]
        if dimensions:
            self.scorers = [s for s in self.scorers if s.name in dimensions]

    def compute(self, stock_data: dict) -> Tuple[int, Dict[str, int], str]:
        total = 0
        details = {}
        detail_strs = []
        for scorer in self.scorers:
            score, detail = scorer.compute(stock_data)
            details[scorer.name] = score
            stock_data[scorer.name] = score  # 写入供后续 scorer 读取（D6依赖D5）
            total += score
            detail_strs.append(f"{scorer.name}={score}")
        return total, details, " | ".join(detail_strs)

    def compute_batch(self, stock_list: list) -> list:
        result = []
        for stock in stock_list:
            total, details, detail_str = self.compute(stock)
            stock_copy = dict(stock)
            stock_copy["总得分"] = total
            stock_copy["评分详情"] = detail_str
            for dim_name, score in details.items():
                stock_copy[dim_name] = score
            result.append(stock_copy)
        return result
