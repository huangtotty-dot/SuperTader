# -*- coding: utf-8 -*-
"""技术标签批量提速的回归闸（2026-10-23 owner：「选股猎手的技术标签更新实在是太慢」）。

实测 50 只板块：**40s → 6.4s**，3 秒出第一批。三条根因各钉一条：

1. **增量交付**：批量缓存 `_TAGS_CACHE` 是「整批算完才落」，而逐只结果早就在
   `_TAGS_ONE_CACHE` ⇒ `_tags_partial` 先把已算好的那部分给前端（否则要等最慢那只）。
2. **30min 取数不该占 pandas 名额**：取数 190ms/只**纯网络**，原来串在
   `_BG_PANDAS_SEM`(2) 里 ⇒ `prefetch_30min` 8 路并发取回、`get_trend30` 直接命中。
3. **结构层预热让路**：`load_stock_tags_batch` 顺带排的「仓位结构」预热同样出网、
   同样抢那 2 个名额 ⇒ 实测把标签从 7.5s 拖到 19.5s，必须等标签批算完再排。
"""
import os
import sys
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import t_gui  # noqa: E402


def _bars(n=200):
    """合成 30min 帧（时间升序，够状态机的 min_bars）。"""
    t = pd.date_range("2026-09-01 09:30", periods=n, freq="30min")
    px = 10 + np.cumsum(np.random.default_rng(0).normal(0, 0.02, n))
    return pd.DataFrame({"time": t, "open": px, "high": px + 0.05,
                         "low": px - 0.05, "close": px, "volume": 1000.0})


class TestIncrementalDelivery(unittest.TestCase):
    def setUp(self):
        self._one = dict(t_gui._TAGS_ONE_CACHE)
        t_gui._TAGS_ONE_CACHE.clear()

    def tearDown(self):
        t_gui._TAGS_ONE_CACHE.clear()
        t_gui._TAGS_ONE_CACHE.update(self._one)

    def test_已算好的逐只结果会先给出去(self):
        import time
        t_gui._TAGS_ONE_CACHE[("600000", True, "")] = (
            time.time(), {"trend": "flat", "tags": [{"label": "震荡", "color": "neutral"}]})
        got = t_gui.Api._tags_partial(["600000", "600001"], True)
        self.assertIn("600000", got, "算好的那只必须能立刻拿到")
        self.assertNotIn("600001", got, "没算完的不能编造")

    def test_空入参(self):
        self.assertEqual(t_gui.Api._tags_partial([], True), {})


class TestPrefetch30min(unittest.TestCase):
    def setUp(self):
        from analysis.trend30 import adapter as ad
        self.ad = ad
        self._raw = dict(ad._RAW)
        self._cache = dict(ad._CACHE)
        ad._RAW.clear()
        ad._CACHE.clear()

    def tearDown(self):
        self.ad._RAW.clear()
        self.ad._RAW.update(self._raw)
        self.ad._CACHE.clear()
        self.ad._CACHE.update(self._cache)

    def test_预取后_get_trend30_不再取数(self):
        calls = []

        def _fake(c, days=35):
            calls.append(c)
            return _bars()

        with mock.patch.object(self.ad, "_fetch_30min", _fake):
            self.ad.prefetch_30min(["600000", "600001"], workers=2)
            n1 = len(calls)
            self.assertEqual(n1, 2)
            r = self.ad.get_trend30("600000")
            self.assertEqual(len(calls), n1, "预取命中后不能再出网取数")
            self.assertEqual(r.get("source"), "30min")

    def test_同一时段重复预取会跳过(self):
        calls = []

        def _fake(c, days=35):
            calls.append(c)
            return _bars()

        with mock.patch.object(self.ad, "_fetch_30min", _fake):
            self.ad.prefetch_30min(["600000"])
            n1 = len(calls)
            self.ad.prefetch_30min(["600000"])
            self.assertEqual(len(calls), n1, "同一 30min 时段不该重复取")


class TestStructWarmDefers(unittest.TestCase):
    def test_结构层预热让路给标签批(self):
        import inspect
        src = inspect.getsource(t_gui.Api.load_stock_tags_batch)
        self.assertIn("if _struct_pending and not _TAGS_RUNNING", src,
                      "结构层预热必须在标签批运行时让路（共用 2 个 pandas 名额）")
        self.assertIn("_spawn_bg()\n        _maybe_kick_struct()", src,
                      "必须先起标签批（置 _TAGS_RUNNING）再判结构层——顺序反了等于没让路")

    def test_标签批先并发预取30min(self):
        import inspect
        src = inspect.getsource(t_gui.Api.load_stock_tags_batch)
        self.assertIn("prefetch_30min", src, "批量前要先 8 路并发取数，别让网络占着 pandas 名额")


if __name__ == "__main__":
    unittest.main(verbosity=2)
