# -*- coding: utf-8 -*-
"""飞书「只播日线多头」闸（core/stage_gate，2026-11-04 owner）。

钉住三件事：
  ① 多头放行、震荡/空头拦下（口径 = 日线三态，MA60 20 日变化率 ±3%）；
  ② 缓存按 (code, mtime) 复用，文件更新后会重算；
  ③ 判不了（无缓存 / 日线不足）⇒ **fail-open** 放行（宁可多一条，不吞真告警）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import stage_gate  # noqa: E402


def _rows(n=160, start=40.0, drift=0.0):
    out, px = [], start
    for i in range(n):
        px += drift
        out.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": px, "high": px + 0.5, "low": px - 0.5,
                    "close": px, "volume": 1e5})
    return out


class TestStageGate(unittest.TestCase):
    def setUp(self):
        self._saved_dir = stage_gate.DAILY_DIR
        self._tmp = Path(tempfile.mkdtemp(prefix="stage_gate_"))
        stage_gate.DAILY_DIR = self._tmp
        stage_gate.clear_cache()
        # 多头（急涨）/ 震荡（恒定价）/ 空头（急跌）
        (self._tmp / "000001.json").write_text(json.dumps({"rows": _rows(160, 40.0, +0.30)}), encoding="utf-8")
        (self._tmp / "000002.json").write_text(json.dumps({"rows": _rows(160, 40.0, 0.0)}), encoding="utf-8")
        (self._tmp / "000003.json").write_text(json.dumps({"rows": _rows(160, 100.0, -0.30)}), encoding="utf-8")
        (self._tmp / "000004.json").write_text(json.dumps({"rows": _rows(70, 40.0, +0.30)}), encoding="utf-8")

    def tearDown(self):
        stage_gate.DAILY_DIR = self._saved_dir
        stage_gate.clear_cache()

    def test_三态判定(self):
        self.assertEqual(stage_gate.stage_of("000001"), "多头")
        self.assertEqual(stage_gate.stage_of("000002"), "震荡")
        self.assertEqual(stage_gate.stage_of("000003"), "空头")
        self.assertTrue(stage_gate.is_bull("000001"))
        self.assertFalse(stage_gate.is_bull("000002"))
        self.assertFalse(stage_gate.is_bull("000003"))

    def test_判不了则放行(self):
        self.assertIsNone(stage_gate.stage_of("999999"))       # 无缓存
        self.assertIsNone(stage_gate.stage_of("000004"))       # 70 根 < 81，MA60 20日前值 NaN
        self.assertTrue(stage_gate.is_bull("999999"))
        self.assertTrue(stage_gate.is_bull("000004"))

    def test_代码后缀归一(self):
        self.assertEqual(stage_gate.stage_of("000001.SZ"), "多头")
        self.assertEqual(stage_gate.stage_of("000002_SH"), "震荡")

    def test_filter_bull(self):
        items = [{"code": "000001"}, {"code": "000002"}, {"code": "000003"}, {"code": "999999"}]
        kept, dropped = stage_gate.filter_bull(items)
        self.assertEqual([x["code"] for x in kept], ["000001", "999999"])
        self.assertEqual([x["code"] for x in dropped], ["000002", "000003"])

    def test_与生产stage_daily逐点一致(self):
        """快捷算法不能和生产真源漂移（阈值常量也一起钉）。"""
        import pandas as pd
        from core.position_structure import stage_daily, MA60_FLAT_PCT
        self.assertEqual(stage_gate.MA60_FLAT_PCT, MA60_FLAT_PCT)
        for n, start, drift in ((120, 40, 0.30), (160, 40, 0.02), (160, 100, -0.30),
                                (160, 40, 0.0), (100, 50, 0.15), (80, 40, 0.30)):
            rows = _rows(n, start, drift)
            want = stage_daily(pd.DataFrame(rows)).get("stage_daily")
            got = stage_gate._stage_of_closes([r["close"] for r in rows])
            self.assertEqual(got, want, f"n={n} start={start} drift={drift}: 快捷={got} 生产={want}")

    def test_缓存随文件更新(self):
        self.assertEqual(stage_gate.stage_of("000002"), "震荡")
        # 覆盖成急涨 ⇒ mtime 变 ⇒ 必须重算成多头
        (self._tmp / "000002.json").write_text(
            json.dumps({"rows": _rows(160, 40.0, +0.30)}), encoding="utf-8")
        self.assertEqual(stage_gate.stage_of("000002"), "多头")


if __name__ == "__main__":
    unittest.main()
