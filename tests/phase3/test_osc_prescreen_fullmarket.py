# -*- coding: utf-8 -*-
"""超跌跳空确认 · 预筛宇宙扩展（猎手池 → 全市场）单测 —— 2026-10-14。

## 钉什么

owner 2026-10-14 决策：OSC 选股宇宙从「猎手表里有韭研主板块的 ~1550 只」扩到
**全 A 股**（剔 ST/退市/北交所/B股），板块强度排名保留但按全市场口径现算。
本文件钉住这次扩展的**边界条件**（扩展错了就是买错票）：

  1. `full_market_meta` 剔除规则：ST / 退市 / 北交所(4/8/920) / B股(200/900) 不进宇宙；
  2. `universe_board_map` 标签优先级：猎手表韭研主板块**覆盖**申万行业（猎手池口径不变式），
     无韭研分类的票回退申万一级行业；
  3. `premarket_candidates` 扩展生效：不在猎手表里的全市场票，其申万行业进 top-N 且超跌
     ⇒ **必须进候选**（这是扩展存在的意义）；被剔除类别（ST/北交所）即使超跌也**不得**进候选；
  4. 退化路径：industry_map.csv 缺失 ⇒ 行为**逐码退回**扩展前的猎手池旧口径（不崩、不扩）。

⚠️ 标定提醒（与 prescreen 模块 docstring 同源）：exp24 的 86% 触发率、exp9 的 Sharpe 顶点
均基于猎手池标定，宇宙扩展后原标定结论需重验证 —— 本测试**只钉机制，不钉参数数值**。

运行：python -m unittest tests.phase3.test_osc_prescreen_fullmarket -v
"""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "execution" / "auto")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import prescreen                     # noqa: E402

_DATES = ["2026-09-%02d" % (d + 1) for d in range(25)]         # 25 根（MA20 预热够）


def _frame(closes):
    return pd.DataFrame({"date": _DATES[:len(closes)], "close": closes})


def _oversold(n=25):
    """缓跌序列 ⇒ 昨收 < MA20。"""
    return [20.0 - 0.1 * i for i in range(n)]


def _uptrend_lu(n=25):
    """上行 + 末日 +10%（贡献板块涨停家数）。"""
    xs = [10.0 + 0.1 * i for i in range(n)]
    xs[-1] = xs[-2] * 1.10
    return xs


class _FakeProvider(object):
    def __init__(self, frames):
        self.frames = frames

    def daily_many(self, codes, days=40):
        return {c: self.frames[c] for c in codes if c in self.frames}


class _Sandbox(unittest.TestCase):
    """把 prescreen 的两个数据源都换成沙箱：tmp industry_map.csv + fake sym2board_map。"""

    ROWS = [
        # 代码, 名称, 行业名称 —— 覆盖：普通A / ST / 退市 / 北交所(8/4/920) / B股(200/900)
        ("300001", "特锐德", "电力设备"),
        ("300002", "神州泰岳", "计算机"),
        ("300003", "乐普医疗", "医药生物"),
        ("300004", "ST南风", "基础化工"),
        ("300005", "探路退", "纺织服饰"),
        ("830001", "北交甲", "机械设备"),
        ("430002", "北交乙", "电子"),
        ("920003", "北交丙", "汽车"),
        ("200004", "深B甲", "交通运输"),
        ("900005", "沪B乙", "轻工制造"),
    ]
    WL = {"300001": "锂电池", "300004": "化工"}     # 猎手表（含一只 ST 股有韭研板块）

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="osc_um_")
        fp = os.path.join(self._tmp, "industry_map.csv")
        with open(fp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "行业名称"])
            w.writerows(self.ROWS)
        self._old_fp = prescreen._INDUSTRY_MAP_FP
        self._old_wl = prescreen.sym2board_map
        prescreen._INDUSTRY_MAP_FP = fp
        prescreen.sym2board_map = lambda: dict(self.WL)
        self.addCleanup(self._restore)

    def _restore(self):
        prescreen._INDUSTRY_MAP_FP = self._old_fp
        prescreen.sym2board_map = self._old_wl


class TestFullMarketMeta(_Sandbox):
    def test_excludes_st_delist_bj_and_b_shares(self):
        meta = prescreen.full_market_meta()
        self.assertIn("300001", meta)
        for bad in ("300004", "300005",                       # ST / 退
                    "830001", "430002", "920003",             # 北交所
                    "200004", "900005"):                      # B 股
            self.assertNotIn(bad, meta, f"{bad} 应被剔除出全市场宇宙")
        self.assertEqual(meta["300001"]["sw"], "电力设备")
        self.assertEqual(meta["300001"]["name"], "特锐德")

    def test_missing_file_returns_empty(self):
        prescreen._INDUSTRY_MAP_FP = os.path.join(self._tmp, "nope.csv")
        self.assertEqual(prescreen.full_market_meta(), {})


class TestUniverseBoardMap(_Sandbox):
    def test_jiuyan_board_wins_over_sw(self):
        m = prescreen.universe_board_map()
        self.assertEqual(m["300001"], "锂电池", "猎手表韭研主板块必须覆盖申万行业（口径不变式）")

    def test_non_watchlist_falls_back_to_sw(self):
        m = prescreen.universe_board_map()
        self.assertEqual(m["300002"], "计算机")
        self.assertEqual(m["300003"], "医药生物")

    def test_excluded_codes_absent(self):
        m = prescreen.universe_board_map()
        for bad in ("300005", "830001", "430002", "920003", "200004", "900005"):
            self.assertNotIn(bad, m)
        # ⚠️ 300004 是 ST、被 industry_map 剔除，但它在猎手表里有韭研板块 ⇒ 仍会进来。
        #    这是「猎手池口径不变式」的代价：猎手表自身的票池卫生由猎手表维护方负责，
        #    本层不二次过滤（与扩展前行为逐码一致）。
        self.assertEqual(m.get("300004"), "化工")

    def test_missing_industry_map_degrades_to_watchlist(self):
        prescreen._INDUSTRY_MAP_FP = os.path.join(self._tmp, "nope.csv")
        m = prescreen.universe_board_map()
        self.assertEqual(m, {c: b for c, b in self.WL.items() if b},
                         "industry_map 缺失 ⇒ 逐码退回扩展前猎手池旧口径")


class TestPremarketCandidatesFullMarket(_Sandbox):
    """端到端（假日线）：扩展前后候选池的差异必须正好是全市场新增部分。"""

    def _frames(self):
        return {
            # 两只「计算机」票（不在猎手表）：一只昨日涨停（把计算机顶成热门）、一只超跌
            "300002": _frame(_oversold()),                    # 超跌 ⇒ 应成候选（扩展新增）
            "300003": _frame(_uptrend_lu()),                  # 医药生物：1 家涨停 + 上行（非超跌）
            "300005": _frame(_oversold()),                    # 退市股：超跌也不得进候选
            "830001": _frame(_oversold()),                    # 北交所：超跌也不得进候选
        }

    def test_fullmarket_candidate_included_excluded_categories_blocked(self):
        frames = self._frames()
        frames["300009"] = _frame(_uptrend_lu())              # 计算机涨停票（把「计算机」顶成热门板块）
        prescreen.sym2board_map = lambda: {**self.WL, "300009": "计算机"}
        cand = prescreen.premarket_candidates(5, "2026-10-14",
                                              provider=_FakeProvider(frames))
        self.assertIn("300002", cand, "全市场超跌票（申万行业进 top-N）必须进候选 —— 扩展的意义")
        self.assertEqual(cand["300002"]["board"], "计算机")
        self.assertEqual(cand["300002"]["name"], "神州泰岳")
        self.assertNotIn("300003", cand, "上行票提供涨停家数、但自己不是超跌票")
        self.assertNotIn("300005", cand, "退市股被宇宙剔除，超跌也不得进候选")
        self.assertNotIn("830001", cand, "北交所被宇宙剔除，超跌也不得进候选")

    def test_missing_industry_map_degrades_to_old_behavior(self):
        prescreen._INDUSTRY_MAP_FP = os.path.join(self._tmp, "nope.csv")
        frames = self._frames()
        frames["300001"] = _frame(_oversold())                # 猎手表票（锂电池），超跌
        frames["300006"] = _frame(_uptrend_lu())              # 无板块涨停票（不计数）
        prescreen.sym2board_map = lambda: {**self.WL, "300006": "锂电池"}
        cand = prescreen.premarket_candidates(5, "2026-10-14",
                                              provider=_FakeProvider(frames))
        self.assertIn("300001", cand, "猎手表超跌票应进候选（旧行为不变）")
        self.assertNotIn("300002", cand,
                         "industry_map 缺失 ⇒ 退回旧口径：非猎手表票不得进候选")


if __name__ == "__main__":
    unittest.main(verbosity=2)
