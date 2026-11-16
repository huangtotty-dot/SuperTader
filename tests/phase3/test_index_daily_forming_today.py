# -*- coding: utf-8 -*-
"""指数日线「当日 forming bar」补丁（2026-11-16，全离线）。

owner 报「尾盘市场温度不更新」，根因链：
  facade.index_daily 只在 `end_date is None` 时调 `_maybe_append_index_forming`，
  而 index_regime 引擎**总是**带 end_date ⇒ GM 正常时最后一根停在上一交易日 ⇒
  regime 的 `date` 退回昨天 ⇒ E5 涨跌停池按昨天取 ⇒ e5_ths_cache / sentiment_daily
  永远缺当天行（14:30 尾盘快照写了旧日期）。

钉住两条：
  A. `end_date == 今天` ⇒ 允许补当日 forming bar（引擎能看到今天的指数）；
  B. `end_date` 是历史日 ⇒ **绝不**补（否则回放/回测会把今天的 bar 塞进历史）。

运行：python -m unittest tests.phase3.test_index_daily_forming_today -v
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import analysis.index_regime as ir  # noqa: E402


def _df(*dates):
    return pd.DataFrame({
        "date": list(dates),
        "open": [1.0] * len(dates), "high": [1.0] * len(dates),
        "low": [1.0] * len(dates), "close": [1.0] * len(dates),
        "volume": [1.0] * len(dates),
    })


class _FakeProvider:
    """最小 facade 替身：index_daily 只回「已定型」的最后一根，补丁由本类模拟。"""

    def __init__(self):
        self.appended = 0

    def index_daily(self, symbol, days, end_date):
        return _df("2026-11-12", "2026-11-13")

    def _maybe_append_index_forming(self, df, index="sh000001"):
        self.appended += 1
        return pd.concat([df, _df("2026-11-16")], ignore_index=True)


class TestIndexDailyFormingToday(unittest.TestCase):
    def test_appends_today_forming_bar(self):
        prov = _FakeProvider()
        with mock.patch("core.market_data.get_provider", return_value=prov), \
                mock.patch.object(ir, "_ir_now", return_value=datetime(2026, 11, 16, 14, 30)):
            out = ir._ir_fetch_index_daily_tx("sh000001", "2026-11-16", 100, {})
        self.assertEqual(str(out["date"].iloc[-1]), "2026-11-16")
        self.assertEqual(prov.appended, 1)

    def test_historical_end_date_untouched(self):
        """历史日（回放/回测）不补当日 bar —— 否则会把今天的行情塞进过去。"""
        prov = _FakeProvider()
        with mock.patch("core.market_data.get_provider", return_value=prov), \
                mock.patch.object(ir, "_ir_now", return_value=datetime(2026, 11, 16, 14, 30)):
            out = ir._ir_fetch_index_daily_tx("sh000001", "2026-11-13", 100, {})
        self.assertEqual(str(out["date"].iloc[-1]), "2026-11-13")
        self.assertEqual(prov.appended, 0)

    def test_provider_without_helper_still_works(self):
        """没这个钩子的 provider（腾讯直连等）照常返回，不抛。"""
        class _Bare:
            def index_daily(self, symbol, days, end_date):
                return _df("2026-11-13")
        with mock.patch("core.market_data.get_provider", return_value=_Bare()), \
                mock.patch.object(ir, "_ir_now", return_value=datetime(2026, 11, 16, 14, 30)):
            out = ir._ir_fetch_index_daily_tx("sh000001", "2026-11-16", 100, {})
        self.assertEqual(str(out["date"].iloc[-1]), "2026-11-13")


if __name__ == "__main__":
    unittest.main(verbosity=2)
