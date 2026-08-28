# -*- coding: utf-8 -*-
"""P1 审核行为级测试（阻断 1-5）。
针对审核打回项的行为验证；网络依赖用例网络不可用时 graceful skip。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]


class TestBlocking1_IndexAmount(unittest.TestCase):
    """阻断1: index_daily 必须含 amount（_two_market_amount 依赖，缺则 KeyError → regime 恒 RANGE）。"""

    def test_index_daily_has_amount(self):
        from core.market_data import get_provider
        df = get_provider().index_daily("sh000001", 30, "2026-08-27")
        self.assertIn("amount", df.columns, "index_daily 缺 amount 列（_two_market_amount 依赖）")
        self.assertFalse(df["amount"].isna().all(), "amount 不应全 NaN")
        self.assertGreater(float(df["amount"].iloc[-1]), 0, "amount 应有非零值（两市成交额腿）")


class TestBlocking2_WeekMonthCollapse(unittest.TestCase):
    """阻断2: 周/月线不能坍缩（用 count 根日线重采样会只剩 ~count/5 条）。"""

    def test_week_not_collapsed(self):
        from core.market_review import _tx_kline
        wk = _tx_kline("sh000001", "week", 52, "2026-08-27")
        self.assertGreaterEqual(len(wk), 40, f"周线坍缩: 仅 {len(wk)} 条（请求 52）")

    def test_month_not_collapsed(self):
        from core.market_review import _tx_kline
        mo = _tx_kline("sh000001", "month", 12, "2026-08-27")
        self.assertGreaterEqual(len(mo), 9, f"月线坍缩: 仅 {len(mo)} 条（请求 12）")


class TestBlocking3_MinuteDateForgery(unittest.TestCase):
    """阻断3: 分钟线不能伪造日期（周末/节假日把上一交易日伪造成当日 → 空）。"""

    def _fake_qt_minute(self, resp_date_ymd):
        """构造腾讯 minute 响应（date=上一交易日，rows=当日分钟行），模拟周末/节假日。"""
        body = (
            f'{{"code":0,"data":{{"sh600481":{{"data":{{"date":"{resp_date_ymd}",'
            f'"data":["0930 4.34 894 387996.00","0931 4.32 6889 2978393.00"]}}}}}}}}'
        ).encode("utf-8")

        class FakeResp:
            def read(self):
                return body

        def fake_urlopen(req, timeout=10):
            return FakeResp()

        return fake_urlopen

    def test_weekend_returns_empty(self):
        from core.market_data.tencent_provider import TencentProvider
        tx = TencentProvider()
        # 请求 2026-08-29（周六），响应日期是 08-28（上一交易日）→ 必须返回空
        with mock.patch("urllib.request.urlopen", self._fake_qt_minute("20260828")):
            df = tx.minute("600481", "2026-08-29")
        self.assertTrue(df.empty, "周末/节假日不应伪造上一交易日分钟线")


class TestBlocking4_LimitFlag(unittest.TestCase):
    """阻断4: stock_hunter 涨停列必须是 0/1 布尔旗标（heat_tracker 按 0/1 计数）。"""

    def test_limit_flag_is_boolean(self):
        from stock_hunter.modules.market_data import MarketDataFetcher
        md = MarketDataFetcher()
        df = md._fetch_spot_tencent(["600481", "002639"])
        if df.empty:
            self.skipTest("快照为空")
        self.assertIn("涨停", df.columns)
        for v in df["涨停"]:
            self.assertIn(int(v), (0, 1), f"涨停应 0/1，得到 {v}")


class TestBlocking5_FormingBar(unittest.TestCase):
    """阻断5: gm 日线盘中缺当日 forming bar → facade 补（带 ts_date 新鲜度闸）。"""

    def _mk_daily(self, last_date):
        import pandas as pd
        rows = [{"date": d, "open": 4.0, "high": 4.1, "low": 3.9,
                 "close": 4.05, "volume": 100.0} for d in ("2026-08-26", last_date)]
        return pd.DataFrame(rows)

    def test_appends_forming_when_snapshot_today(self):
        from core.market_data.facade import MarketDataFacade
        f = MarketDataFacade()
        df = self._mk_daily("2026-08-27")
        with mock.patch.object(f._tx, "snapshot",
                               return_value={"600481": {"price": 4.2, "open": 4.1, "high": 4.3,
                                                        "low": 4.0, "volume": 50.0,
                                                        "ts_date": "2026-08-28"}}):
            out = f._maybe_append_forming(df, "600481")
        self.assertEqual(out["date"].iloc[-1], "2026-08-28", "盘中应补当日 forming bar")

    def test_no_append_when_snapshot_yesterday(self):
        from core.market_data.facade import MarketDataFacade
        f = MarketDataFacade()
        df = self._mk_daily("2026-08-27")
        with mock.patch.object(f._tx, "snapshot",
                               return_value={"600481": {"price": 4.2, "ts_date": "2026-08-27"}}):
            out = f._maybe_append_forming(df, "600481")
        self.assertEqual(out["date"].iloc[-1], "2026-08-27", "快照非当日不应补 forming bar")


if __name__ == "__main__":
    unittest.main()
