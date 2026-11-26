# -*- coding: utf-8 -*-
"""pack_v2（B40 + G8 + Tcombo）行为钉子测试（2026-11-26 owner 直投生产）。

钉住三件事：
  ① G8：开盘跳空 > 8% 的信号在**排序阶段**被标 NO_TRADE('gap_capped')，
     picks/spares 自然顺移给下一名（不是"挡掉就完"）；
  ② Tcombo：账本 temp 标签驱动出场分层——弱势档 pop+2%/3日/-3%普适止损，
     强势档 pop+5%，mid/无标签维持现行；
  ③ 预算默认：DEFAULTS 日预算 40 万（B40），hooks 默认回滚开关 osc_pack_v2。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTO = ROOT / "execution" / "auto"
for path in (str(ROOT), str(AUTO)):
    if path not in sys.path:
        sys.path.insert(0, path)

from oversold_gap_confirm import decision, executor  # noqa: E402


def _bars(close, low=None, open_=None, vwap_below=True):
    low = close - 0.1 if low is None else low
    return {"low": low, "close": close,
            "amount": close * 0.99 * 1000 if vwap_below else close * 1.01 * 1000,
            "volume": 1000, **({"open": open_} if open_ else {})}


class TestGapCap(unittest.TestCase):
    """① G8：开盘跳空 > 8% ⇒ gap_capped 剔除并顺移。"""

    def _evaluate(self, gap_cap):
        prev_close = {"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0}
        ma20 = {c: 12.0 for c in prev_close}
        bars = {
            # 开盘 +19%（超 cap），10:00 仍强：旧规则必排第一
            "A": _bars(11.95, low=11.9, open_=11.9),
            "B": _bars(10.5, low=10.4, open_=10.4),
            "C": _bars(10.3, low=10.2, open_=10.2),
            "D": _bars(10.2, low=10.1, open_=10.1),
        }
        return decision.evaluate(prev_close, ma20, bars, top_n=2, gap_cap=gap_cap)

    def test_cap_off_keeps_big_gap(self):
        r = self._evaluate(None)
        self.assertEqual([p["code"] for p in r["picks"]], ["A", "B"])
        self.assertEqual(r["rejected_gap"], 0)

    def test_cap_on_excludes_and_shifts(self):
        r = self._evaluate(0.08)
        # A 被剔除（标记留痕），picks 顺移为 B/C
        self.assertEqual([p["code"] for p in r["picks"]], ["B", "C"])
        self.assertEqual(r["rejected_gap"], 1)
        capped = [s for s in r["signals"] if s.get("reason") == "gap_capped"]
        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0]["code"], "A")
        self.assertEqual(capped[0]["decision"], decision.NO_TRADE)
        # 顺移语义：A 仍在 signals 里（留痕可审计），但 build_spares 只取 TRADE
        from oversold_gap_confirm import backfill
        spares = backfill.build_spares(r["signals"], ["B", "C"], [],
                                       bars={"A": {"close": 11.95}, "D": {"close": 10.2}})
        self.assertEqual([s["code"] for s in spares], ["D"])

    def test_open_missing_falls_back_to_close_gap(self):
        prev_close = {"A": 10.0, "B": 10.0, "C": 10.0}
        ma20 = {c: 12.0 for c in prev_close}
        bars = {"A": _bars(11.5, low=11.4),          # 无 open：close-gap +15% > cap
                "B": _bars(10.5, low=10.4),
                "C": _bars(10.3, low=10.2)}
        r = decision.evaluate(prev_close, ma20, bars, top_n=2, gap_cap=0.08)
        self.assertEqual([p["code"] for p in r["picks"]], ["B", "C"])


class TestTcomboExitLayering(unittest.TestCase):
    """② Tcombo：temp 标签驱动出场分层。"""

    def test_layered_exit_params(self):
        self.assertEqual(executor.layered_exit_params(25.0), (0.02, 3, 0.03, True))
        self.assertEqual(executor.layered_exit_params(40.0), (0.02, 3, 0.03, True))
        self.assertEqual(executor.layered_exit_params(55.0), (0.03, 5, 0.05, False))
        self.assertEqual(executor.layered_exit_params(70.0), (0.05, 5, 0.05, False))
        self.assertEqual(executor.layered_exit_params(None), (0.03, 5, 0.05, False))

    def _book(self, temp):
        return {"X": {"entry_date": "2026-11-25", "entry_price": 10.0,
                      "dev20": -0.03, "days_held": 0, "temp": temp}}

    def test_morning_pop_target_by_temp(self):
        highs = {"X": 10.25}     # +2.5%：mid 档(+3%)不卖，弱势档(+2%)卖
        d = "2026-11-26"
        weak = executor.build_morning_sell_plan(self._book(25.0), highs, d, tcombo=True)
        mid = executor.build_morning_sell_plan(self._book(55.0), highs, d, tcombo=True)
        self.assertEqual(weak[0]["reason"], "pop_sell")
        self.assertEqual(weak[0]["pop_target"], 0.02)
        self.assertEqual(mid[0]["reason"], "hold")
        self.assertEqual(mid[0]["pop_target"], 0.03)
        # 强势档 +5%：+2.5% 不卖
        strong = executor.build_morning_sell_plan(self._book(70.0), highs, d, tcombo=True)
        self.assertEqual(strong[0]["reason"], "hold")
        self.assertEqual(strong[0]["pop_target"], 0.05)
        # tcombo 关闭：回到单目标（兼容旧账本/回滚）
        off = executor.build_morning_sell_plan(self._book(25.0), highs, d, tcombo=False)
        self.assertEqual(off[0]["reason"], "hold")

    def test_afternoon_weak_fast_stop_and_expiry(self):
        # 弱势档：-3% 普适止损（dev20=-0.03 不是深票，旧规则不挂止损）
        book = {"X": {"entry_date": "2026-11-24", "entry_price": 10.0,
                      "dev20": -0.03, "days_held": 1, "temp": 25.0}}
        prices = {"X": 9.6}        # -4%：弱势档 SL-3% 触发；旧规则（非深票）不触发
        on = executor.build_sell_plan(book, prices, "2026-11-26", tcombo=True)
        off = executor.build_sell_plan(book, prices, "2026-11-26", tcombo=False)
        self.assertEqual(on[0]["reason"], "sl5_deep")
        self.assertEqual(on[0]["exit_leg"], "weak_fast")
        self.assertEqual(off[0]["reason"], "hold")
        # 弱势档 3 日到期（days_held=3 ⇒ 到期；旧规则 5 日不到期）
        book3 = {"X": {"entry_date": "2026-11-21", "entry_price": 10.0,
                       "dev20": -0.03, "days_held": 3, "temp": 25.0}}
        on3 = executor.build_sell_plan(book3, {"X": 10.4}, "2026-11-26", tcombo=True)
        self.assertEqual(on3[0]["reason"], "horizon_expired")
        # 无 temp 标签（旧账本）：tcombo 开着也不分层 ⇒ 行为与旧规则一致
        legacy = {"X": {"entry_date": "2026-11-24", "entry_price": 10.0,
                        "dev20": -0.03, "days_held": 1}}
        on_legacy = executor.build_sell_plan(legacy, {"X": 9.6}, "2026-11-26", tcombo=True)
        self.assertEqual(on_legacy[0]["reason"], "hold")


class TestB40Default(unittest.TestCase):
    """③ 预算默认值钉子（B40）。"""

    def test_defaults(self):
        self.assertEqual(executor.DEFAULTS["daily_budget"], 400000.0)
        self.assertEqual(executor.DEFAULTS["gap_cap"], 0.08)
        self.assertEqual(executor.DEFAULTS["tcombo"], True)


if __name__ == "__main__":
    unittest.main()
