# -*- coding: utf-8 -*-
"""gm_bridge/watcher.py 仓位告警改用账户真源（㉜ 修复）单测 —— 2026-11-13。**全离线**。

## 假告警是什么

watcher 的「仓位超限」告警原先用**本地估计仓**（初始现金 20 万 + 仅本进程
可见的成交）算仓位占比 ⇒ 2026-11-13 误报「佳驰科技市值占比 96% > 80%」，
而账户真源口径下该票约占 24%、总仓位约 71.8%。

## 修法钉什么

`_account_truth_today()`：读 BRIDGE_DIR 下今日 heartbeat 尾行
（含真实 cash 与全量持仓）⇒ (cash, {code: {qty,cost}})；文件缺失/读坏 ⇒ None。
`_track_position` 的告警改按真源算**账户总仓位**（Σqty×cost / (cash+Σqty×cost)）；
真源不可用 ⇒ 不告警（宁可缺一条，不可再发误导性假告警）；消息文案含「账户总仓位」。

⚠️ **刻意不 import watcher**：watcher 是长驻进程。本测试按标记从源码切出
「旁路风控」那一段再 exec —— 测真源码、零副作用（同 test_bridge_push_label 惯例）。

运行：python -m unittest tests.phase3.test_watcher_position_alarm_truth -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Dict

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_WATCHER_FP = _ROOT / "execution" / "auto" / "_gm" / "gm_bridge" / "watcher.py"
_SRC = _WATCHER_FP.read_text(encoding="utf-8")
_A = _SRC.index("# ── 旁路风控 ──")
_B = _SRC.index("# ── 心跳监控 ──")

# 11-13 真实账户快照（heartbeat 尾行口径）
_REAL_POSITIONS = {"SHSE.600629": {"qty": 6400, "cost": 15.5477},     # 99,505.28
                   "SZSE.300534": {"qty": 8200, "cost": 11.2471},     # 92,226.22
                   "SHSE.688708": {"qty": 2500, "cost": 36.7948}}     # 91,987.00
_REAL_MV = 6400 * 15.5477 + 8200 * 11.2471 + 2500 * 36.7948           # ≈ 283,718.5


class TestPositionAlarmTruth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="watcher_truth_")
        self.addCleanup(self._cleanup)
        self.pushes = []
        ns = {"os": os, "json": json, "time": time, "datetime": datetime,
              "Dict": Dict, "BRIDGE_DIR": self._tmp,
              "_push": lambda *a: self.pushes.append(a),
              "_stock_label": lambda code: code}
        exec(compile(_SRC[_A:_B], "watcher.py:risk_block", "exec"), ns)
        self.ns = ns

    def _cleanup(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_heartbeat(self, rows):
        fp = os.path.join(self._tmp,
                          "heartbeat_%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
        with open(fp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _buy_fill(self, code="688708"):
        self.ns["_track_position"]({"event": "fill", "code": code,
                                    "side": "BUY", "qty": 100, "price": 37.0})

    def test_false_alarm_suppressed_with_account_truth(self):
        """11-13 场景复现：真源总仓位 ≈71.8% < 80% ⇒ 不得告警（本地估计仓曾误报 96%）。"""
        # 首行：现金被掏空口径（若误用首行会告警 96.6%）⇒ 同时钉住「取尾行」语义
        self._write_heartbeat([
            {"cash": 10000.0, "positions": _REAL_POSITIONS},
            {"cash": 111203.22, "positions": _REAL_POSITIONS},
        ])
        self._buy_fill()
        self.assertEqual(self.pushes, [],
                         "真源口径总仓位 %.1f%% < 80%%，告警必须静默"
                         % (100 * _REAL_MV / (111203.22 + _REAL_MV)))

    def test_real_over_limit_still_alerts(self):
        """真源总仓位 > 80% ⇒ 告警照常发出，文案含「账户总仓位」与触发票代码。"""
        self._write_heartbeat([{"cash": 50000.0, "positions": _REAL_POSITIONS}])
        self._buy_fill()
        self.assertEqual(len(self.pushes), 1, "真超限必须告警")
        title, msg, color = self.pushes[0]
        self.assertEqual(title, "⚠️ 仓位超限")
        self.assertIn("账户总仓位", msg)
        self.assertIn("688708", msg)
        self.assertEqual(color, "orange")

    def test_no_heartbeat_no_alarm_no_crash(self):
        """heartbeat 缺失 ⇒ 真源不可用 ⇒ 不告警、不抛错（fail-silent，宁缺毋滥）。"""
        self._buy_fill()
        self.assertEqual(self.pushes, [])

    def test_malformed_last_line_no_alarm_no_crash(self):
        """heartbeat 尾行损坏 ⇒ 真源不可用 ⇒ 不告警、不抛错。"""
        fp = os.path.join(self._tmp,
                          "heartbeat_%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
        with open(fp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"cash": 50000.0, "positions": _REAL_POSITIONS}) + "\n")
            f.write("{bad json line\n")
        self._buy_fill()
        self.assertEqual(self.pushes, [])

    def test_empty_positions_low_ratio_no_alarm(self):
        """真源无持仓（空仓）⇒ 0% < 80% ⇒ 不告警。"""
        self._write_heartbeat([{"cash": 400000.0, "positions": {}}])
        self._buy_fill()
        self.assertEqual(self.pushes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
