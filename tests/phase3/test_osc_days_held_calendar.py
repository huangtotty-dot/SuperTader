# -*- coding: utf-8 -*-
"""超跌跳空确认 · 持有天数按交易日历推导（㉘ 修复）单测 —— 2026-11-13。**全离线**。

## 红线是什么

688652（佳驰科技）2026-11-05 入场，持有 5 个交易日到期日 = 2026-11-12
（11-06/09/10/11/12，跨一个周末）。但旧的 `_advance_days_held` 是
「策略每跑一天 +1」的**计数器**：跨周末/漏跑/状态回退都丢增量 ⇒ 11-12 当天
计数器只有 4，`build_sell_plan` 判定 days_held(4) < hold_days(5) ⇒ 到期漏卖，
迟至 11-13 才卖出 —— W46 周复盘硬红线之一。

## 修法钉什么

`_days_held_by_calendar(entry_date, date, trading_days)`：
`entry_date < d <= date` 的交易日数（含当日；正在运行的当日必为交易日，自动补上）。
`_advance_days_held`：日历可用时**以日历为准**，计数与日历不一致则纠正并留痕
`days_held_calendar_fixed`；日历未注入/取数失败 ⇒ 退化为旧 +1 计数器（幂等不变）。

运行：python -m unittest tests.phase3.test_osc_days_held_calendar -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "execution" / "auto")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import executor as ex                       # noqa: E402

# 688652 真实时间线：11-05(四) 入场；11-06(五)/09(一)/10/11/12(四) 五个交易日
ENTRY = "2026-11-05"
D12 = "2026-11-12"
TDS_TO_12 = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]


class TestDaysHeldByCalendar(unittest.TestCase):
    def test_688652_redline_scenario_is_5(self):
        """11-05 入场 → 11-12 恰为第 5 个持有交易日（跨周末不计）。"""
        got = ex._days_held_by_calendar(ENTRY, D12, TDS_TO_12)
        self.assertEqual(got, 5, "entry<d<=today 的交易日数：11-06/09/10/11/12 = 5")

    def test_weekend_not_counted(self):
        """周末两天（11-07/08）不在交易日列表里 ⇒ 不得计入。"""
        got = ex._days_held_by_calendar(ENTRY, "2026-11-09", TDS_TO_12)
        self.assertEqual(got, 2, "11-06 + 11-09 = 2；周末 11-07/08 不计")

    def test_today_auto_added_when_calendar_stale(self):
        """日历没覆盖到当日（拉取滞后）⇒ 函数自动把当日算作交易日。"""
        got = ex._days_held_by_calendar(ENTRY, D12, TDS_TO_12[:-1])
        self.assertEqual(got, 5, "日历缺 11-12 时自动补当日，结果仍为 5")

    def test_entry_today_is_zero(self):
        self.assertEqual(ex._days_held_by_calendar(D12, D12, TDS_TO_12), 0)

    def test_entry_in_future_is_zero(self):
        self.assertEqual(ex._days_held_by_calendar("2026-11-20", D12, TDS_TO_12), 0)

    def test_empty_calendar_returns_none(self):
        self.assertIsNone(ex._days_held_by_calendar(ENTRY, D12, []))
        self.assertIsNone(ex._days_held_by_calendar(ENTRY, D12, None))

    def test_empty_entry_returns_zero_when_calendar_present(self):
        self.assertEqual(ex._days_held_by_calendar("", D12, TDS_TO_12), 0)


class TestAdvanceDaysHeld(unittest.TestCase):
    def _pos(self, days_held=4, last_check="2026-11-11"):
        return {"688652": {"entry_date": ENTRY, "entry_price": 60.0, "qty": 2500,
                           "days_held": days_held, "last_check_date": last_check}}

    def test_lagging_counter_corrected_with_trace(self):
        """红线复现：计数器 4 滞后 → 日历纠正为 5 并留痕 days_held_calendar_fixed。"""
        pos = self._pos(days_held=4)
        logs = []
        ex._advance_days_held(pos, D12, trading_days=TDS_TO_12, log=logs.append)
        rec = pos["688652"]
        self.assertEqual(rec["days_held"], 5)
        self.assertEqual(rec["days_held_calendar_fixed"],
                         {"from": 4, "to": 5, "date": D12})
        self.assertEqual(rec["last_check_date"], D12)
        self.assertTrue(any("㉘天数日历校准" in s and "688652" in s for s in logs),
                        "纠正必须打日志留痕")

    def test_inflated_counter_also_corrected(self):
        """计数器虚高（重复推进/状态错乱）⇒ 同样以日历为准纠正回来。"""
        pos = self._pos(days_held=6)
        ex._advance_days_held(pos, D12, trading_days=TDS_TO_12, log=lambda s: None)
        rec = pos["688652"]
        self.assertEqual(rec["days_held"], 5, "日历为准：虚高 6 也要纠回 5")
        self.assertEqual(rec["days_held_calendar_fixed"]["from"], 6)

    def test_matching_counter_no_trace(self):
        pos = self._pos(days_held=5)
        ex._advance_days_held(pos, D12, trading_days=TDS_TO_12, log=lambda s: None)
        self.assertNotIn("days_held_calendar_fixed", pos["688652"],
                         "计数与日历一致时不写纠正留痕")
        self.assertEqual(pos["688652"]["last_check_date"], D12)

    def test_no_calendar_degrades_to_legacy_counter(self):
        """日历不可用 ⇒ 旧 +1 行为，且同日幂等（last_check_date 防重）。"""
        pos = self._pos(days_held=4)
        ex._advance_days_held(pos, D12, trading_days=None, log=lambda s: None)
        self.assertEqual(pos["688652"]["days_held"], 5)
        ex._advance_days_held(pos, D12, trading_days=None, log=lambda s: None)
        self.assertEqual(pos["688652"]["days_held"], 5, "同日重复调用不得重复 +1")
        self.assertNotIn("days_held_calendar_fixed", pos["688652"])

    def test_entry_today_untouched(self):
        pos = {"300534": {"entry_date": D12, "days_held": 0}}
        ex._advance_days_held(pos, D12, trading_days=TDS_TO_12, log=lambda s: None)
        self.assertEqual(pos["300534"]["days_held"], 0)
        self.assertNotIn("last_check_date", pos["300534"], "entry 当日整票跳过")

    def test_empty_book_no_crash(self):
        ex._advance_days_held({}, D12, trading_days=TDS_TO_12, log=lambda s: None)
        ex._advance_days_held({"X": {}}, D12, trading_days=TDS_TO_12, log=lambda s: None)


class TestCalendarToSellPlanEndToEnd(unittest.TestCase):
    """红线全链路：日历校准后 build_sell_plan 必须在到期日触发 horizon_expired。"""

    def test_688652_expires_on_11_12_not_11_13(self):
        open_pos = {"688652": {"entry_date": ENTRY, "entry_price": 60.0, "qty": 2500,
                               "dev20": None, "days_held": 4, "last_check_date": "2026-11-11"}}
        ex._advance_days_held(open_pos, D12, trading_days=TDS_TO_12, log=lambda s: None)
        plan = ex.build_sell_plan(open_pos, {"688652": 61.0}, D12, hold_days=5)
        self.assertEqual(plan[0]["reason"], "horizon_expired",
                         "11-12 满 5 个交易日 ⇒ 当天到期平仓（修复前计数器 4 ⇒ hold 漏卖）")
        self.assertTrue(plan[0]["expired"])
        self.assertEqual(plan[0]["days_held"], 5)

    def test_not_expired_before_5th_day(self):
        open_pos = {"688652": {"entry_date": ENTRY, "entry_price": 60.0, "qty": 2500,
                               "dev20": None, "days_held": 0}}
        ex._advance_days_held(open_pos, "2026-11-11", trading_days=TDS_TO_12, log=lambda s: None)
        plan = ex.build_sell_plan(open_pos, {"688652": 61.0}, "2026-11-11", hold_days=5)
        self.assertEqual(plan[0]["days_held"], 4)
        self.assertEqual(plan[0]["reason"], "hold", "第 4 天不得提前到期")


if __name__ == "__main__":
    unittest.main(verbosity=2)
