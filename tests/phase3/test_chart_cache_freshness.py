# -*- coding: utf-8 -*-
"""`core.chart_cache` 缓存层离线单测（2026-10-04）。

覆盖「盘后预下载 + 盘中增量读」的地基：交易日近似新鲜度、日线展示缓存读取、
图表 payload 落盘/读取、分钟历史落盘/读取。**全离线**：monkeypatch 缓存目录到 tmp，
不读真实 t_io/cache、不打网络。

运行：python tests/phase3/test_chart_cache_freshness.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
from core import chart_cache as cc  # noqa: E402


def _d(gap_days):
    return (datetime.now() - timedelta(days=gap_days)).strftime("%Y-%m-%d")


class TestFreshness(unittest.TestCase):
    def test_display_阈值(self):
        self.assertTrue(cc.is_display_fresh([{"date": _d(0)}]))
        self.assertTrue(cc.is_display_fresh([{"date": _d(12)}]))   # 长假上限
        self.assertFalse(cc.is_display_fresh([{"date": _d(13)}]))
        self.assertFalse(cc.is_display_fresh([{"date": "garbage"}]))
        self.assertFalse(cc.is_display_fresh([]))

    def test_prefetch_current_阈值(self):
        self.assertTrue(cc.is_prefetch_current([{"date": _d(0)}]))
        self.assertTrue(cc.is_prefetch_current([{"date": _d(5)}]))
        self.assertFalse(cc.is_prefetch_current([{"date": _d(6)}]))

    def test_末行日期优先_不看写入日(self):
        """核心回归：缓存只看**末行数据日期**，不看写入日 `date` —— 盘后 D 日落的缓存 D+1 仍应命中。"""
        rows = [{"date": _d(0)}]
        self.assertTrue(cc.is_display_fresh(rows))
        # 末行=今天但写入日=昨天，仍算新鲜（旧 daily_cache 会因 date!=today 判死）
        self.assertEqual(cc.last_row_date(rows), _d(0))

    def test_gap_解析(self):
        self.assertEqual(cc.natural_gap_days(_d(3)), 3)
        self.assertEqual(cc.natural_gap_days("bad"), 9999)


class TestDailyDisplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc_daily_")
        self._orig = cc._DAILY_CACHE_DIR
        cc._DAILY_CACHE_DIR = Path(self.tmp)

    def tearDown(self):
        cc._DAILY_CACHE_DIR = self._orig

    def _write(self, code, last_date):
        rows = [{"date": (datetime.strptime(last_date, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d"),
                 "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100.0} for i in range(5)][::-1]
        (Path(self.tmp) / f"{code}.json").write_text(
            json.dumps({"date": _d(99), "saved_at": "x", "rows": rows}), encoding="utf-8")

    def test_新鲜命中_陈旧不命中(self):
        self._write("600000", _d(1))
        df = cc.load_daily_display("600000")
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 5)
        self.assertEqual(df.attrs.get("source"), "cache")
        self._write("600001", _d(20))
        self.assertIsNone(cc.load_daily_display("600001"), "陈旧缓存必须 miss")

    def test_缺失返回None(self):
        self.assertIsNone(cc.load_daily_display("999999"))


class TestPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc_payload_")
        self._orig = cc._PAYLOAD_DIR
        cc._PAYLOAD_DIR = Path(self.tmp)

    def tearDown(self):
        cc._PAYLOAD_DIR = self._orig

    def test_roundtrip(self):
        payload = {"available": True, "version": "v1", "period_data": {"daily": {"dates": [_d(1)]}}}
        rows = [{"date": _d(1)}]
        self.assertTrue(cc.save_payload("600000", payload, rows))
        hit = cc.load_payload("600000")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["version"], "v1")
        self.assertEqual(hit["last_daily_date"], _d(1))
        self.assertTrue(cc.payload_display_fresh(hit))

    def test_不可用不写(self):
        self.assertFalse(cc.save_payload("600000", {"available": False}, []))
        self.assertIsNone(cc.load_payload("600000"))

    def test_容量上限LRU淘汰(self):
        for i in range(6):
            cc.save_payload(f"60000{i}", {"available": True, "version": f"v{i}"}, [{"date": _d(1)}])
            import time as _t
            _t.sleep(0.01)   # 拉开 mtime
        self.assertEqual(len(list(Path(self.tmp).glob("*.json"))), 6)
        removed = cc.enforce_payload_cap(3)
        self.assertEqual(removed, 3)
        self.assertEqual(len(list(Path(self.tmp).glob("*.json"))), 3)
        # 最旧的应被删（600000/600001/600002 走人）
        self.assertFalse((Path(self.tmp) / "600000.json").exists())
        self.assertTrue((Path(self.tmp) / "600005.json").exists())


class TestMinuteCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc_min_")
        self._orig = cc._MINUTE_CACHE_DIR
        cc._MINUTE_CACHE_DIR = Path(self.tmp)

    def tearDown(self):
        cc._MINUTE_CACHE_DIR = self._orig

    def test_roundtrip_merge(self):
        t = datetime.now().strftime("%Y-%m-%d 14:00")
        df = pd.DataFrame([{"time": t, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 5.0}])
        self.assertTrue(cc.save_minute_history("600000.SH", "30min", df))
        got = cc.load_minute_history("600000.SH", "30min")
        self.assertIsNotNone(got)
        self.assertEqual(len(got), 1)
        # 二次 merge 同 time 应去重
        cc.save_minute_history("600000.SH", "30min", df)
        self.assertEqual(len(cc.load_minute_history("600000.SH", "30min")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
