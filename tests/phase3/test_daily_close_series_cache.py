# -*- coding: utf-8 -*-
"""日线「日期+收盘」共享解析层（core.chart_cache.daily_close_series, P2-15）离线单测。

契约三条：
  ① 同文件内容不变（mtime_ns/size 不变）⇒ 不重解析，返回**同一个缓存对象**；
  ② 文件改写（mtime/size 变化）⇒ 键自然失效、重解析，拿到新值；同路径旧版本被逐出；
  ③ 任一行 close 转不动 = 整档解析失败 ⇒ ((), ()) 且不缓存（与调用方"样本不足跳过"同向）。

运行：python -m unittest tests.phase3.test_daily_close_series_cache -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.chart_cache as cc  # noqa: E402


def _rows(n=400, close0=10.0):
    return [{"date": "2026-01-%02d" % (i % 28 + 1), "open": 1, "high": 2,
             "low": 1, "close": close0 + i * 0.01, "volume": 1000 + i}
            for i in range(n)]


class _CountingReader:
    def __init__(self, real):
        self.calls = 0
        self._real = real

    def __call__(self, fp, default=None):
        self.calls += 1
        return self._real(fp, default)


class TestDailyCloseSeriesCache(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.fp = Path(self._td.name) / "600001.json"
        self._o_reader = cc._read_json
        cc.invalidate_daily_close_cache()          # 全清，防用例间串

    def tearDown(self):
        cc._read_json = self._o_reader
        cc.invalidate_daily_close_cache()
        self._td.cleanup()

    def _write(self, rows):
        self.fp.write_text(json.dumps({"rows": rows}), encoding="utf-8")

    def test_1_unchanged_file_not_reparsed(self):
        """① 内容不变 ⇒ 第二次调用命中缓存（零重解析、对象同一）。"""
        self._write(_rows())
        cnt = _CountingReader(self._o_reader)
        cc._read_json = cnt
        d1, c1 = cc.daily_close_series(self.fp)
        d2, c2 = cc.daily_close_series(self.fp)
        self.assertEqual(cnt.calls, 1, "内容不变必须只解析一次")
        self.assertIs((d1, c1) and d1, d2, "命中必须返回缓存本体（同一对象）")
        self.assertIs(c1, c2)
        self.assertEqual(len(d1), 400)
        self.assertAlmostEqual(c1[-1], 10.0 + 399 * 0.01, places=9)
        self.assertEqual(d1[0], "2026-01-01")      # str(date)[:10] 语义

    def test_2_mtime_change_reparses(self):
        """② 文件改写 ⇒ 旧键失效重解析、新值生效；同路径旧版本被逐出（缓存不双份）。"""
        self._write(_rows())
        cnt = _CountingReader(self._o_reader)
        cc._read_json = cnt
        d1, c1 = cc.daily_close_series(self.fp)
        self.assertEqual(cnt.calls, 1)
        # 改写：多一行（size 必变 ⇒ 键必变，不依赖 Windows 15.6ms 的 mtime 粒度）
        time.sleep(0.02)
        self._write(_rows(n=401))
        d2, c2 = cc.daily_close_series(self.fp)
        self.assertEqual(cnt.calls, 2, "mtime/size 变了必须重解析")
        self.assertEqual(len(d2), 401)
        self.assertIsNot(c1, c2)
        same_path = [k for k in cc._DAILY_KV_CACHE if k[0] == str(self.fp)]
        self.assertEqual(len(same_path), 1, "同路径只保留最新版本")

    def test_3_bad_close_whole_file_dropped(self):
        """③ 任一行 close 非数值 ⇒ ((), ()) 且不进缓存（下次仍尝试重解析）。"""
        rows = _rows()
        rows[123]["close"] = "not-a-number"
        self._write(rows)
        cnt = _CountingReader(self._o_reader)
        cc._read_json = cnt
        d1, c1 = cc.daily_close_series(self.fp)
        self.assertEqual((d1, len(c1)), ((), 0))
        cc.daily_close_series(self.fp)
        self.assertEqual(cnt.calls, 2, "解析失败不缓存 ⇒ 每次调用都重试")
        # 修好以后立即可用
        self._write(_rows())
        d3, c3 = cc.daily_close_series(self.fp)
        self.assertEqual(len(d3), 400)


if __name__ == "__main__":
    unittest.main()
