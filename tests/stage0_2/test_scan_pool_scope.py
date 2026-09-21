# -*- coding: utf-8 -*-
"""建仓扫描的标的范围单测（2026-09-21）。

## 为什么要有这个测试

owner 报「建仓扫描中有很多股票一直处于等待扫描状态」。排查结论：

- 人工盘扫描原按 P3-2 池分管 `_is_manual_pool(k, v)` **只扫 manual 池**；
- 而 `pool` 字段曾被两个写入口压平（`sync_watchlist_pool(code,"auto")` 与
  `add_to_watchlist` 把 holdings 的 `both` 也写成 `auto`）⇒ `_is_manual_pool=False`
  ⇒ 那些标的**永远不进扫描**、人工盘表一直显示"等待扫描"、拿不到判定/得分；
- 缺口不止 auto 池：还有两只（300364/515180）根本不在 AUTO_POOL，属权威 manual，
  却被陈旧字段挡在扫描之外。

owner 拍板（2026-09-21）：**人工盘扫描不再按池排除** —— 股池里 monitoring/signal 的
标的全扫。本测试把这条钉死，防止有人把 `and _is_manual_pool(k, v)` 加回来。

全测试 monkeypatch，不联网、不写真实轨迹。

运行：python tests/stage0_2/test_scan_pool_scope.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "config") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "config"))

import core.position_builder as pb  # noqa: E402


def _min_result(code, name):
    """scan_stock 的最小可用返回（scan_error 缺省 ⇒ 会走 update_watchlist）。"""
    return {"code": code, "name": name, "verdict": "weak", "composite_score": 0,
            "conditions": {}, "channels": {}, "errors": []}


class TestScanPoolScope(unittest.TestCase):
    def setUp(self):
        self.scanned = []
        self._orig = {k: getattr(pb, k) for k in
                      ("WATCHLIST_FILE", "scan_stock", "update_watchlist", "_write_trace_line")}

        def _fake_scan(code, info, *a, **k):
            self.scanned.append(code)
            return _min_result(code, (info or {}).get("name", code))

        pb.scan_stock = _fake_scan
        pb.update_watchlist = lambda r, w: None
        pb._write_trace_line = lambda entry, d: None

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(pb, k, v)

    def _write_wl(self, td, stocks):
        fp = Path(td) / "watchlist_buy.json"
        fp.write_text(json.dumps({"stocks": stocks, "total_capital": 300000,
                                  "max_per_stock_pct": 0.2}), encoding="utf-8")
        pb.WATCHLIST_FILE = fp

    def test_auto_pool_stock_is_scanned(self):
        """核心回归：watchlist pool=auto 的标的也必须被人工盘扫到。"""
        with tempfile.TemporaryDirectory() as td:
            self._write_wl(td, {
                "600000": {"name": "浦发银行", "status": "monitoring", "pool": "manual"},
                "000988": {"name": "华工科技", "status": "monitoring", "pool": "auto"},
                "002451": {"name": "摩恩电气", "status": "monitoring", "pool": "both"},
            })
            pb.run_position_scan(date_str="2026-09-21", scan_type="manual",
                                 silent=True, no_feishu=True)

        self.assertIn("600000", self.scanned, "manual 池本就在范围内")
        self.assertIn("000988", self.scanned,
                      "pool=auto 的标的也必须扫（不再按池排除）—— 否则就是'等待扫描'永不消失")
        self.assertIn("002451", self.scanned, "both 池也应扫")

    def test_pool_field_absent_still_scanned(self):
        """历史条目可能没有 pool 字段（缺省=manual），不能因此漏扫。"""
        with tempfile.TemporaryDirectory() as td:
            self._write_wl(td, {"600001": {"name": "X", "status": "monitoring"}})
            pb.run_position_scan(date_str="2026-09-21", scan_type="manual",
                                 silent=True, no_feishu=True)
        self.assertEqual(self.scanned, ["600001"])

    def test_archived_and_nonexample_filtered(self):
        """archived 停用股与 _example 占位不吃扫描范围（原有语义保留）。"""
        with tempfile.TemporaryDirectory() as td:
            self._write_wl(td, {
                "600002": {"name": "在扫", "status": "monitoring"},
                "600003": {"name": "已停用", "status": "archived"},
                "_example": {"name": "示例", "status": "monitoring"},
            })
            pb.run_position_scan(date_str="2026-09-21", scan_type="manual",
                                 silent=True, no_feishu=True)
        self.assertIn("600002", self.scanned)
        self.assertNotIn("600003", self.scanned, "archived 不该扫")
        self.assertNotIn("_example", self.scanned, "占位符不该扫")


if __name__ == "__main__":
    unittest.main(verbosity=2)
