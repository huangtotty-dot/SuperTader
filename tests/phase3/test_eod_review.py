# -*- coding: utf-8 -*-
"""收盘后 EOD 任务簇单测 —— core/eod_review.py（2026-11-14 补）。

四个调度器全是「交易时段后一次性触发」：本测试只测**确定性契约**，不真起子进程/线程——
① 时间闸（周末 / 未到阈值时刻 / 当日已占位 → 直接 return，不 Popen 不起线程）；
② `_maybe_record_daily_pnl` 的盈亏数学（day_pnl/day_pct/mkt_val/cost_val/汇总）+ JSONL 落盘结构
   + 收盘 pre_close 回写（HOLDINGS 原地更新 + save_pre_close 审计调用）。
历史易错点都钉住：14:59 去右界补扫、当日去重由 `_daily_pnl_push_date` 保证、pre_close 一次写全。

运行：python tests/phase3/test_eod_review.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.eod_review as er  # noqa: E402

FRIDAY = datetime(2026, 11, 13, 15, 30, 0)    # 周五，各阈值都已过
SATURDAY = datetime(2026, 11, 14, 15, 30, 0)  # 周六，周末闸


class _EodBase(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "HOLDINGS": er.HOLDINGS,
            "DDS": er.DAILY_DECISION_STATS,
            "review": dict(er._daily_review_state),
            "prefetch": dict(er._chart_prefetch_state),
            "patrol": dict(er._freshness_patrol_state),
            "pnl_date": er._daily_pnl_push_date,
            "root": er._ROOT,
        }
        self.tmp = tempfile.mkdtemp()
        er._ROOT = self.tmp

    def tearDown(self):
        er.HOLDINGS = self._saved["HOLDINGS"]
        er.DAILY_DECISION_STATS = self._saved["DDS"]
        er._daily_review_state.clear(); er._daily_review_state.update(self._saved["review"])
        er._chart_prefetch_state.clear(); er._chart_prefetch_state.update(self._saved["prefetch"])
        er._freshness_patrol_state.clear(); er._freshness_patrol_state.update(self._saved["patrol"])
        er._daily_pnl_push_date = self._saved["pnl_date"]
        er._ROOT = self._saved["root"]

    @property
    def _pnl_path(self):
        return os.path.join(self.tmp, "t_io", "logs", "daily_pnl.jsonl")


class TestDailyReviewGate(unittest.TestCase):
    def _reset(self):
        er._daily_review_state.clear(); er._daily_review_state.update({"date": "", "next_try": 0.0})

    def tearDown(self):
        self._reset()

    def test_weekend_gate(self):
        self._reset()
        with mock.patch("subprocess.Popen") as popen:
            er._maybe_run_daily_review(SATURDAY)
        popen.assert_not_called()
        self.assertEqual(er._daily_review_state["date"], "")

    def test_before_1510_gate(self):
        self._reset()
        with mock.patch("subprocess.Popen") as popen:
            er._maybe_run_daily_review(datetime(2026, 11, 13, 15, 9, 0))
        popen.assert_not_called()

    def test_same_day_dedup(self):
        er._daily_review_state.clear()
        er._daily_review_state.update({"date": "2026-11-13", "next_try": 0.0})
        with mock.patch("subprocess.Popen") as popen:
            er._maybe_run_daily_review(FRIDAY)
        popen.assert_not_called()

    def test_throttle_next_try(self):
        """next_try 未到（失败重试节流窗内）→ 不触发。"""
        er._daily_review_state.clear()
        er._daily_review_state.update({"date": "", "next_try": 9e18})  # 未来
        with mock.patch("subprocess.Popen") as popen:
            er._maybe_run_daily_review(FRIDAY)
        popen.assert_not_called()


class TestChartPrefetchGate(unittest.TestCase):
    def tearDown(self):
        er._chart_prefetch_state.clear(); er._chart_prefetch_state.update({"date": "", "next_try": 0.0})

    def test_weekend_gate(self):
        er._chart_prefetch_state.clear()
        er._chart_prefetch_state.update({"date": "", "next_try": 0.0})
        with mock.patch("subprocess.Popen") as popen:
            er._maybe_run_chart_prefetch(SATURDAY)
        popen.assert_not_called()

    def test_same_day_dedup(self):
        er._chart_prefetch_state.clear()
        er._chart_prefetch_state.update({"date": "2026-11-13", "next_try": 0.0})
        with mock.patch("subprocess.Popen") as popen:
            er._maybe_run_chart_prefetch(FRIDAY)
        popen.assert_not_called()


class TestFreshnessPatrolGate(unittest.TestCase):
    def tearDown(self):
        er._freshness_patrol_state.clear(); er._freshness_patrol_state.update({"date": ""})

    def test_before_1540_gate(self):
        er._freshness_patrol_state.clear(); er._freshness_patrol_state.update({"date": ""})
        with mock.patch.object(er._threading, "Thread") as thr:
            er._maybe_run_freshness_patrol(datetime(2026, 11, 13, 15, 39, 0))
        thr.assert_not_called()

    def test_same_day_dedup(self):
        er._freshness_patrol_state.clear(); er._freshness_patrol_state.update({"date": "2026-11-13"})
        with mock.patch.object(er._threading, "Thread") as thr:
            er._maybe_run_freshness_patrol(FRIDAY)
        thr.assert_not_called()


class TestRecordDailyPnl(_EodBase):
    def test_weekend_noop(self):
        er._daily_pnl_push_date = ""
        er.HOLDINGS = {"600481": {"qty": 100, "cost": 10.0, "pre_close": 9.5}}
        with mock.patch("core.holdings_repo.save_pre_close") as spc:
            er._maybe_record_daily_pnl(SATURDAY)
        spc.assert_not_called()
        self.assertFalse(os.path.exists(self._pnl_path))

    def test_before_1459_noop(self):
        er._daily_pnl_push_date = ""
        er.HOLDINGS = {"600481": {"qty": 100, "cost": 10.0, "pre_close": 9.5}}
        with mock.patch("core.holdings_repo.save_pre_close") as spc:
            er._maybe_record_daily_pnl(datetime(2026, 11, 13, 14, 58, 0))
        spc.assert_not_called()

    def test_same_day_dedup_noop(self):
        er._daily_pnl_push_date = "2026-11-13"
        er.HOLDINGS = {"600481": {"qty": 100, "cost": 10.0, "pre_close": 9.5}}
        with mock.patch("core.holdings_repo.save_pre_close") as spc:
            er._maybe_record_daily_pnl(FRIDAY)
        spc.assert_not_called()
        self.assertFalse(os.path.exists(self._pnl_path))

    def test_empty_holdings_still_marks_date(self):
        er._daily_pnl_push_date = ""
        er.HOLDINGS = {}
        with mock.patch("core.holdings_repo.save_pre_close") as spc:
            er._maybe_record_daily_pnl(FRIDAY)
        self.assertEqual(er._daily_pnl_push_date, "2026-11-13")  # 去重标记仍落
        spc.assert_not_called()
        self.assertFalse(os.path.exists(self._pnl_path))

    def test_pnl_math_and_jsonl(self):
        er._daily_pnl_push_date = ""
        er.HOLDINGS = {
            "600481": {"name": "A", "qty": 1000, "cost": 10.0, "pre_close": 9.5},
            "002639": {"name": "B", "qty": 2000, "cost": 20.0, "pre_close": 19.0},
        }
        er.DAILY_DECISION_STATS = {
            "600481": {"last_price": 10.0},
            "002639": {"last_price": 20.5},
        }
        with mock.patch("core.holdings_repo.save_pre_close", return_value=2) as spc:
            er._maybe_record_daily_pnl(FRIDAY)

        spc.assert_called_once()
        with open(self._pnl_path, encoding="utf-8") as f:
            rec = json.loads(f.read().strip())
        self.assertEqual(rec["date"], "2026-11-13")
        self.assertEqual(rec["total_value"], 51000.0)
        self.assertEqual(rec["total_cost"], 50000.0)
        self.assertEqual(rec["total_pnl"], 1000.0)
        self.assertEqual(rec["total_pnl_pct"], 2.0)
        self.assertEqual(rec["day_pnl_float"], 3500.0)
        self.assertEqual(rec["t0_realized"], 0.0)

        by_code = {s["code"]: s for s in rec["stocks"]}
        self.assertEqual(len(by_code), 2)
        self.assertEqual(by_code["600481"]["day_pnl"], 500.0)
        self.assertAlmostEqual(by_code["600481"]["day_pct"], 5.26, places=2)
        self.assertEqual(by_code["600481"]["mkt_val"], 10000.0)
        self.assertEqual(by_code["002639"]["day_pnl"], 3000.0)
        self.assertEqual(by_code["002639"]["mkt_val"], 41000.0)

    def test_pre_close_updated_and_audited(self):
        er._daily_pnl_push_date = ""
        er.HOLDINGS = {"600481": {"name": "A", "qty": 1000, "cost": 10.0, "pre_close": 9.5}}
        er.DAILY_DECISION_STATS = {"600481": {"last_price": 10.0}}
        with mock.patch("core.holdings_repo.save_pre_close", return_value=1) as spc:
            er._maybe_record_daily_pnl(FRIDAY)

        self.assertEqual(er.HOLDINGS["600481"]["pre_close"], 10.0)  # 内存真源同步
        spc.assert_called_once_with({"600481": 10.0}, actor="main", reason="eod_pre_close")

    def test_zero_pre_close_day_pnl_zero(self):
        """pre_close<=0 → day_pnl/day_pct 归零（除零保护）。"""
        er._daily_pnl_push_date = ""
        er.HOLDINGS = {"600481": {"name": "A", "qty": 100, "cost": 10.0, "pre_close": 0.0}}
        er.DAILY_DECISION_STATS = {"600481": {"last_price": 5.0}}
        with mock.patch("core.holdings_repo.save_pre_close", return_value=1):
            er._maybe_record_daily_pnl(FRIDAY)
        with open(self._pnl_path, encoding="utf-8") as f:
            rec = json.loads(f.read().strip())
        s = rec["stocks"][0]
        self.assertEqual(s["day_pnl"], 0.0)
        self.assertEqual(s["day_pct"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
