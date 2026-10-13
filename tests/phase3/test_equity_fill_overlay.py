# -*- coding: utf-8 -*-
"""equity_daily._apply_fill_overlay 单测（2026-10-13 P0：台账丢票/跨日 events 回退）。

覆盖：
1) fill 证实持仓但台账缺失 → 补入（10-12 实证：605198×1800/688003×1700 全天 positions={}）；
2) 当日无 events 文件 → 回退 ≤date 最近一个 events 的末笔 fill 状态；
3) 台账已有票 qty 不符 → 以 pos_after 覆盖；pos_after=0 → 剔除；
4) 台账与 fill 一致 → 不动、无 overlay 记录。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import scripts.equity_daily as ed  # noqa: E402


def _w(fp, recs):
    with open(fp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestFillOverlay(unittest.TestCase):
    def setUp(self):
        self._old = ed._BRIDGE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        ed._BRIDGE_DIR = self.tmp.name

    def tearDown(self):
        ed._BRIDGE_DIR = self._old
        self.tmp.cleanup()

    def _ev(self, yyyymmdd, fills):
        _w(os.path.join(self.tmp.name, f"events_{yyyymmdd}.jsonl"),
           [{"event": "fill", "code": c, "pos_after": q} for c, q in fills])

    def test_missing_position_added(self):
        self._ev("20261012", [("600584", 0), ("605198", 1800), ("688003", 1700)])
        pos = {}
        ov, name = ed._apply_fill_overlay("2026-10-12", pos)
        self.assertEqual(pos.get("SHSE.605198", {}).get("qty"), 1800)
        self.assertEqual(pos.get("SHSE.688003", {}).get("qty"), 1700)
        self.assertEqual(len(ov), 2)
        self.assertEqual(name, "events_20261012.jsonl")

    def test_fallback_to_latest_prior_events(self):
        self._ev("20261012", [("605198", 1800), ("688003", 1700)])
        pos = {}
        ov, name = ed._apply_fill_overlay("2026-10-13", pos)   # 当日无 events
        self.assertEqual(name, "events_20261012.jsonl")
        self.assertEqual(pos.get("SHSE.605198", {}).get("qty"), 1800)
        self.assertEqual(pos.get("SHSE.688003", {}).get("qty"), 1700)

    def test_never_use_future_events(self):
        self._ev("20261014", [("605198", 1800)])
        ov, name = ed._apply_fill_overlay("2026-10-13", {})
        self.assertEqual((ov, name), ([], None))

    def test_fill_less_current_file_does_not_block_fallback(self):
        """10-13 实证：当日 events 只有 risk 记录零 fill，必须回退到 10-12 的末笔 fill。"""
        self._ev("20261012", [("605198", 1800), ("688003", 1700)])
        _w(os.path.join(self.tmp.name, "events_20261013.jsonl"),
           [{"event": "risk", "kind": "strategy_exit"}])
        pos = {}
        ov, name = ed._apply_fill_overlay("2026-10-13", pos)
        self.assertEqual(name, "events_20261012.jsonl")
        self.assertEqual(pos.get("SHSE.605198", {}).get("qty"), 1800)
        self.assertEqual(pos.get("SHSE.688003", {}).get("qty"), 1700)

    def test_later_fill_wins_per_code(self):
        """同一票跨文件：以最近一次 fill 的 pos_after 为准。"""
        self._ev("20261012", [("688003", 1700)])
        self._ev("20261013", [("688003", 900)])
        pos = {}
        ov, name = ed._apply_fill_overlay("2026-10-13", pos)
        self.assertEqual(name, "events_20261013.jsonl")
        self.assertEqual(pos.get("SHSE.688003", {}).get("qty"), 900)

    def test_qty_overwrite_and_zero_pop(self):
        self._ev("20261013", [("600584", 0), ("688003", 900)])
        pos = {"SHSE.600584": {"qty": 1200, "cost": 55.0},
               "SHSE.688003": {"qty": 1700, "cost": 65.0}}
        ov, _ = ed._apply_fill_overlay("2026-10-13", pos)
        self.assertNotIn("SHSE.600584", pos)
        self.assertEqual(pos["SHSE.688003"]["qty"], 900)
        self.assertEqual(pos["SHSE.688003"]["cost"], 65.0)   # 保留原 cost
        self.assertEqual(len(ov), 2)

    def test_consistent_noop(self):
        self._ev("20261013", [("688003", 1700)])
        pos = {"SHSE.688003": {"qty": 1700, "cost": 65.0}}
        ov, _ = ed._apply_fill_overlay("2026-10-13", pos)
        self.assertEqual(ov, [])
        self.assertEqual(pos["SHSE.688003"]["qty"], 1700)

    def test_sz_prefix_for_shenzhen_code(self):
        self._ev("20261013", [("300054", 500)])
        pos = {}
        ed._apply_fill_overlay("2026-10-13", pos)
        self.assertEqual(pos.get("SZSE.300054", {}).get("qty"), 500)


if __name__ == "__main__":
    unittest.main()
