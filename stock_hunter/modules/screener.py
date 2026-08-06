# -*- coding: utf-8 -*-
"""
多维选股筛选器 v1.0
职责：基于 watchlist_jiuyan.json 的分类体系进行多维度股票筛选
支持：
  - 按 category（一级分类）过滤
  - 按 concept（概念层级）精准/模糊匹配
  - 多条件 AND / OR 组合
  - 行情数据过滤（涨停、涨跌幅、成交额）
  - 交集分析（自动发现跨板块标的）
  - 结果排序与 CSV 导出

扩展接口：继承 ScreenerBase 可接入新数据源
"""
import os
import json
import pandas as pd
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set, Tuple


class ScreenerBase(ABC):
    """筛选器基类"""

    @abstractmethod
    def filter(self, **kwargs) -> pd.DataFrame:
        pass


class WatchlistScreener(ScreenerBase):
    """
    基于 watchlist_jiuyan.json 的多维选股器
    """

    def __init__(self, watchlist_path: str = None, config_path: str = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._watchlist_path = watchlist_path or os.path.join(base_dir, "watchlist_jiuyan.json")
        self._df: Optional[pd.DataFrame] = None
        self._raw: Optional[dict] = None
        self._name_to_codes: Optional[Dict[str, List[str]]] = None

    # ── 数据加载 ──────────────────────────────

    def _load(self):
        """加载 watchlist 并展开为 DataFrame（复用 DataLoader 逻辑）"""
        if self._df is not None:
            return

        with open(self._watchlist_path, "r", encoding="utf-8") as f:
            self._raw = json.load(f)

        records = []
        self._name_to_codes = {}

        for code, info in self._raw.items():
            if not isinstance(info, dict):
                continue
            name = str(info.get("name", ""))
            if name:
                self._name_to_codes.setdefault(name, []).append(code)

            base = {
                "代码": code,
                "名称": name,
                "板块": str(info.get("sector", "")),
                "主营业务": str(info.get("business_summary", "")),
                "来源": str(info.get("primary_source", "")),
                "更新时间": str(info.get("updated_at", "")),
            }

            # 收集所有 category/concept 对
            pairs = []
            for i in range(1, 10):
                cat = info.get(f"jiuyan_category{i}", "")
                concept = info.get(f"jiuyan_concept{i}", "")
                if cat and str(cat).strip():
                    pairs.append((str(cat).strip(), str(concept).strip()))

            if not pairs:
                # 回退到旧字段
                cat = str(info.get("jiuyan_category", ""))
                concept = str(info.get("jiuyan_concept", ""))
                if cat and cat.strip():
                    for c in cat.split("|"):
                        c = c.strip()
                        if c:
                            pairs.append((c, concept.strip()))

            if pairs:
                for cat, concept in pairs:
                    row = base.copy()
                    row["韭研分类"] = cat
                    row["韭研概念"] = concept
                    records.append(row)
            else:
                row = base.copy()
                row["韭研分类"] = ""
                row["韭研概念"] = ""
                records.append(row)

        self._df = pd.DataFrame(records)

    @property
    def df(self) -> pd.DataFrame:
        self._load()
        return self._df

    @property
    def name_to_codes(self) -> Dict[str, List[str]]:
        self._load()
        return self._name_to_codes

    def get_code(self, name: str) -> Optional[str]:
        """通过股票名称查代码"""
        codes = self.name_to_codes.get(name, [])
        return codes[0] if codes else None

    # ── 筛选方法 ──────────────────────────────

    def filter(
        self,
        categories: List[str] = None,
        concepts: List[str] = None,
        mode: str = "AND",
        min_score: float = None,
        limit_up_only: bool = False,
        min_change: float = None,
        max_change: float = None,
        min_volume: float = None,  # 单位：元
        exclude_st: bool = True,
    ) -> pd.DataFrame:
        """
        多维度筛选。

        参数
        ----
        categories : 分类列表，匹配任一即可（OR 逻辑），
                     若 mode='AND' 则要求同时满足所有 category。
        concepts : 概念关键词列表（模糊匹配 concept 字段），
                   每个关键词独立匹配，OR 逻辑。
        mode : 'AND' — categories 和 concepts 必须同时满足
               'OR'  — 满足 categories 或 concepts 任一即可
        min_score : 最低得分（需在外部评分后使用）
        limit_up_only : 仅保留涨停标的
        min_change / max_change : 涨跌幅区间
        min_volume : 最低成交额（元）
        exclude_st : 排除 ST/*ST

        返回
        ----
        pd.DataFrame，列含：代码, 名称, 韭研分类, 韭研概念, 板块, 来源
        """
        self._load()
        df = self._df.copy()

        # ST 过滤
        if exclude_st:
            st_mask = df["名称"].str.startswith(("*ST", "ST", "SST", "S*ST")).fillna(False)
            df = df[~st_mask]

        # Category 过滤
        if categories:
            cats = [c.strip() for c in categories if c.strip()]
            if cats:
                if mode == "AND":
                    # 必须同时属于所有 category → 找同时在所有 category 中的代码
                    code_sets = []
                    for cat in cats:
                        codes_in_cat = set(df[df["韭研分类"] == cat]["代码"].unique())
                        code_sets.append(codes_in_cat)
                    valid_codes = code_sets[0]
                    for cs in code_sets[1:]:
                        valid_codes = valid_codes & cs
                    df = df[df["代码"].isin(valid_codes)]
                else:
                    # OR：匹配任一
                    df = df[df["韭研分类"].isin(cats)]

        # Concept 过滤（模糊匹配）
        if concepts:
            for keyword in concepts:
                kw = keyword.strip()
                if kw:
                    df = df[df["韭研概念"].str.contains(kw, na=False)]

        # 行情过滤（如果 DataFrame 中有这些列）
        if limit_up_only and "涨停" in df.columns:
            df = df[df["涨停"] > 0]

        if min_score is not None and "总得分" in df.columns:
            df = df[df["总得分"] >= min_score]

        if min_change is not None and "涨跌幅" in df.columns:
            df = df[df["涨跌幅"] >= min_change]

        if max_change is not None and "涨跌幅" in df.columns:
            df = df[df["涨跌幅"] <= max_change]

        if min_volume is not None and "成交额" in df.columns:
            df = df[df["成交额"] >= min_volume]

        return df.reset_index(drop=True)

    # ── 交集分析 (P1) ──────────────────────────

    def intersect(
        self,
        category_pairs: List[Tuple[str, str]] = None,
        top_categories: List[str] = None,
    ) -> pd.DataFrame:
        """
        分析分类交集：找出同时属于多个 category 的标的。

        参数
        ----
        category_pairs : 指定要分析的交集对，如 [("电力", "央企"), ("半导体", "AI应用")]
        top_categories : 自动分析前 N 个分类之间的所有两两交集

        返回
        ----
        pd.DataFrame，列含：分类A, 分类B, 交集数量, 交集标的（代码+名称）
        """
        self._load()
        pool = self._df[self._df["韭研分类"].str.strip().ne("")]

        if top_categories:
            top = pool["韭研分类"].value_counts().head(top_categories).index.tolist()
            pairs = []
            for i in range(len(top)):
                for j in range(i + 1, len(top)):
                    pairs.append((top[i], top[j]))

        if not pairs:
            return pd.DataFrame()

        rows = []
        # 预计算每个 category 的代码集合
        cat_codes: Dict[str, Set[str]] = {}
        for cat in set(p[0] for p in pairs) | set(p[1] for p in pairs):
            cat_codes[cat] = set(pool[pool["韭研分类"] == cat]["代码"].unique())

        for cat_a, cat_b in pairs:
            codes_a = cat_codes.get(cat_a, set())
            codes_b = cat_codes.get(cat_b, set())
            intersect_codes = codes_a & codes_b

            if intersect_codes:
                # 获取交集标的的名称
                stock_info = []
                for code in sorted(intersect_codes):
                    name = self._raw.get(code, {}).get("name", "?") if self._raw else "?"
                    stock_info.append(f"{code} {name}")

                rows.append({
                    "分类A": cat_a,
                    "分类B": cat_b,
                    "交集数量": len(intersect_codes),
                    "交集标的": " | ".join(stock_info),
                    "标的列表": list(intersect_codes),
                })

        return pd.DataFrame(rows).sort_values("交集数量", ascending=False).reset_index(drop=True) \
            if rows else pd.DataFrame()

    def bridge_stocks(self, cat_a: str, cat_b: str) -> pd.DataFrame:
        """返回同时属于 cat_a 和 cat_b 的标的详情"""
        self._load()
        pool = self._df[self._df["韭研分类"].str.strip().ne("")]
        codes_a = set(pool[pool["韭研分类"] == cat_a]["代码"].unique())
        codes_b = set(pool[pool["韭研分类"] == cat_b]["代码"].unique())
        bridge = codes_a & codes_b
        return pool[pool["代码"].isin(bridge)].drop_duplicates(subset=["代码"]) \
            .sort_values("代码").reset_index(drop=True)

    # ── 统计与导出 ────────────────────────────

    def category_stats(self) -> pd.DataFrame:
        """各分类的标的数量统计"""
        self._load()
        pool = self._df[self._df["韭研分类"].str.strip().ne("")]
        stats = pool.groupby("韭研分类").agg(
            标的数量=("代码", "nunique"),
            概念数量=("韭研概念", lambda x: x[x.str.strip().ne("")].nunique()),
        ).sort_values("标的数量", ascending=False).reset_index()
        stats.index = range(1, len(stats) + 1)
        stats.index.name = "排名"
        return stats

    def get_stock_categories(self, code: str) -> pd.DataFrame:
        """获取某只标的的所有分类和概念"""
        self._load()
        return self._df[self._df["代码"] == code][["代码", "名称", "韭研分类", "韭研概念"]]

    def to_csv(self, df: pd.DataFrame, path: str):
        """导出筛选结果为 CSV"""
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[导出] 已保存 {len(df)} 条记录到 {path}")


# ── 便捷函数 ──────────────────────────────────

def screen(
    categories: List[str] = None,
    concepts: List[str] = None,
    mode: str = "AND",
    limit_up_only: bool = False,
    top: int = None,
    **kwargs,
) -> pd.DataFrame:
    """
    一行式选股便捷函数。
    用法: screen(categories=["电力", "央企"], limit_up_only=True)
    """
    s = WatchlistScreener()
    result = s.filter(categories=categories, concepts=concepts, mode=mode,
                      limit_up_only=limit_up_only, **kwargs)
    if top:
        result = result.head(top)
    return result


def intersect_matrix(top_n: int = 10) -> pd.DataFrame:
    """输出前 N 大分类的交集矩阵"""
    s = WatchlistScreener()
    return s.intersect(top_categories=top_n)


# ── 命令行入口（供直接 python -m modules.screener 调用）──
if __name__ == "__main__":
    import sys

    s = WatchlistScreener()

    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(s.category_stats().to_string())
    elif len(sys.argv) > 1 and sys.argv[1] == "intersect":
        df = s.intersect(top_categories=10)
        if not df.empty:
            for _, row in df.iterrows():
                print(f"\n{row['分类A']} ∩ {row['分类B']}: {row['交集数量']}只")
                print(f"  {row['交集标的'][:200]}...")
    else:
        # 默认展示分类统计 + 部分交集
        print("=== 分类统计 ===")
        print(s.category_stats().head(15).to_string())
        print("\n=== TOP10 分类交集 ===")
        df = s.intersect(top_categories=10)
        if not df.empty:
            for _, row in df.head(10).iterrows():
                print(f"  {row['分类A']} ∩ {row['分类B']}: {row['交集数量']}只")
