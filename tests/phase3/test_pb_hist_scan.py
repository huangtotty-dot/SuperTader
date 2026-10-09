# -*- coding: utf-8 -*-
"""建仓扫描「对历史任意一天重放」的离线单测（2026-10-09）。**全离线**。

需求：GUI 选历史日 → `recompute_pb(date)` → `run_position_scan(date_str=D, ...)` 重放该日扫描。
核心正确性：历史重放**只写按日 trace**，**绝不回写实时 watchlist 状态**（否则用历史结论覆盖当前
status/last_check_date/signal_history）。本测试把这个闸钉死。

覆盖：
  - 历史日重放：watchlist 文件**逐字节不变**（不回写）、trace 写到 `position_builder_{D}.jsonl`
  - 当天扫描：watchlist 会被回写（对照组，证明闸只对历史日生效）

桩：临时 watchlist 文件 + 补桩 `scan_stock`（无网络）+ 补桩 `_write_trace_line`（捕获路径）。
运行：python -m unittest tests.phase3.test_pb_hist_scan
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.position_builder as pb  # noqa: E402


def _fake_result():
    return {"code": "000001", "name": "甲", "latest_price": 10.0, "composite_score": 50,
            "verdict": "weak", "channel": "manual", "scan_type": "eod", "errors": []}


def _past_weekday(days=7):
    d = datetime.now() - timedelta(days=days)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


class TestPBHistScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="pbhist_")
        self._wl = os.path.join(self._tmp, "watchlist_buy.json")
        self._wl_body = {"total_capital": 300000,
                         "stocks": {"000001": {"name": "甲", "status": "monitoring"}}}
        with open(self._wl, "w", encoding="utf-8") as f:
            json.dump(self._wl_body, f, ensure_ascii=False)
        self._orig = {k: getattr(pb, k) for k in
                      ("WATCHLIST_FILE", "scan_stock", "_write_trace_line", "_write_manual_signal_event")}
        self._traces = []
        pb.WATCHLIST_FILE = __import__("pathlib").Path(self._wl)
        pb.scan_stock = lambda code, info, date_str=None, **kw: _fake_result()
        pb._write_trace_line = lambda rec, d: self._traces.append((d, rec))
        pb._write_manual_signal_event = lambda rec, d: None

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(pb, k, v)

    def _wl_bytes(self):
        with open(self._wl, "rb") as f:
            return f.read()

    def test_01_hist_run_does_not_touch_watchlist(self):
        before = self._wl_bytes()
        pb.run_position_scan(date_str=_past_weekday(7), scan_type="eod", silent=True, no_feishu=True)
        self.assertEqual(self._wl_bytes(), before, "历史日重放**不得**回写 watchlist_buy.json")
        self.assertTrue(self._traces, "历史日仍应写 trace")
        self.assertTrue(all(d == _past_weekday(7) for d, _ in self._traces),
                        "trace 应写到历史日文件")

    def test_02_today_run_still_writes_watchlist(self):
        before = self._wl_bytes()
        pb.run_position_scan(date_str=datetime.now().strftime("%Y-%m-%d"),
                             scan_type="eod", silent=True, no_feishu=True)
        self.assertNotEqual(self._wl_bytes(), before, "当天扫描应回写 watchlist（对照）")

    def test_03_scan_still_produces_rows_hist(self):
        res = pb.run_position_scan(date_str=_past_weekday(7), scan_type="eod",
                                   silent=True, no_feishu=True)
        self.assertTrue(res, "历史日重放应产出结果行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
