# -*- coding: utf-8 -*-
"""超跌跳空确认 · 预筛 top-N 板块 tie-break 稳定化单测 —— 2026-10-16。

## 钉什么

背景（doc/research/2026-10-16_全市场宇宙标定重验证_干跑357vs实跑176.md）：
大涨家数稀疏日，「前一日大涨(≥9.5%)家数 top-N 板块」的第 4/5 名由个位数家数 +
`Counter.most_common` 的**插入序破 tie** 决定；插入序受 industry_map.csv 行序、
猎手表覆盖序、取数抖动影响 ⇒ 干跑/实跑破出不同板块，候选量摆动 ~2x（357 vs 176）。

2026-10-16 加固（owner 已拍板）：`prescreen.premarket_candidates` 的板块排名改为
确定性二次排序键「**家数降序 + 板块名升序**」，同一输入任何环境下名次唯一确定。

本文件钉住：
  1. **并列破序确定性**：多个板块家数并列、跨过 top-N 边界时，入选板块由板块名
     升序决定，与 industry_map.csv 行序（插入序）无关 —— 正序/逆序/交错序三种
     行序下候选集逐码一致；
  2. **同输入可复现**：同一输入跑两遍结果完全一致；
  3. **家数优先于板块名**：家数更多的板块即使名字靠后也排在前面（排序键主键未变）。

运行：python -m unittest tests.phase3.test_osc_prescreen_tiebreak -v
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
    """上行 + 末日 +10%（贡献板块大涨家数）。"""
    xs = [10.0 + 0.1 * i for i in range(n)]
    xs[-1] = xs[-2] * 1.10
    return xs


class _FakeProvider(object):
    def __init__(self, frames):
        self.frames = frames

    def daily_many(self, codes, days=40):
        return {c: self.frames[c] for c in codes if c in self.frames}


# 板块构造（top_n=3）：
#   Alpha 大涨 3 家 ⇒ 第 1 名（无并列）
#   Beta / Gamma / Delta 各大涨 2 家 ⇒ 并列争第 2、3 名（破 tie 现场）
# 确定性键「家数降序 + 板块名升序」⇒ 入选 = Alpha, Beta, Delta；Gamma 被挤出。
# 每个板块另挂 1 只超跌票，候选集 = 各入选板块的超跌票。
_BOARDS = {
    "Alpha": {"lu": ["300101", "300102", "300103"], "os": "300100"},
    "Beta":  {"lu": ["300201", "300202"], "os": "300200"},
    "Gamma": {"lu": ["300301", "300302"], "os": "300300"},
    "Delta": {"lu": ["300401", "300402"], "os": "300400"},
}
_EXPECTED_CAND = {"300100", "300200", "300400"}               # Alpha/Beta/Delta 的超跌票


def _rows(order):
    """按给定板块顺序生成 industry_map 行（板块内 lu 票在前、超跌票在后）。"""
    out = []
    for b in order:
        cfg = _BOARDS[b]
        for c in cfg["lu"] + [cfg["os"]]:
            out.append((c, "名称%s" % c, b))
    return out


class TestTieBreakDeterminism(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="osc_tie_")
        self._fp = os.path.join(self._tmp, "industry_map.csv")
        self._old_fp = prescreen._INDUSTRY_MAP_FP
        self._old_wl = prescreen.sym2board_map
        prescreen._INDUSTRY_MAP_FP = self._fp
        prescreen.sym2board_map = lambda: {}                   # 全靠申万行业回退
        self.addCleanup(self._restore)
        frames = {}
        for cfg in _BOARDS.values():
            for c in cfg["lu"]:
                frames[c] = _frame(_uptrend_lu())
            frames[cfg["os"]] = _frame(_oversold())
        self._provider = _FakeProvider(frames)

    def _restore(self):
        prescreen._INDUSTRY_MAP_FP = self._old_fp
        prescreen.sym2board_map = self._old_wl

    def _run(self, order):
        with open(self._fp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "行业名称"])
            w.writerows(_rows(order))
        return prescreen.premarket_candidates(3, "2026-10-14", provider=self._provider)

    def test_tie_broken_by_board_name_not_insertion_order(self):
        """三种行序（正/逆/交错）⇒ 候选集逐码一致，且 = 板块名升序破出的 Alpha/Beta/Delta。"""
        base = ["Alpha", "Beta", "Gamma", "Delta"]
        orders = [base, list(reversed(base)), ["Gamma", "Alpha", "Delta", "Beta"]]
        results = [set(self._run(o)) for o in orders]
        for i, r in enumerate(results):
            self.assertEqual(r, _EXPECTED_CAND,
                             f"行序 {orders[i]} 下候选集必须仍是板块名升序破序的结果")
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_same_input_reproducible(self):
        """同一输入跑两遍 ⇒ 结果完全一致。"""
        first = self._run(["Gamma", "Delta", "Beta", "Alpha"])
        second = self._run(["Gamma", "Delta", "Beta", "Alpha"])
        self.assertEqual(first, second)

    def test_count_dominates_board_name(self):
        """家数是主键：名字最靠后的板块家数第一 ⇒ 仍排第 1（排序键主键未变）。"""
        # Zulu 家数 3 但名字最后；Able 家数 2 名字最前 ⇒ top_n=1 必须选 Zulu。
        rows = [("300901", "祖鲁一", "Zulu"), ("300902", "祖鲁二", "Zulu"),
                ("300903", "祖鲁三", "Zulu"), ("300900", "祖鲁超跌", "Zulu"),
                ("300801", "艾波一", "Able"), ("300802", "艾波二", "Able"),
                ("300800", "艾波超跌", "Able")]
        with open(self._fp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["代码", "名称", "行业名称"])
            w.writerows(rows)
        frames = {"300901": _frame(_uptrend_lu()), "300902": _frame(_uptrend_lu()),
                  "300903": _frame(_uptrend_lu()), "300900": _frame(_oversold()),
                  "300801": _frame(_uptrend_lu()), "300802": _frame(_uptrend_lu()),
                  "300800": _frame(_oversold())}
        cand = prescreen.premarket_candidates(1, "2026-10-14",
                                              provider=_FakeProvider(frames))
        self.assertEqual(set(cand), {"300900"},
                         "家数第一（3 家）的 Zulu 必须赢过家数 2 但名字最前的 Able")


if __name__ == "__main__":
    unittest.main(verbosity=2)
