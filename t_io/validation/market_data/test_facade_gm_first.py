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
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ST not in sys.path:
    sys.path.insert(0, _ST)

import pandas as pd
from core.market_data.facade import MarketDataFacade, _gm_minute_cache
from core.market_data import tencent_provider


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

    # ---------- F-6(2026-09-04): ETF 日线链可观测 — 兜底复用昨日缓存需非空且不重复日期 ----------
    _CODE = "999999"   # 哨兵码：urlopen 全 mock，不触真网

    @staticmethod
    def _write_cache(tmpdir, code, date, saved_at, rows):
        os.makedirs(tmpdir, exist_ok=True)
        with open(os.path.join(tmpdir, f"{code}.json"), "w", encoding="utf-8") as f:
            f.write(tencent_provider.json.dumps(
                {"date": date, "saved_at": saved_at, "rows": rows}, ensure_ascii=False))

    @staticmethod
    def _rows(dates):
        return [{"date": d, "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
                 "volume": 1000.0} for d in dates]

    @staticmethod
    def _snap(ts_date, price=10.2):
        return {TestFacadeGmFirst._CODE: {"ts_date": ts_date, "price": price, "open": 10.0,
                                          "high": 10.5, "low": 9.8, "volume": 1000.0}}

    def test_f6_gm_empty_tx_501_yesterday_cache_source_cache(self):
        """F6-T1: gm 返空 + 腾讯主机全挂(501) + 仅昨日缓存 → facade.daily 非空且 source=cache，
        且今日 forming bar 补入一次。"""
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        tmp = tempfile.mkdtemp(prefix="f6_t1_")
        try:
            self._write_cache(tmp, self._CODE, yesterday, yesterday + " 15:00:00",
                              self._rows([(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                                          for i in range(5, 1, -1)]))  # 仅昨日及更早行
            self.f._gm._ready = True
            with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("sim 501")), \
                 mock.patch.object(tencent_provider, "_DAILY_CACHE_DIR", tmp), \
                 mock.patch.object(self.f._gm, "daily",
                                   return_value=pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])), \
                 mock.patch.object(self.f._tx, "snapshot", return_value=self._snap(today)):
                df = self.f.daily(self._CODE)
            self.assertFalse(df.empty)
            self.assertEqual(df.attrs.get("source"), "cache")
            today_rows = [r for r in df["date"].astype(str) if r == today]
            self.assertEqual(len(today_rows), 1)   # forming bar 只补一次，不重复
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_f6_dedup_no_double_today_row(self):
        """F6-T2: 缓存已含今日行(陈旧>15min 触发 live→兜底) → 守卫不重复追加今日 forming。"""
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        stale = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        tmp = tempfile.mkdtemp(prefix="f6_t2_")
        try:
            # date=today 但 saved_at 20 分钟前 → daily_cache 判 stale 返 None → 走 live → 兜底读同缓存
            self._write_cache(tmp, self._CODE, today, stale,
                              self._rows([yesterday, today]))
            self.f._gm._ready = True
            with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("sim 501")), \
                 mock.patch.object(tencent_provider, "_DAILY_CACHE_DIR", tmp), \
                 mock.patch.object(self.f._gm, "daily",
                                   return_value=pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])), \
                 mock.patch.object(self.f._tx, "snapshot", return_value=self._snap(today)):
                df = self.f.daily(self._CODE)
            self.assertFalse(df.empty)
            self.assertEqual(df.attrs.get("source"), "cache")
            today_rows = [r for r in df["date"].astype(str) if r == today]
            self.assertEqual(len(today_rows), 1)   # 修复前=2(重复 09-04 实测 347 行案)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
