# -*- coding: utf-8 -*-
"""建仓信号扫描「整行底色 = 站上5日线绿 / 破5日线红」的补齐（2026-10-24 owner：
「建仓信号扫描站上5日线应该显示绿色，破了5日线应该显示红色，结果现在都是白色」）。

根因两条，都是 `timing_features` 缺失 → 前端 `_above5` 为 undefined → 不着色：
  ① 历史日 / 旧 trace 里根本没有这个字段；
  ② **那天压根没扫过**（`traces/position_builder_{date}.jsonl` 不存在）⇒ 全是"等待扫描"行。
两条都用**该日期的本地日线缓存**补 `above_ma5` / `above_ma20`（离线、带 mtime 缓存）。
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402


class TestBackfill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = t_gui.Api()
        cls._cache = dict(t_gui.Api._PB_TF_CACHE)

    @classmethod
    def tearDownClass(cls):
        t_gui.Api._PB_TF_CACHE.clear()
        t_gui.Api._PB_TF_CACHE.update(cls._cache)

    def test_缺特征的行按该日补出布尔值(self):
        rows = [{"code": "000988"}]                      # trace 里没有 timing_features
        t_gui.Api._backfill_timing_features(rows, "2026-04-08")
        tf = rows[0].get("timing_features") or {}
        self.assertIsInstance(tf.get("above_ma5"), bool, "必须补出 True/False，前端才有着色依据")
        self.assertIsInstance(tf.get("above_ma20"), bool)

    def test_已有的值不被覆盖(self):
        rows = [{"code": "000988", "timing_features": {"above_ma5": False, "above_ma20": False}}]
        t_gui.Api._backfill_timing_features(rows, "2026-04-08")
        self.assertIs(rows[0]["timing_features"]["above_ma5"], False, "trace 里有的值以 trace 为准")

    def test_缓存生效(self):
        t_gui.Api._PB_TF_CACHE.clear()
        rows1 = [{"code": "000988"}]
        t_gui.Api._backfill_timing_features(rows1, "2026-04-08")
        n = len(t_gui.Api._PB_TF_CACHE)
        self.assertGreater(n, 0)
        rows2 = [{"code": "000988"}]
        t_gui.Api._backfill_timing_features(rows2, "2026-04-08")
        self.assertEqual(len(t_gui.Api._PB_TF_CACHE), n, "同 (码,日期,mtime) 不该重复读盘")
        self.assertEqual(rows1[0]["timing_features"], rows2[0]["timing_features"])

    def test_没有缓存的码不会崩(self):
        rows = [{"code": "999999"}]
        t_gui.Api._backfill_timing_features(rows, "2026-04-08")   # 不存在 → 静默跳过
        self.assertTrue(rows[0].get("timing_features") is None or
                        isinstance(rows[0].get("timing_features"), dict))


if __name__ == "__main__":
    unittest.main(verbosity=2)
