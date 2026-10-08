# -*- coding: utf-8 -*-
"""「站上5/10日线」（破线回站）报警离线单测（2026-10-08）。

owner 需求：破5日线有飞书通知，**从破线转为站上**也要通知。
覆盖：T1 `check_ma_break` 的 reclaim 判定（昨破→今上）T2 卡片构造 T3 去重存取。
铁律全离线：patch `fetch_daily_kline` / `load_snapshot_df`，不打网络。

运行：python tests/phase3/test_ma_reclaim_alert.py
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import core.position_builder as pb  # noqa: E402


def _mk_df(closes, end_date):
    n = len(closes)
    dates = [(end_date - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d") for i in range(n)]
    return pd.DataFrame({"date": dates, "open": closes, "high": closes,
                         "low": closes, "close": closes, "volume": [1.0] * n})


class TestReclaimDetect(unittest.TestCase):
    def _check(self, closes):
        end = datetime.now()
        endd = end.strftime("%Y-%m-%d")
        df = _mk_df(closes, end)
        with mock.patch.object(pb, "fetch_daily_kline", return_value=df), \
             mock.patch.object(pb, "load_snapshot_df",
                               return_value=(pd.DataFrame(), None, None)):
            return pb.check_ma_break("600000", {"name": "x"}, endd)

    def test_昨破今上_判reclaim5(self):
        # 前 10 根 10.0，倒数第二根跌到 9.0（破线），最后一根(当日)回到 10.5（站上）
        r = self._check([10.0] * 10 + [9.0, 10.5])
        self.assertFalse(r["broke5"], "这是回站、不是刚破")
        self.assertTrue(r["reclaim5"], "昨收<昨MA5 且 现价>今MA5 ⇒ 应判 reclaim5")
        self.assertGreater(r["price"], r["ma5"])

    def test_一直站上_不判reclaim(self):
        r = self._check([10.0] * 11 + [10.5])
        self.assertFalse(r["reclaim5"])

    def test_刚破线_判broke不判reclaim(self):
        r = self._check([10.0] * 10 + [10.5, 9.0])
        self.assertTrue(r["broke5"])
        self.assertFalse(r["reclaim5"])


class TestCard(unittest.TestCase):
    def test_卡片绿色且含标签(self):
        ev = [{"code": "600000", "name": "浦发", "is_holding": True, "reclaim5": True,
               "reclaim10": False, "price": 10.5, "ma5": 10.1, "ma10": 9.9,
               "dev5_pct": 3.96, "dev10_pct": 6.06}]
        c = pb.build_ma_reclaim_card(ev, "2026-10-08")
        self.assertEqual(c["card"]["header"]["template"], "green")
        self.assertIn("站上5日线", c["card"]["elements"][0]["content"])
        self.assertIn("持仓", c["card"]["elements"][0]["content"])

    def test_空事件返回None(self):
        self.assertIsNone(pb.build_ma_reclaim_card([], "2026-10-08"))


class TestDedup(unittest.TestCase):
    def test_存取回环(self):
        tmp = Path(tempfile.mkdtemp(prefix="ma_reclaim_")) / "s.json"
        with mock.patch.object(pb, "MA_RECLAIM_STATE_FILE", tmp):
            pb._mark_ma_reclaim_pushed("600000", "2026-10-08")
            pb._mark_ma_reclaim_pushed("600000", "2026-10-08")   # 幂等
            pb._mark_ma_reclaim_pushed("000001", "2026-10-08")
            d = pb._load_ma_reclaim_dedup()
        self.assertEqual(sorted(d["2026-10-08"]), ["000001", "600000"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
