# -*- coding: utf-8 -*-
"""日线缓存「过去某天还挂着 forming」的拦截与修复（2026-11-03 owner：「11-02 的 K 线明显不对」）。

事故链：
  11-02 09:31 的**盘中快照根**（close=当时价、量只有零头）带着 `forming:["2026-11-02"]` 落盘；
  `is_unconfirmed_today_bar` 只拦"末行 == 今天"，到 11-03 就不再命中 ⇒ 半成品被当收盘。
  实测 2374 个缓存文件带 forming、2102 只收盘价与实际不符。

守三条：
  ① `is_stale_forming_tail`：**过去某天**仍标 forming ⇒ 不可当收盘；**当天** forming 正常放行；
  ② `load_daily_display` 命中该判据时返回 None（逼调用方重取真数据）；
  ③ 修复脚本能扫出这些文件（只挑"过去 + forming"），修完从 forming 里清掉。

运行：python -m unittest tests.phase3.test_daily_forming_tail_guard -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.chart_cache as cc  # noqa: E402
from core.market_data.tencent_provider import is_stale_forming_tail  # noqa: E402


def _rows(last_date, close=10.0, n=30):
    return [{"date": f"2026-10-{i:02d}", "open": 10.0, "close": 10.0, "high": 10.2,
             "low": 9.8, "volume": 1000.0} for i in range(1, n + 1)][:-1] + [
        {"date": last_date, "open": 10.0, "close": close, "high": max(close, 10.2),
         "low": min(close, 9.8), "volume": 1000.0}]


class TestGuard(unittest.TestCase):
    def test_过去某天还在forming_判陈旧(self):
        now = datetime(2026, 11, 3, 10, 0)
        cached = {"forming": ["2026-11-02"], "saved_at": "2026-11-02 09:31:38"}
        self.assertTrue(is_stale_forming_tail(_rows("2026-11-02"), cached, now=now))

    def test_当天forming_正常放行(self):
        now = datetime(2026, 11, 3, 10, 0)
        cached = {"forming": ["2026-11-03"], "saved_at": "2026-11-03 09:31:00"}
        self.assertFalse(is_stale_forming_tail(_rows("2026-11-03"), cached, now=now))

    def test_有真数据就不拦(self):
        now = datetime(2026, 11, 3, 10, 0)
        self.assertFalse(is_stale_forming_tail(_rows("2026-11-02"), {"forming": []}, now=now))


class TestLoadDailyDisplayRejects(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._old_dir = cc._DAILY_CACHE_DIR
        cc._DAILY_CACHE_DIR = Path(self._td.name)
        with cc._DISPLAY_LOCK:
            cc._DISPLAY_CACHE.clear()

    def tearDown(self):
        cc._DAILY_CACHE_DIR = self._old_dir
        with cc._DISPLAY_LOCK:
            cc._DISPLAY_CACHE.clear()
        self._td.cleanup()

    def _write(self, code, forming, close):
        fp = Path(self._td.name) / f"{code}.json"
        fp.write_text(json.dumps({
            "date": "2026-11-02", "saved_at": "2026-11-02 09:31:38",
            "forming": forming, "rows": _rows("2026-11-02", close),
        }, ensure_ascii=False), encoding="utf-8")

    def test_过去forming的缓存被判不可用(self):
        self._write("000002", ["2026-11-02"], 3.17)
        df = cc.load_daily_display("000002", now=datetime(2026, 11, 3, 10, 0))
        self.assertIsNone(df, "11-02 还挂着 forming ⇒ 必须判 None（逼重取）")

    def test_forming清掉后可正常读(self):
        self._write("000006", [], 5.9)
        df = cc.load_daily_display("000006", now=datetime(2026, 11, 3, 10, 0))
        self.assertIsNotNone(df)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 5.9)


class TestRepairScan(unittest.TestCase):
    def test_只挑过去且带forming的(self):
        from core import daily_repair as dr
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "000002.json").write_text(json.dumps(
                {"forming": ["2026-11-02"], "rows": _rows("2026-11-02")}), encoding="utf-8")
            (d / "000006.json").write_text(json.dumps(
                {"forming": [], "rows": _rows("2026-11-02")}), encoding="utf-8")
            today = datetime.now().strftime("%Y-%m-%d")
            (d / "000007.json").write_text(json.dumps(
                {"forming": [today], "rows": _rows(today)}), encoding="utf-8")
            (d / "index_sh000001.json").write_text(json.dumps(
                {"forming": ["2026-11-02"], "rows": _rows("2026-11-02")}), encoding="utf-8")
            codes = [t[0] for t in dr.find_forming_tails("2026-11-02", cache_dir=d)]
        self.assertEqual(codes, ["000002"], "只修个股、只修过去且带 forming 的")

class TestDropSuspendedFakeBar(unittest.TestCase):
    """停牌股：11-02 无成交（市场快照成交额 0），腾讯日线也没有这一天 ⇒ 删掉假 bar。"""

    def test_腾讯也没有该日则删掉假bar(self):
        import pandas as pd
        from unittest import mock
        from core import daily_repair as dr
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "000004.json"
            fp.write_text(json.dumps({
                "forming": ["2026-11-02"],
                "rows": [{"date": "2026-10-30", "open": 0.51, "close": 0.51,
                          "high": 0.51, "low": 0.51, "volume": 0.0},
                         {"date": "2026-11-02", "open": 0.51, "close": 0.51,
                          "high": 0.51, "low": 0.51, "volume": 0.0}],
            }, ensure_ascii=False), encoding="utf-8")
            fake = pd.DataFrame([{"date": "2026-10-30", "open": 0.51, "close": 0.51,
                                  "high": 0.51, "low": 0.51, "volume": 0.0}])
            with mock.patch("core.market_data.tencent_provider.daily_kline_raw",
                            return_value=fake):
                code, st, _d, _c = dr.fix_one(("000004", fp, "2026-11-02", 0.51))
            self.assertEqual(st, "cleared", "停牌日的平 bar 保留，只清 forming")
            d = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual([r["date"] for r in d["rows"]], ["2026-10-30", "2026-11-02"])
            self.assertNotIn("2026-11-02", d.get("forming") or [])

    def test_腾讯返回空但快照显示停牌_也删(self):
        import pandas as pd
        from unittest import mock
        from core import daily_repair as dr
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "000004.json"
            fp.write_text(json.dumps({
                "forming": ["2026-11-02"],
                "rows": [{"date": "2026-10-30", "open": 0.51, "close": 0.51,
                          "high": 0.51, "low": 0.51, "volume": 0.0},
                         {"date": "2026-11-02", "open": 0.51, "close": 0.51,
                          "high": 0.51, "low": 0.51, "volume": 0.0}],
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch("core.market_data.tencent_provider.daily_kline_raw",
                            return_value=pd.DataFrame()),                  mock.patch.object(dr, "_snapshot_zero_turnover", return_value=True):
                _code, st, _d, _c = dr.fix_one(("000004", fp, "2026-11-02", 0.51))
            self.assertEqual(st, "cleared")
            d = json.loads(fp.read_text(encoding="utf-8"))
            self.assertEqual([r["date"] for r in d["rows"]], ["2026-10-30", "2026-11-02"])

class TestWiring(unittest.TestCase):
    def test_读取路径挂了闸(self):
        src = (_ROOT / "core" / "chart_cache.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("is_stale_forming_tail"), 2,
                                "内存缓存命中 + 磁盘读取两条路都要判")
        self.assertIn("def is_stale_forming_tail", (
            _ROOT / "core" / "market_data" / "tencent_provider.py").read_text(encoding="utf-8"))

    def test_图表payload按收盘价比对(self):
        tg = (_ROOT / "t_gui.py").read_text(encoding="utf-8")
        self.assertIn("payload 里存的是盘中快照根", tg, "日期/长度闸相同，必须按值比对")


if __name__ == "__main__":
    unittest.main(verbosity=2)
