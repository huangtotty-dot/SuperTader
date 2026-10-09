# -*- coding: utf-8 -*-
"""建仓信号扫描「30min 判定」列的离线单测（2026-10-09）。**全离线**。

背景：建仓表的「背离」列换成与「持仓 30min 风险体检」**同口径**的 30min 判定
（`m30_features.verdict_from_features`）。判定只用 feats+div ⇒ 走**轻量**快照
`_build_m30_light`（跳过 trend30 状态机 ~300ms），整池 ~53 只 ≈ 数秒。主线程**零计算**，
未热显示「计算中」+ `pb_m30_pending`（10s 轮询自动补）。

覆盖：
  - `_build_m30_light`：好帧→有 feats+div；空/过短→{}
  - `_m30_verdict_pb`：冷→pending；热→合法 level/label/reason，且与 verdict_from_features 同口径
  - `_build_m30_light` **比全量** `_build_m30_snapshot` **快**（证轻量口径成立）

运行：python -m unittest tests.phase3.test_pb_m30_verdict
"""
import os
import sys
import time
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402,F401
import pandas  # noqa: E402,F401
import t_gui  # noqa: E402
from analysis import m30_features as mf  # noqa: E402


def _m30_uptrend(days=45, base=10.0, step=0.01):
    rows, px = [], base
    times = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
    for d in range(days):
        day = f"2026-01-{(d % 28) + 1:02d}"
        for hm in times:
            px += step
            rows.append({"time": f"{day} {hm}:00", "open": px - 0.005, "high": px + 0.02,
                         "low": px - 0.02, "close": px, "volume": 1000.0})
    return pandas.DataFrame(rows)


class TestPBM30Verdict(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._saved = dict(t_gui._PB_M30_CACHE)
        t_gui._PB_M30_CACHE.clear()

    def tearDown(self):
        t_gui._PB_M30_CACHE.clear()
        t_gui._PB_M30_CACHE.update(self._saved)

    def test_01_light_snapshot(self):
        snap = t_gui._build_m30_light(_m30_uptrend())
        self.assertTrue(snap, "好帧应产出轻量快照")
        self.assertIn("feats", snap)
        self.assertIn("div", snap)
        self.assertNotIn("t30", snap, "轻量快照不应含 trend30（那是 300ms 的那部分）")

    def test_02_light_snapshot_empty_or_short(self):
        self.assertEqual(t_gui._build_m30_light(pandas.DataFrame()), {})
        self.assertEqual(t_gui._build_m30_light(_m30_uptrend().head(20)), {})

    def test_03_cold_is_pending(self):
        v = self.api._m30_verdict_pb("000001")
        self.assertEqual(v["level"], "pending")
        self.assertIn("计算中", v["label"])

    def test_04_warm_verdict_matches_logic(self):
        snap = t_gui._build_m30_light(_m30_uptrend())
        t_gui._PB_M30_CACHE[f"000001_{t_gui._min_bars_slot()}"] = snap
        v = self.api._m30_verdict_pb("000001")
        self.assertIn(v["level"], ("high", "watch", "bull", "none", "na"))
        self.assertTrue(v["label"] and v["reason"])
        # 与「同口径」直算一致（判定不依赖 trend）
        _dv = snap.get("div") or {}
        ref = mf.verdict_from_features(snap["feats"], None, _dv.get("type"), _dv.get("bars_ago"))
        self.assertEqual((v["level"], v["label"], v["reason"]), ref)

    def test_05_light_faster_than_full(self):
        df = _m30_uptrend()
        t_gui._build_m30_light(df)              # 预热 import
        t = time.perf_counter(); t_gui._build_m30_light(df); light = time.perf_counter() - t
        t = time.perf_counter(); t_gui._build_m30_snapshot(df); full = time.perf_counter() - t
        self.assertLess(light, full, f"轻量({light*1000:.0f}ms) 应快于全量({full*1000:.0f}ms)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
