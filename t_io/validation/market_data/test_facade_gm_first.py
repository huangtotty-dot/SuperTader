# coding=utf-8
"""test_facade_gm_first.py — 手动盘数据源与自动盘对齐（gm 优先，去腾讯 cache-first）验证

2026-08-31：facade.daily/minute 去掉腾讯 cache-first，改 gm 优先（与自动盘 gm_main 同源）；
腾讯仅做 gm 不可用时的降级兜底。

覆盖:
  T1  daily gm 优先：即使腾讯缓存存在且新鲜，gm 就绪时返回 gm（不再 cache-first）
  T2  daily gm 失败降级：gm 抛异常 → 腾讯兜底（source in cache/tencent/gm）不崩
  T3  minute gm 优先 + 60s 去重：60s 内同股同分钟两次调用只拉一次 gm
  T4  minute gm 不可用兜底：gm 不 ready → 腾讯兜底

运行: python t_io/validation/market_data/test_facade_gm_first.py
"""
import os
import sys
import unittest
from unittest import mock

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ST not in sys.path:
    sys.path.insert(0, _ST)

import pandas as pd
from core.market_data.facade import MarketDataFacade, _gm_minute_cache


def _mk_daily():
    return pd.DataFrame({"date": ["2026-08-26", "2026-08-27", "2026-08-28"],
                         "open": [10.0, 10.0, 10.0], "high": [11.0, 11.0, 11.0],
                         "low": [9.0, 9.0, 9.0], "close": [10.1, 10.2, 10.3],
                         "volume": [100.0, 100.0, 100.0]})


def _mk_minute():
    return pd.DataFrame({"time": pd.to_datetime(["2026-08-31 09:30:00", "2026-08-31 09:31:00"]),
                         "open": [10.0, 10.0], "high": [10.0, 10.0], "low": [10.0, 10.0],
                         "close": [10.0, 10.0], "volume": [100.0, 100.0], "amount": [1000.0, 1000.0]})


class TestFacadeGmFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = MarketDataFacade()

    def tearDown(self):
        _gm_minute_cache.clear()

    def test_daily_gm_first_even_cache_exists(self):
        """T1: gm 就绪且腾讯缓存存在 → 返回 gm（不再 cache-first）。"""
        self.f._gm._ready = True
        with mock.patch.object(self.f._gm, "daily", return_value=_mk_daily()) as m_gm, \
             mock.patch.object(self.f._tx, "daily_cache", return_value=_mk_daily()) as m_cache, \
             mock.patch.object(self.f, "_maybe_append_forming", side_effect=lambda df, c: df):
            df = self.f.daily("600481")
        self.assertEqual(df.attrs.get("source"), "gm")
        m_gm.assert_called_once()
        m_cache.assert_not_called()   # 不再优先读腾讯缓存

    def test_daily_gm_fail_falls_back(self):
        """T2: gm 抛异常 → 腾讯兜底不崩。"""
        self.f._gm._ready = True
        with mock.patch.object(self.f._gm, "daily", side_effect=RuntimeError("gm down")), \
             mock.patch.object(self.f._tx, "daily", return_value=_mk_daily()) as m_tx:
            df = self.f.daily("600481")
        self.assertIn(df.attrs.get("source"), ("cache", "tencent", "gm"))
        m_tx.assert_called_once()

    def test_minute_gm_dedup_within_ttl(self):
        """T3: 60s 内两次调用同股同分钟 → 第二次不触发 _gm.minute（去重）。"""
        self.f._gm._ready = True
        with mock.patch.object(self.f._gm, "minute", return_value=_mk_minute()) as m_gm, \
             mock.patch.object(self.f._tx, "save_minute_cache", return_value=None):
            d1 = self.f.minute("600481", "2026-08-31")
            self.assertEqual(d1.attrs.get("source"), "gm")
            d2 = self.f.minute("600481", "2026-08-31")
        self.assertEqual(d2.attrs.get("source"), "gm")
        self.assertEqual(m_gm.call_count, 1)   # 第二次命中 60s 去重缓存不重复拉

    def test_minute_gm_down_falls_back(self):
        """T4: gm 不 ready → 腾讯兜底。"""
        self.f._gm._ready = False
        with mock.patch.object(self.f._tx, "minute", return_value=_mk_minute()) as m_tx:
            df = self.f.minute("600481", "2026-08-31")
        m_tx.assert_called_once()
        self.assertIn(df.attrs.get("source"), ("cache", "tencent", "gm"))


if __name__ == "__main__":
    unittest.main()
