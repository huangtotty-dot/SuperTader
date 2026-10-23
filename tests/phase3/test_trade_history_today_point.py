# -*- coding: utf-8 -*-
"""台账「今日点」补点（2026-10-23 owner：「累计收益趋势的图没更新到今天」）。

背景：`core.trade_history` 的 `cum`/`cum_all` 是**按成交日**逐日累计的 ⇒ 最后一点 =
最后一个**成交日**。今天没成交时，那张「含浮动」的曲线就停在昨天，可浮动明明每天都变。

`t_gui.Api._ledger_append_today` 在**收盘后**、且台账里还没有今天的成交时，补一个点：
已实现/累计买入不变，`cum_total = 已实现 + 浮动`（浮动取台账里的 `unrealized`）。
盘中不补（台账里的 `last_price` 不是今天的收盘价）。

运行：python -m pytest tests/phase3/test_trade_history_today_point.py -q
"""
import os
import sys
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402

APPEND = t_gui.Api._ledger_append_today


def _ledger():
    return {
        "data_through": "2026-10-22",
        "cum": [{"account": "A", "date": "2026-10-22", "cum_realized": -100.0,
                 "cum_buy": 1000.0, "pct": -10.0, "cum_total": -150.0}],
        "cum_all": [{"date": "2026-10-22", "cum_realized": -100.0, "cum_buy": 1000.0,
                     "pct": -10.0, "cum_total": -150.0, "pct_total": -15.0, "delta_total": 0.0}],
        "stocks": [{"account": "A", "unrealized": -20.0}],
        "total_realized": -100.0, "total_unrealized": -20.0,
    }


class TestLedgerTodayPoint(unittest.TestCase):
    def test_收盘后补今日点(self):
        d = _ledger()
        APPEND(d, datetime(2026, 10, 23, 16, 30))
        self.assertEqual(d["cum"][-1]["date"], "2026-10-23")
        self.assertAlmostEqual(d["cum"][-1]["cum_total"], -120.0)      # 已实现 −100 + 浮动 −20
        self.assertTrue(d["cum"][-1]["today_est"])
        self.assertAlmostEqual(d["cum_all"][-1]["cum_total"], -120.0)
        self.assertAlmostEqual(d["cum_all"][-1]["delta_total"], 30.0)  # 比 10-22 的 −150 好了 30
        self.assertAlmostEqual(d["cum_all"][-1]["pct_total"], -12.0)

    def test_盘中不补(self):
        d = _ledger()
        APPEND(d, datetime(2026, 10, 23, 10, 30))
        self.assertEqual(len(d["cum"]), 1, "盘中台账的 last_price 不是今日收盘 ⇒ 补了就是假数")

    def test_今天已有成交不补(self):
        d = _ledger()
        d["data_through"] = "2026-10-23"
        APPEND(d, datetime(2026, 10, 23, 16, 30))
        self.assertEqual(len(d["cum"]), 1)

    def test_幂等(self):
        d = _ledger()
        APPEND(d, datetime(2026, 10, 23, 16, 30))
        APPEND(d, datetime(2026, 10, 23, 16, 40))
        self.assertEqual(len(d["cum"]), 2, "重复调用不能补出第二个今日点")
        self.assertEqual(len(d["cum_all"]), 2)

    def test_空台账不炸(self):
        APPEND({}, datetime(2026, 10, 23, 16, 30))
        APPEND({"cum": []}, datetime(2026, 10, 23, 16, 30))


if __name__ == "__main__":
    unittest.main(verbosity=2)
