# -*- coding: utf-8 -*-
"""候选㊱ 回归测试：先卖后买的现金衔接（cash_floor）。

事故原型（2026-11-19）：早盘卖 10:00:07 成交回款 18.4 万，买侧同秒 get_cash()
仍读到卖前 5,456 元 ⇒ 688630「不足一手」漏买、002119 欠配。
修复：hooks 传入 cash_floor = 卖前基线 + 已成交净回款；executor 取
max(实时读数, 下限)。本测试钉住四种场景：
  1. 无 floor（None）⇒ 与旧版逐位一致（高价票仍 below_min_lot）
  2. 现金滞后 + floor ⇒ 按下限配额，高价票买得到
  3. 现金已含回款（读数 ≥ floor）⇒ floor 不生效，无双计
  4. floor 低于实时读数 ⇒ 不生效
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import executor as ex                     # noqa: E402
from tests.phase3.test_osc_backfill_shift import FakeGateway        # noqa: E402


def _bar(close, low, vwap):
    return {"open": close, "high": close, "low": low, "close": close,
            "amount": vwap * 1000, "volume": 1000}


# 三只全过票：A 高价（214，一手 21,400）、B/C 低价（11.5）
# prev_close=200 / ma20=220（超跌）/ low 205≥200 / close 214>200 / vwap 210<214
CODES = ["A00001", "B00002", "C00003"]
PREV = {c: 200.0 for c in CODES}
MA20 = {c: 220.0 for c in CODES}
BARS = {"A00001": _bar(close=214.0, low=205.0, vwap=210.0),
        "B00002": _bar(close=11.5, low=10.1, vwap=11.4),
        "C00003": _bar(close=11.5, low=10.1, vwap=11.4)}


def _run(gw, cash_floor, td):
    return ex.run_buy_exec(gw, "2026-11-20", PREV, MA20, BARS,
                           board={c: "半导体" for c in CODES}, book_dir=td,
                           dry_run=True, sleep_fn=lambda *_: None,
                           log=lambda *_: None,
                           names={c: "测试股" for c in CODES},
                           prescreen_meta={"universe": "full_market", "board_top_n": 5},
                           cash_floor=cash_floor)


class TestOscCashFloor(unittest.TestCase):
    """候选㊱：买侧现金下限。"""

    def test_no_floor_keeps_legacy_behavior(self):
        """无 floor ⇒ 旧行为：现金 5,456 时高价票 below_min_lot 跳过。"""
        gw = FakeGateway(cash=5456.53, place_status="submitted")
        with tempfile.TemporaryDirectory() as td:
            fills = _run(gw, None, td)
            buys = {b["code"]: b for b in fills["buys"]}
            a = buys["A00001"]
            self.assertEqual(a["qty_placed"], 0)
            self.assertIn("below_min_lot", str(a.get("defer_reason")))
            self.assertEqual(fills["summary"]["cash_before"], 5456.53)
            self.assertNotIn("cash_floor", fills["summary"])

    def test_stale_cash_floor_rescues_high_price_picker(self):
        """现金滞后 + floor=188,878 ⇒ A 票按 10 万单票额度买到 400 股。"""
        gw = FakeGateway(cash=5456.53, place_status="submitted")
        with tempfile.TemporaryDirectory() as td:
            fills = _run(gw, 188878.0, td)
            buys = {b["code"]: b for b in fills["buys"]}
            a = buys["A00001"]
            self.assertGreaterEqual(a["qty_placed"], 100, "floor 生效后 A 票必须买得到")
            self.assertIsNone(a.get("defer_reason"))
            s = fills["summary"]
            self.assertTrue(s["cash_floor_applied"])
            self.assertEqual(s["cash_before_raw"], 5456.53)
            self.assertEqual(s["cash_floor"], 188878.0)
            self.assertEqual(s["cash_before"], 188878.0)

    def test_fresh_cash_floor_noop(self):
        """实时读数已含回款（≥floor）⇒ floor 标记不生效，额度不变。"""
        gw = FakeGateway(cash=188878.0, place_status="submitted")
        with tempfile.TemporaryDirectory() as td:
            fills = _run(gw, 188878.0, td)
            buys = {b["code"]: b for b in fills["buys"]}
            self.assertGreaterEqual(buys["A00001"]["qty_placed"], 100)
            s = fills["summary"]
            self.assertFalse(s["cash_floor_applied"])
            self.assertEqual(s["cash_before"], 188878.0)

    def test_floor_below_cash_noop(self):
        """floor 低于实时读数 ⇒ 不生效（读数优先）。"""
        gw = FakeGateway(cash=5456.53, place_status="submitted")
        with tempfile.TemporaryDirectory() as td:
            fills = _run(gw, 1000.0, td)
            buys = {b["code"]: b for b in fills["buys"]}
            self.assertEqual(buys["A00001"]["qty_placed"], 0)
            self.assertFalse(fills["summary"]["cash_floor_applied"])
            self.assertEqual(fills["summary"]["cash_before"], 5456.53)


if __name__ == "__main__":
    unittest.main()
