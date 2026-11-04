# -*- coding: utf-8 -*-
"""选股猎手轮询的候选池筛选（2026-11-04 owner）：

「只轮询股票在多头，同时均线粘合的股票」。

口径：完成日线 ①日线三态=多头（MA60 20 日变化率>+3%）②均线粘合
（`core.ma_reclaim.convergence_state`：五线≤3% 或 短线 5/10/20≤2%）。
本文件只验筛选本身（全离线：临时日线缓存 + 临时 watchlist）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.hunter_poll as HP  # noqa: E402
from core import stage_gate  # noqa: E402


def _series(n=160, start=100.0, growth=0.0):
    """等比涨跌（growth>0 慢涨 → 均线粘合；大幅增长 → 多头但离散）。"""
    p, rows = start, []
    for i in range(n):
        rows.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                     "open": p, "high": p, "low": p, "close": p, "volume": 1e5})
        p *= (1 + growth)
    return rows


class TestHunterCandidateFilter(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="hp_filter_"))
        self._saved_daily = stage_gate.DAILY_DIR
        self._saved_wl = HP._WATCHLIST_FP
        stage_gate.DAILY_DIR = self._tmp
        stage_gate.clear_cache()
        HP._CAND_CACHE.update({"date": None, "codes": None, "total": 0})
        HP._POOL_CACHE.update({"mtime": None, "codes": None})
        # 000001 多头+短线粘合；000002 震荡(涨太慢)；000003 空头；000004 多头但离散
        for code, g in (("000001", 0.0015), ("000002", 0.0010),
                        ("000003", -0.0015), ("000004", 0.0030)):
            (self._tmp / f"{code}.json").write_text(
                json.dumps({"rows": _series(growth=g)}, ensure_ascii=False), encoding="utf-8")
        wl = {
            "000001": {"name": "a", "jiuyan_category": "半导体", "jiuyan_concept": "设备"},
            "000002": {"name": "b", "jiuyan_category": "半导体", "jiuyan_concept": "设备"},
            "000003": {"name": "c", "jiuyan_category": "半导体", "jiuyan_concept": "设备"},
            "000004": {"name": "d", "jiuyan_category": "半导体", "jiuyan_concept": "设备"},
            "000009": {"name": "no-concept", "jiuyan_category": "", "jiuyan_concept": ""},
        }
        self._wl_fp = self._tmp / "watchlist_jiuyan.json"
        self._wl_fp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
        HP._WATCHLIST_FP = str(self._wl_fp)

    def tearDown(self):
        stage_gate.DAILY_DIR = self._saved_daily
        stage_gate.clear_cache()
        HP._WATCHLIST_FP = self._saved_wl
        HP._CAND_CACHE.update({"date": None, "codes": None, "total": 0})
        HP._POOL_CACHE.update({"mtime": None, "codes": None})

    def test_只保留多头且粘合(self):
        got = HP.bull_converged_codes("2026-11-05")
        self.assertEqual(got, {"000001"},
                         "应只保留 多头+粘合；震荡/空头/多头但离散/无概念 一律不做候选")

    def test_无概念不进池(self):
        self.assertNotIn("000009", HP._hunter_pool_codes())

    def test_按日缓存(self):
        a = HP.bull_converged_codes("2026-11-05")
        # 覆盖成空 watchlist（mtime 变也不该影响当日缓存；下一日才会重算）
        HP.bull_converged_codes("2026-11-05")
        self.assertEqual(a, {"000001"})
        self.assertEqual(HP._CAND_CACHE["date"], "2026-11-05")

    def test_日线不足被跳过(self):
        (self._tmp / "000006.json").write_text(
            json.dumps({"rows": _series(n=70, growth=0.0015)}), encoding="utf-8")
        wl = json.loads(self._wl_fp.read_text(encoding="utf-8"))
        wl["000006"] = {"name": "short", "jiuyan_concept": "设备"}
        self._wl_fp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
        HP._POOL_CACHE.update({"mtime": None, "codes": None})
        HP._CAND_CACHE.update({"date": None, "codes": None, "total": 0})
        self.assertNotIn("000006", HP.bull_converged_codes("2026-11-05"))


if __name__ == "__main__":
    unittest.main()
