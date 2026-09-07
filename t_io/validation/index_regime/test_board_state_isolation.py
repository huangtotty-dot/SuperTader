# -*- coding: utf-8 -*-
"""analysis/index_regime.py C0 分板单测（2026-09-07）：index_code 参数 + 每板 state/缓存隔离。

验收：index_code=None（缺省）→ 市场（key=state.json / 缓存 key "mode:target"，行为不变）；
index_code="sz399006" → 走独立 state 文件与 symbol 维缓存，并把 sym 传入 _detect_inner。
全离线：monkeypatch _detect_inner 防网络；不做真实行情判定。
pytest / unittest 均可运行：python t_io/validation/index_regime/test_board_state_isolation.py
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import analysis.index_regime as ir  # noqa: E402


class TestStatePathIsolation(unittest.TestCase):
    def test_market_vs_board_paths(self):
        self.assertEqual(ir._ir_state_path("X"), os.path.join("X", "state.json"))
        self.assertEqual(ir._ir_state_path("X", "sz399006"), os.path.join("X", "state_sz399006.json"))
        self.assertNotEqual(ir._ir_state_path("X"), ir._ir_state_path("X", "sh000688"))

    def test_detect_signature_has_index_code(self):
        import inspect
        self.assertIn("index_code", str(inspect.signature(ir.detect_index_regime)))


class TestDetectPassesSymbol(unittest.TestCase):
    """monkeypatch _detect_inner 记录 sym 维度 + 验证缓存键隔离（全程无网络）。"""

    def _fake_inner(self, target, state_dir, p, mode, index_code=None):
        self._seen.append((mode, index_code))
        return ir.IndexRegime.RANGE, 0.0, {"date": target, "mode": mode,
                                           "index_symbol": index_code or "sh000001",
                                           "degraded": [], "regime": ir.IndexRegime.RANGE.value}

    def setUp(self):
        self._seen = []
        self._eng = ir._ir_get_engine()
        ir._IR_MEM_CACHE.clear()
        self._patcher = mock.patch.object(self._eng, "_detect_inner",
                                          side_effect=self._fake_inner)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        ir._IR_MEM_CACHE.clear()

    def test_board_passes_sym(self):
        r, s, ctx = ir.detect_index_regime(as_of="2026-09-04", force=True, mode="eod",
                                           index_code="sz399006")
        self.assertEqual(self._seen[-1][1], "sz399006")       # sym 传入引擎
        self.assertEqual(ctx.get("index_symbol"), "sz399006")

    def test_market_passes_none(self):
        ir.detect_index_regime(as_of="2026-09-04", force=True, mode="eod")
        self.assertIn(( "eod", None), self._seen)             # 缺省 → None（市场，行为不变）

    def test_cache_key_board_isolation(self):
        ir.detect_index_regime(as_of="2026-09-04", force=True, mode="eod", index_code="sz399006")
        ir.detect_index_regime(as_of="2026-09-04", force=True, mode="eod", index_code="sh000688")
        ir.detect_index_regime(as_of="2026-09-04", force=True, mode="eod")   # 市场
        keys = list(ir._IR_MEM_CACHE.keys())
        self.assertIn("eod:2026-09-04", keys)                 # 市场 key 保持旧格式
        self.assertIn("sz399006:eod:2026-09-04", keys)
        self.assertIn("sh000688:eod:2026-09-04", keys)
        self.assertEqual(len(keys), 3)                        # 三者互不串缓存


if __name__ == "__main__":
    unittest.main(verbosity=2)
