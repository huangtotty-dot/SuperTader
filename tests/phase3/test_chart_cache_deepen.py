# -*- coding: utf-8 -*-
"""`chart_cache.deepen_daily`：历史偏短的票用长窗口补齐（2026-10-30）。

守三条：
  ① **先探针**：数据源取不到 ⇒ 直接 `provider_down` 返回，不许对几百只逐个付超时；
  ② 探针成功才走 `prefetch_daily(skip_current=False, days=800)`（merge 语义，不截短）；
  ③ `limit` 生效，且按传入顺序取前 N 只。

运行：python -m pytest -q tests/phase3/test_chart_cache_deepen.py
"""
import os
import sys
import unittest
from unittest import mock

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.chart_cache as cc  # noqa: E402


class _Prov:
    def __init__(self, ok=True, source="gm"):
        self.ok = ok
        self.source = source
        self.calls = []

    def daily(self, code, days=800):
        self.calls.append((code, days))
        if not self.ok:
            return pd.DataFrame()
        df = pd.DataFrame([{"date": "2026-10-30", "open": 1, "high": 1, "low": 1,
                            "close": 1, "volume": 1}])
        df.attrs["source"] = self.source
        return df


class TestDeepenDaily(unittest.TestCase):
    def test_探针只拿到缓存也算不可用(self):
        """facade.daily 在 GM/腾讯都不可用时会原样返回磁盘缓存（source=cache）——
        那不是"补到长历史"，必须判 provider_down。"""
        prov = _Prov(ok=True, source="cache")
        with mock.patch("core.market_data.get_provider", return_value=prov),              mock.patch.object(cc, "prefetch_daily") as pf:
            st = cc.deepen_daily(["301079"])
        self.assertEqual(st["skipped"], "provider_down")
        self.assertFalse(pf.called)

    def test_空列表(self):
        self.assertEqual(cc.deepen_daily([])["skipped"], "empty")

    def test_数据源不可用直接跳过(self):
        prov = _Prov(ok=False)
        with mock.patch("core.market_data.get_provider", return_value=prov), \
             mock.patch.object(cc, "prefetch_daily") as pf:
            st = cc.deepen_daily(["301079", "000006"])
        self.assertEqual(st["skipped"], "provider_down")
        self.assertFalse(pf.called, "探针失败后不得再批量拉")
        self.assertEqual(len(prov.calls), 1, "只允许一次探针")

    def test_探针成功则按limit补长历史(self):
        prov = _Prov(ok=True)
        with mock.patch("core.market_data.get_provider", return_value=prov), \
             mock.patch.object(cc, "prefetch_daily",
                               return_value={"requested": 2, "got": 2, "skipped": 0,
                                             "failed": 0}) as pf:
            st = cc.deepen_daily(["301079", "000006", "000008"], limit=2, days=800)
        self.assertTrue(st.get("deepened"))
        args, kw = pf.call_args
        self.assertEqual(args[0], ["301079", "000006"], "limit 生效且保序")
        self.assertEqual(kw.get("days"), 800)
        self.assertFalse(kw.get("skip_current", True), "必须强制重取（这些票本来就是'够新'但太短）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
