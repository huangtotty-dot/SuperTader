# -*- coding: utf-8 -*-
"""回归：chart_payload 里**盘中半成品根**必须被判旧（2026-10-25 owner 报 603667 10-23 那根不对）。

现场：payload 于 10-23 11:37 落盘，最后一行是当时的快照（收 43.09），
当天真实收盘 48.39。`payload_display_fresh` 只看日期 ⇒ 周末照旧命中，图一直显示错的。

三条判据（任一成立 ⇒ load_payload 返回 None，调用方重建）：
  ① provisional 且已过收盘；
  ② 与权威日线缓存同一天、收盘差 >0.3%；
  ③ 权威缓存比 payload 更新（payload 少一天）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import core.chart_cache as cc


class TestPayloadPartialBar(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._old = (cc._PAYLOAD_DIR, cc._DAILY_CACHE_DIR)
        cc._PAYLOAD_DIR = root / "chart_payload"
        cc._DAILY_CACHE_DIR = root / "daily_kline"
        cc._PAYLOAD_DIR.mkdir(parents=True)
        cc._DAILY_CACHE_DIR.mkdir(parents=True)

    def tearDown(self):
        cc._PAYLOAD_DIR, cc._DAILY_CACHE_DIR = self._old
        self._tmp.cleanup()

    def _write(self, code, payload_rows, cache_rows, saved_at, provisional=False):
        rec = {"date": saved_at[:10], "saved_at": saved_at, "schema": 4, "version": 1,
               "last_daily_date": payload_rows[-1]["date"], "provisional": provisional,
               "payload": {"available": True}, "daily_rows": payload_rows}
        (cc._PAYLOAD_DIR / f"{code}.json").write_text(json.dumps(rec), encoding="utf-8")
        (cc._DAILY_CACHE_DIR / f"{code}.json").write_text(
            json.dumps({"rows": cache_rows}), encoding="utf-8")

    def test_盘中共价差超过阈值判旧(self):
        rows = [{"date": "2026-10-23", "open": 44.1, "high": 45.29, "low": 42.5,
                 "close": 43.09, "volume": 105204}]
        cache = [{"date": "2026-10-23", "open": 44.1, "high": 48.59, "low": 42.5,
                  "close": 48.39, "volume": 261868}]
        self._write("603667", rows, cache, "2026-10-23 11:37:00")
        self.assertIsNone(cc.load_payload("603667", "2026-10-25 10:00:00"))

    def test_收盘价一致则命中(self):
        rows = [{"date": "2026-10-23", "open": 44.1, "high": 48.59, "low": 42.5,
                 "close": 48.39, "volume": 261868}]
        cache = [dict(rows[0])]
        self._write("603667", rows, cache, "2026-10-25 09:00:00")
        self.assertIsNotNone(cc.load_payload("603667", "2026-10-25 10:00:00"))

    def test_缓存更新时判旧(self):
        rows = [{"date": "2026-10-22", "open": 44.29, "high": 46.85, "low": 44.1,
                 "close": 44.66, "volume": 173239}]
        cache = rows + [{"date": "2026-10-23", "open": 44.1, "high": 48.59, "low": 42.5,
                         "close": 48.39, "volume": 261868}]
        self._write("603667", rows, cache, "2026-10-22 16:00:00")
        self.assertIsNone(cc.load_payload("603667", "2026-10-25 10:00:00"))

    def test_provisional过收盘判旧(self):
        rows = [{"date": "2026-10-23", "open": 44.1, "high": 48.59, "low": 42.5,
                 "close": 48.39, "volume": 261868}]
        self._write("603667", rows, list(rows), "2026-10-23 11:00:00", provisional=True)
        self.assertIsNone(cc.load_payload("603667", "2026-10-23 15:30:00"))


if __name__ == "__main__":
    unittest.main()
