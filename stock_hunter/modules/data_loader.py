# -*- coding: utf-8 -*-
"""
数据加载模块 —— 以 watchlist_jiuyan.json 为唯一数据源
职责：从脚本目录下的 watchlist_jiuyan.json 读取，生成概念总排名、详细个股排名、板块明细
扩展接口：继承 DataLoaderBase 可接入新数据源（数据库、API 等）
"""
import os
import json
import pandas as pd
from abc import ABC, abstractmethod
from typing import Dict, Optional, List


class DataLoaderBase(ABC):
    """数据加载器基类"""

    @abstractmethod
    def load(self, source: str) -> pd.DataFrame:
        pass


class WatchlistLoader(DataLoaderBase):
    """watchlist_jiuyan.json 专用加载器
    支持多 category/concept 对：jiuyan_category1/concept1, jiuyan_category2/concept2, ...
    每个 category/concept 对生成一行，确保统计时精确对应
    """

    def load(self, source: str) -> pd.DataFrame:
        if not os.path.exists(source):
            raise FileNotFoundError(f"watchlist 文件不存在: {source}")

        with open(source, "r", encoding="utf-8") as f:
            raw = json.load(f)

        records = []
        for code, info in raw.items():
            if not isinstance(info, dict):
                continue
            # 安全提取字段，确保所有值都是字符串
            def _safe_str(val):
                if val is None:
                    return ""
                if isinstance(val, list):
                    return ",".join(str(v) for v in val)
                return str(val)

            # 基础信息
            base_record = {
                "代码": code,
                "名称": _safe_str(info.get("name", "")),
                "板块": _safe_str(info.get("sector", "")),
                "主营业务": _safe_str(info.get("business_summary", "")),
                "概念板块": _safe_str(info.get("concept_boards", [])),
                "行业板块": _safe_str(info.get("industry_boards", [])),
                "来源": _safe_str(info.get("primary_source", "")),
                "更新时间": _safe_str(info.get("updated_at", "")),
            }

            # 收集所有 category/concept 对（精确对应）
            pairs = []
            has_multi_fields = False
            for i in range(1, 10):  # 最多支持 9 个 category
                cat_key = f"jiuyan_category{i}"
                concept_key = f"jiuyan_concept{i}"
                cat = info.get(cat_key, "")
                concept = info.get(concept_key, "")
                if cat and str(cat).strip():
                    pairs.append((str(cat).strip(), str(concept).strip()))
                    has_multi_fields = True

            if not pairs:
                # 回退到旧字段：单个 jiuyan_category / jiuyan_concept
                cat = info.get("jiuyan_category", "")
                concept = info.get("jiuyan_concept", "")
                if cat and str(cat).strip():
                    # 按 | 拆分 category（兼容旧数据）
                    cats = [c.strip() for c in str(cat).split("|") if c.strip()]
                    for c in cats:
                        pairs.append((c, str(concept).strip()))

            # 为每个 category/concept 对生成一行
            if pairs:
                for cat, concept in pairs:
                    record = base_record.copy()
                    record["韭研分类"] = cat
                    record["韭研概念"] = concept
                    records.append(record)
            else:
                # 回退到旧字段：单个 jiuyan_category / jiuyan_concept
                cat = info.get("jiuyan_category", "")
                concept = info.get("jiuyan_concept", "")
                if cat and str(cat).strip():
                    # 按 | 拆分 category（兼容旧数据）
                    cats = [c.strip() for c in str(cat).split("|") if c.strip()]
                    for c in cats:
                        record = base_record.copy()
                        record["韭研分类"] = c
                        record["韭研概念"] = str(concept).strip()
                        records.append(record)
                else:
                    # 无韭研概念，保留一行（空值）
                    record = base_record.copy()
                    record["韭研分类"] = ""
                    record["韭研概念"] = ""
                    records.append(record)

        return pd.DataFrame(records)


class DataLoader:
    """
    统一数据加载入口 —— 以 watchlist_jiuyan.json 为唯一数据源
    """

    def __init__(self, data_dir: str = None, config: dict = None):
        if config is None:
            config = self._load_config()
        self.config = config
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = data_dir or config.get("base_dir", ".") + "/" + config.get("data_dir", "data")
        project_root = os.path.dirname(self.base_dir)
        self._watchlist_path = os.path.join(project_root, "watchlist_jiuyan.json")
        self._watchlist_df: Optional[pd.DataFrame] = None

    @staticmethod
    def _load_config() -> dict:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_watchlist(self) -> pd.DataFrame:
        """内部缓存：只加载一次 watchlist"""
        if self._watchlist_df is not None:
            return self._watchlist_df

        if not os.path.exists(self._watchlist_path):
            raise FileNotFoundError(
                f"watchlist_jiuyan.json 未在脚本目录找到: {self._watchlist_path}\n"
                f"请确保文件已拷贝到脚本同级目录。"
            )

        loader = WatchlistLoader()
        self._watchlist_df = loader.load(self._watchlist_path)
        print(f"  [OK] watchlist_jiuyan.json 加载完成: {len(self._watchlist_df)} 只标的")
        # 统计有韭研概念的标的数量
        has_concept = self._watchlist_df["韭研概念"].str.strip().ne("").sum()
        print(f"     含韭研概念标签: {has_concept} 只（进入打分池）")
        return self._watchlist_df

    def set_watchlist(self, df: pd.DataFrame):
        """外部注入：允许主流程将合并行情后的 watchlist 传回"""
        self._watchlist_df = df

    def load_watchlist(self) -> Optional[pd.DataFrame]:
        """对外接口：加载完整 watchlist"""
        try:
            print(f"[HUNTER] Attempting to load watchlist from: {self._watchlist_path}")
            result = self._load_watchlist()
            print(f"[HUNTER] Watchlist loaded successfully")
            return result
        except FileNotFoundError as e:
            print(f"  [ERROR] {e}")
            print(f"[HUNTER] Watchlist file not found at: {self._watchlist_path}")
            import os
            print(f"[HUNTER] Directory contents: {os.listdir(os.path.dirname(self._watchlist_path))}")
            return None
        except Exception as e:
            print(f"  [ERROR] Unexpected error loading watchlist: {e}")
            import traceback
            traceback.print_exc()
            return None

    def load_concept_summary(self) -> pd.DataFrame:
        """
        生成 Sheet 1: 概念总排名
        按 jiuyan_category 分组统计，使用总得分计算平均分
        注意：WatchlistLoader 已将多 category 股票拆分为多行，每行对应一个精确的 category/concept 对
        """
        df = self._load_watchlist()
        df_pool = df[df["韭研概念"].str.strip().ne("")].copy()

        if df_pool.empty:
            return pd.DataFrame()

        summary_rows = []
        # 直接按韭研分类分组（已精确拆分，无需 explode）
        for category in sorted(df_pool["韭研分类"].unique()):
            if not category:
                continue
            cat_df = df_pool[df_pool["韭研分类"] == category].copy()
            # 收集该分类下的所有细分概念（去重）
            sub_concepts = set()
            for sc in cat_df["韭研概念"].dropna():
                for c in str(sc).split("|"):
                    c = c.strip()
                    if c:
                        sub_concepts.add(c)
            sub_concepts = sorted(sub_concepts)

            stock_count = len(cat_df)

            # 使用总得分（D1-D8评分规则）计算平均分
            if "总得分" in cat_df.columns and cat_df["总得分"].notna().any():
                total_score = int(cat_df["总得分"].fillna(0).sum())
                avg_score = round(cat_df["总得分"].fillna(0).mean(), 2)
                max_score = int(cat_df["总得分"].fillna(0).max())
                limit_up_count = int(cat_df["涨停"].fillna(0).sum()) if "涨停" in cat_df.columns else 0
                avg_amount = round(cat_df["成交额"].fillna(0).mean(), 2) if "成交额" in cat_df.columns else 0
            elif "涨跌幅" in cat_df.columns and cat_df["涨跌幅"].notna().any():
                # 回退：无总得分时使用涨跌幅*10（兼容旧数据）
                total_score = int(cat_df["涨跌幅"].fillna(0).sum() * 10)
                avg_score = round(cat_df["涨跌幅"].fillna(0).mean() * 10, 2)
                max_score = int(cat_df["涨跌幅"].fillna(0).max() * 10)
                limit_up_count = int(cat_df["涨停"].fillna(0).sum()) if "涨停" in cat_df.columns else 0
                avg_amount = round(cat_df["成交额"].fillna(0).mean(), 2) if "成交额" in cat_df.columns else 0
            else:
                # 无行情数据时回退到基础分
                total_score = stock_count * 10
                avg_score = 10.0
                max_score = 10
                limit_up_count = 0
                avg_amount = 0

            # 找到最强细分（按总得分）
            if "总得分" in cat_df.columns and cat_df["总得分"].notna().any():
                cat_df_sorted = cat_df.sort_values("总得分", ascending=False).reset_index(drop=True)
                best_stock = cat_df_sorted.iloc[0]
                best_score = int(best_stock.get("总得分", 0))
            elif "涨跌幅" in cat_df.columns and cat_df["涨跌幅"].notna().any():
                cat_df_sorted = cat_df.sort_values("涨跌幅", ascending=False).reset_index(drop=True)
                best_stock = cat_df_sorted.iloc[0]
                _change = best_stock.get("涨跌幅", 0)
                best_score = int(0 if pd.isna(_change) else float(_change) * 10)
            else:
                cat_df_sorted = cat_df.sort_values("名称", ascending=True).reset_index(drop=True)
                best_stock = cat_df.iloc[0]
                best_score = 10

            summary_rows.append({
                "排名": 0,
                "板块": category,
                "细分数量": len(sub_concepts),
                "平均分": avg_score,
                "涨停数": limit_up_count,
                "最强细分": sub_concepts[0] if len(sub_concepts) > 0 else "",
                "最强细分得分": best_score,
                "前三强": " | ".join([
                    f"{i+1}.{row['名称']}({row['代码']})-{int(row.get('总得分', 0) if '总得分' in row and pd.notna(row.get('总得分')) else 0)}分"
                    for i, (_, row) in enumerate(cat_df_sorted.head(3).iterrows())
                ]),
            })

        summary_df = pd.DataFrame(summary_rows)
        summary_df = summary_df.sort_values("平均分", ascending=False).reset_index(drop=True)
        summary_df["排名"] = range(1, len(summary_df) + 1)
        return summary_df

    def load_detail_ranking(self) -> pd.DataFrame:
        """
        生成 Sheet 2: 概念排名
        按拆分后的韭研概念值分组（jiuyan_concept 按 | 拆分，再按 - 拆分取前半部分），按平均分排序
        列：排名 | 概念 | 股票数量 | 平均分 | 最高分 | 涨停数
        """
        df = self._load_watchlist()
        df_pool = df[df["韭研概念"].str.strip().ne("")].copy()

        if df_pool.empty:
            return pd.DataFrame()

        rows = []
        for _, row in df_pool.iterrows():
            jiuyan_concept_raw = str(row.get("韭研概念", "")).strip()
            if not jiuyan_concept_raw:
                continue
            # 按 | 拆分为子概念
            sub_concepts = [c.strip() for c in jiuyan_concept_raw.split("|") if c.strip()]
            # 对每个子概念按 - 拆分，取前半部分作为分类概念（如"算力芯片-GPU"→"算力芯片"）
            category_concepts = []
            for c in sub_concepts:
                if "-" in c:
                    category_concepts.append(c.split("-")[0].strip())
                else:
                    category_concepts.append(c)
            # 去重
            category_concepts = list(dict.fromkeys(category_concepts))

            score = int(row.get("总得分", 0)) if pd.notna(row.get("总得分", 0)) else 10
            limit_up = int(row.get("涨停", 0)) if pd.notna(row.get("涨停", 0)) else 0
            amount = row.get("成交额", 0) if pd.notna(row.get("成交额", 0)) else 0

            for concept in category_concepts:
                rows.append({
                    "概念": concept,
                    "代码": row.get("代码", ""),
                    "总得分": score,
                    "涨停": limit_up,
                    "成交额": amount,
                })

        df_detail = pd.DataFrame(rows)

        # 按分类概念汇总
        concept_stats = df_detail.groupby("概念").agg({
            "总得分": ["count", "mean", "max", "sum"],
            "涨停": "sum",
            "成交额": "mean",
        }).reset_index()
        concept_stats.columns = ["概念", "股票数量", "平均分", "最高分", "总得分", "涨停数", "平均成交额"]
        concept_stats = concept_stats.sort_values("平均分", ascending=False).reset_index(drop=True)
        concept_stats["排名"] = range(1, len(concept_stats) + 1)
        return concept_stats[["排名", "概念", "股票数量", "平均分", "最高分", "涨停数"]]

    def load_all_sectors(self) -> Dict[str, pd.DataFrame]:
        """
        生成各板块明细 Sheet
        按 jiuyan_category 分组，使用精确的 jiuyan_concept 作为所属板块
        注意：WatchlistLoader 已将多 category 股票拆分为多行，每行对应一个精确的 category/concept 对
        """
        df = self._load_watchlist()
        df_pool = df[df["韭研概念"].str.strip().ne("")].copy()

        if df_pool.empty:
            return {}

        sectors = {}
        # 直接按韭研分类分组（已精确拆分，无需 explode）
        for category in sorted(df_pool["韭研分类"].unique()):
            if not category:
                continue
            cat_df = df_pool[df_pool["韭研分类"] == category].copy()

            # 如果有总得分数据，按总得分降序
            if "总得分" in cat_df.columns and cat_df["总得分"].notna().any():
                cat_df = cat_df.sort_values("总得分", ascending=False).reset_index(drop=True)
            else:
                cat_df = cat_df.sort_values("代码").reset_index(drop=True)

            rows = []
            for idx, row in cat_df.iterrows():
                if "涨跌幅" in row and pd.notna(row.get("涨跌幅")):
                    _change = row.get("涨跌幅", 0)
                    score = int(0 if pd.isna(_change) else float(_change) * 10)
                    top_score = score
                    limit_up = int(row.get("涨停", 0)) if pd.notna(row.get("涨停")) else 0
                    amount = row.get("成交额", 0) if pd.notna(row.get("成交额")) else 0
                else:
                    score = 10
                    top_score = 0
                    limit_up = 0
                    amount = 0

                # 使用拆分后的 jiuyan_concept 前半部分作为子概念分类（如"算力芯片-GPU"→"算力芯片"）
                # 但个股明细中保留完整概念作为解释
                jiuyan_concept_raw = str(row.get("韭研概念", "")).strip()
                if not jiuyan_concept_raw:
                    continue
                sub_concepts = [c.strip() for c in jiuyan_concept_raw.split("|") if c.strip()]
                category_concepts = []
                for c in sub_concepts:
                    if "-" in c:
                        category_concepts.append(c.split("-")[0].strip())
                    else:
                        category_concepts.append(c)
                category_concepts = list(dict.fromkeys(category_concepts))

                for concept in category_concepts:
                    rows.append({
                        "排名": idx + 1,
                        "代码": row.get("代码", ""),
                        "名称": row.get("名称", ""),
                        "所属板块": jiuyan_concept_raw,
                        "子概念": concept,
                        "总得分": int(row.get("总得分", score)),
                        "涨跌幅": row.get("涨跌幅", 0) if pd.notna(row.get("涨跌幅")) else 0,
                        "涨停": int(row.get("涨停", 0)) if pd.notna(row.get("涨停")) else 0,
                        "成交额": row.get("成交额", amount),
                        "D1强势形态且新高": int(row.get("D1强势形态且新高", 0)),
                        "D2强势形态": int(row.get("D2强势形态", 0)),
                        "D4首板资金池": int(row.get("D4首板资金池", 0)),
                        "D5潜在突破10日": int(row.get("D5潜在突破10日", 0)),
                        "D6潜在突破5日": int(row.get("D6潜在突破5日", 0)),
                        "D7持续性": int(row.get("D7持续性", 0)),
                        "D8情绪分数": int(row.get("D8情绪分数", 0)),
                        "D9活跃程度": int(row.get("D9活跃程度", 0)),
                        "大成交额额外加分": int(row.get("大成交额额外加分", 0)),
                    })
            sectors[category] = pd.DataFrame(rows)

        return sectors

    def load_six_dim_scores(self) -> Optional[pd.DataFrame]:
        """
        六维评分 —— 当前 watchlist 无此数据，返回 None
        后续可从外部数据源接入
        """
        return None


# --------------- 扩展接口示例（注释） ---------------
# class DatabaseLoader(DataLoaderBase):
#     """数据库数据源示例"""
#     def __init__(self, conn_str: str):
#         self.conn_str = conn_str
#     def load(self, source: str) -> pd.DataFrame:
#         import sqlalchemy
#         engine = sqlalchemy.create_engine(self.conn_str)
#         return pd.read_sql(source, engine)
#
# 使用方式：
# loader = DataLoader()
# loader._loaders[".sql"] = DatabaseLoader("mysql://user:pwd@host/db")
