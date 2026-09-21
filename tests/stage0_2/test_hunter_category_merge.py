# -*- coding: utf-8 -*-
"""选股猎手 · 板块合并单测（2026-09-21）。

## 为什么要有这个测试

owner 需求：「创新药概念和中药概念合并到医药，创新药/中药作为其子概念」。
实现在 `stock_hunter/modules/data_loader.py`：`category_merge` 把命中的细板块名
改写成父板块，原板块名落到「原分类」列，下游（概念总排名 / 板块明细 / 热度 / 飞书）
统一按父板块聚合，并把「原分类」当作子概念。

三处最容易悄悄回归且肉眼难发现：
1. 只改了板块名、忘了让子概念用「原分类」→ 医药下仍是一堆细标签（种植/百年老字号…）
   —— 与 owner 要的「两层」不符。
2. 只改了 `load_all_sectors`、漏了 `load_concept_summary` → 总排名里 医药 的
   「细分数量」按细标签数算（几十个），与实际两个子概念不一致。
3. 未命中的板块被误伤（半导体等）→ 影响面远超预期。

本测试全用临时 watchlist，**不读真实数据、不联网**。

运行：python tests/stage0_2/test_hunter_category_merge.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HUNTER = os.path.join(_ROOT, "stock_hunter")
for _p in (_ROOT, _HUNTER):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.data_loader import DEFAULT_CATEGORY_MERGE, WatchlistLoader  # noqa: E402

# 三只样本：中药（多细标签）、创新药（单细标签）、半导体（未命中，作对照）
SAMPLE = {
    "000538": {"name": "云南白药", "jiuyan_category": "中药",
               "jiuyan_concept": "种植|民族药|百年老字号|中药饮片", "sector": "医药行业"},
    "300204": {"name": "舒泰神", "jiuyan_category": "创新药",
               "jiuyan_concept": "创新药", "sector": "医药行业"},
    "600584": {"name": "长电科技", "jiuyan_category": "半导体",
               "jiuyan_concept": "封测|先进封装", "sector": "半导体行业"},
}


def _loader(merge=None):
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "watchlist_jiuyan.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(SAMPLE, f, ensure_ascii=False)
        return WatchlistLoader(category_merge=merge).load(fp)


class TestCategoryMerge(unittest.TestCase):
    def test_default_mapping_is_medicine(self):
        self.assertEqual(DEFAULT_CATEGORY_MERGE, {"创新药": "医药", "中药": "医药"})

    def test_merged_rows_carry_parent_and_original(self):
        df = _loader()
        by_code = {r["代码"]: r for _, r in df.iterrows()}
        for code, orig in (("000538", "中药"), ("300204", "创新药")):
            self.assertEqual(by_code[code]["韭研分类"], "医药", f"{code} 应并入医药")
            self.assertEqual(by_code[code]["原分类"], orig, f"{code} 原分类应保留 {orig}")
        # 未命中的板块不该被动
        self.assertEqual(by_code["600584"]["韭研分类"], "半导体")
        self.assertEqual(by_code["600584"]["原分类"], "")

    def test_original_concept_text_kept_for_explanation(self):
        """细标签要留在「韭研概念」里做解释，不能被丢掉。"""
        df = _loader()
        row = df[df["代码"] == "000538"].iloc[0]
        self.assertIn("百年老字号", row["韭研概念"])

    def test_custom_mapping_overrides_default(self):
        df = _loader({"中药": "中医药", "创新药": "生物医药"})
        cats = set(df["韭研分类"])
        self.assertIn("中医药", cats)
        self.assertIn("生物医药", cats)
        self.assertNotIn("医药", cats, "自定义映射应覆盖默认（否则说明默认被无条件叠加）")

    def test_data_loader_subconcept_is_original_category(self):
        """端到端：DataLoader 的板块明细里，医药的子概念应为 创新药/中药 两个。"""
        from modules.data_loader import DataLoader
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "watchlist_jiuyan.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(SAMPLE, f, ensure_ascii=False)
            dl = DataLoader(config={"category_merge": DEFAULT_CATEGORY_MERGE})
            dl._watchlist_path = fp
            sectors = dl.load_all_sectors()
            summary = dl.load_concept_summary()

        self.assertIn("医药", sectors)
        self.assertNotIn("中药", sectors)
        self.assertNotIn("创新药", sectors)
        self.assertEqual(sorted(sectors["医药"]["子概念"].unique()), ["中药", "创新药"])
        # 对照板块不受影响
        self.assertIn("半导体", sectors)

        med = summary[summary["板块"] == "医药"]
        self.assertEqual(len(med), 1, "医药 应只有一行")
        self.assertEqual(int(med.iloc[0]["细分数量"]), 2,
                         "医药的细分数量应为两个子概念（创新药/中药），而不是细标签个数")
        self.assertFalse(summary["板块"].isin(["中药", "创新药"]).any(),
                         "中药/创新药 不应再作为独立板块出现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
