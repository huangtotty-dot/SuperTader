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


class TestRejectUnfreeze(unittest.TestCase):
    """_reject_unfreeze_cash 单测（2026-10-14 实弹事故：收盘后 status=12 拒单解冻回补）。

    覆盖：
    1) 拒单晚于 heartbeat 末行快照 → BUY 拒单按 qty×委托价回补；
    2) 拒单早于快照（S1 时代 status=8 盘中即时拒单）→ 不回补（幂等、不重补旧事件）；
    3) SELL 拒单 → 不回补（解冻的是持仓不是现金）；
    4) 无同 code BUY order 价格 → 跳过不硬算；
    5) heartbeat 缺 _ts / 当日无 events 文件 → 不回补。
    """

    def setUp(self):
        self._old = ed._BRIDGE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        ed._BRIDGE_DIR = self.tmp.name

    def tearDown(self):
        ed._BRIDGE_DIR = self._old
        self.tmp.cleanup()

    def _ev(self, yyyymmdd, recs):
        _w(os.path.join(self.tmp.name, f"events_{yyyymmdd}.jsonl"), recs)

    _HB = {"_ts": 1000.0, "cash": 1.0}

    def test_late_buy_reject_restored(self):
        self._ev("20261014", [
            {"event": "order", "code": "600097", "side": "BUY", "qty": 9800, "price": 10.2, "_ts": 100.0},
            {"event": "reject", "code": "600097", "side": "BUY", "qty": 9800, "_ts": 2000.0},
        ])
        amount, parts = ed._reject_unfreeze_cash("2026-10-14", self._HB)
        self.assertAlmostEqual(amount, 9800 * 10.2)
        self.assertEqual(parts, ["600097×9800@10.2"])

    def test_early_reject_not_restored(self):
        """S1 旧事件：盘中即时拒单，解冻早于现金快照 → 不回补。"""
        self._ev("20260929", [
            {"event": "order", "code": "002733", "side": "BUY", "qty": 5700, "price": 18.6, "_ts": 100.0},
            {"event": "reject", "code": "002733", "side": "BUY", "qty": 5700, "_ts": 500.0},
        ])
        amount, parts = ed._reject_unfreeze_cash("2026-09-29", self._HB)
        self.assertEqual((amount, parts), (0.0, []))

    def test_sell_reject_not_restored(self):
        self._ev("20261009", [
            {"event": "reject", "code": "600584", "side": "SELL", "qty": 1200, "_ts": 2000.0},
        ])
        amount, _ = ed._reject_unfreeze_cash("2026-10-09", self._HB)
        self.assertEqual(amount, 0.0)

    def test_no_order_price_skipped(self):
        self._ev("20261014", [
            {"event": "reject", "code": "600097", "side": "BUY", "qty": 9800, "_ts": 2000.0},
        ])
        amount, parts = ed._reject_unfreeze_cash("2026-10-14", self._HB)
        self.assertEqual((amount, parts), (0.0, []))

    def test_raw_side_fallback_buy(self):
        """reject 缺 side 字段时从 raw repr 解析 GM side:1=BUY。"""
        self._ev("20261014", [
            {"event": "order", "code": "000823", "side": "BUY", "qty": 4000, "price": 24.55, "_ts": 100.0},
            {"event": "reject", "code": "000823", "qty": 4000,
             "raw": "{'status': 12, 'side': 1, 'volume': 4000}", "_ts": 2000.0},
        ])
        amount, _ = ed._reject_unfreeze_cash("2026-10-14", self._HB)
        self.assertAlmostEqual(amount, 4000 * 24.55)

    def test_missing_hb_ts_or_events_noop(self):
        self._ev("20261014", [
            {"event": "order", "code": "600097", "side": "BUY", "qty": 9800, "price": 10.2, "_ts": 100.0},
            {"event": "reject", "code": "600097", "side": "BUY", "qty": 9800, "_ts": 2000.0},
        ])
        self.assertEqual(ed._reject_unfreeze_cash("2026-10-14", {}), (0.0, []))
        self.assertEqual(ed._reject_unfreeze_cash("2026-10-15", self._HB), (0.0, []))


if __name__ == "__main__":
    unittest.main()
