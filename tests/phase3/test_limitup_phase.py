# -*- coding: utf-8 -*-
"""涨停股「阶段」分类 `core/limitup_phase.py`（2026-11-15 owner：异常监控池按时期分类 + 找介入点）。

纯函数，只测八类阶段 + ST 阈值 + PHASE_ORDER + 池表接线（`limitup_phase` 字段 + 按阶段分组排序）。
判词：doc/research/2026-11-15_涨停股介入时机与阶段分类.md

运行：python -m unittest tests.phase3.test_limitup_phase -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.limitup_phase import PHASE_ORDER, limitup_phase  # noqa: E402
import gui  # noqa: E402


def _bar(date, close, high=None):
    return {"date": date, "open": close, "close": close,
            "high": close if high is None else high,
            "low": close, "volume": 0}


def _rows(pairs):
    """pairs: [(date, close)] 升序；high 默认 = close（封板）。"""
    return [_bar(d, c) for d, c in pairs]


class TestPhasePure(unittest.TestCase):
    def test_无数据(self):
        self.assertEqual(limitup_phase([], "2026-11-13", code="600000"),
                         {"phase": "无数据", "board": 0, "gap": None, "rebound": False})

    def test_连板中_二板(self):
        rows = _rows([("2026-11-11", 10.0), ("2026-11-12", 11.0), ("2026-11-13", 12.10)])
        r = limitup_phase(rows, "2026-11-13", code="600000")
        self.assertEqual((r["phase"], r["board"], r["gap"], r["rebound"]), ("连板中", 2, 0, False))

    def test_连板中_三板(self):
        rows = _rows([("2026-11-11", 10.0), ("2026-11-12", 11.0),
                      ("2026-11-13", 12.10), ("2026-11-14", 13.31)])
        r = limitup_phase(rows, "2026-11-14", code="600000")
        self.assertEqual((r["phase"], r["board"]), ("连板中", 3))

    def test_今日首板(self):
        rows = _rows([("2026-11-12", 10.0), ("2026-11-13", 10.20), ("2026-11-14", 11.22)])
        r = limitup_phase(rows, "2026-11-14", code="600000")
        self.assertEqual((r["phase"], r["board"], r["gap"], r["rebound"]), ("今日首板", 1, 0, False))

    def test_反包(self):
        # 11-12 涨停 → 11-13 断板 → 11-14 再次涨停（昨日非涨停，但近期有涨停）⇒ 反包
        rows = _rows([("2026-11-11", 10.0), ("2026-11-12", 11.0),
                      ("2026-11-13", 10.80), ("2026-11-14", 11.88)])
        r = limitup_phase(rows, "2026-11-14", code="600000")
        self.assertEqual((r["phase"], r["board"], r["gap"], r["rebound"]), ("反包", 1, 0, True))

    def test_断板D加1(self):
        rows = _rows([("2026-11-12", 10.0), ("2026-11-13", 11.0), ("2026-11-14", 10.80)])
        r = limitup_phase(rows, "2026-11-14", code="600000")
        self.assertEqual((r["phase"], r["board"], r["gap"], r["rebound"]), ("断板D+1", 1, 1, False))

    def test_断板回踩_gap2(self):
        rows = _rows([("2026-11-11", 10.0), ("2026-11-12", 11.0),
                      ("2026-11-13", 10.80), ("2026-11-14", 10.90)])
        r = limitup_phase(rows, "2026-11-14", code="600000")
        self.assertEqual((r["phase"], r["gap"]), ("断板回踩", 2))

    def test_断板回踩_gap3(self):
        rows = _rows([("2026-11-11", 10.0), ("2026-11-12", 11.0),
                      ("2026-11-13", 10.80), ("2026-11-14", 10.90), ("2026-11-15", 10.95)])
        r = limitup_phase(rows, "2026-11-15", code="600000")
        self.assertEqual((r["phase"], r["gap"]), ("断板回踩", 3))

    def test_断板走远_gap4(self):
        rows = _rows([("2026-11-10", 10.0), ("2026-11-11", 11.0),
                      ("2026-11-12", 10.80), ("2026-11-13", 10.90),
                      ("2026-11-14", 10.95), ("2026-11-15", 11.00)])
        r = limitup_phase(rows, "2026-11-15", code="600000")
        self.assertEqual((r["phase"], r["gap"]), ("断板走远", 4))

    def test_无近期涨停(self):
        rows = _rows([("2026-11-13", 10.0), ("2026-11-14", 10.10), ("2026-11-15", 10.20)])
        r = limitup_phase(rows, "2026-11-15", code="600000")
        self.assertEqual((r["phase"], r["board"], r["gap"]), ("无近期涨停", 0, None))

    def test_涨停当日为近端末行_断板后走远超出回看窗(self):
        # 涨停发生在 11 天前（超出 _REBOUND_LOOKBACK=10）⇒ 应判「无近期涨停」，不是「断板走远」
        pairs = [("2026-11-01", 10.0), ("2026-11-02", 11.0)]
        for i in range(3, 14):
            pairs.append((f"2026-11-{i:02d}", 10.90))
        rows = _rows(pairs)
        r = limitup_phase(rows, "2026-11-13", code="600000")
        self.assertEqual(r["phase"], "无近期涨停")

    def test_ST阈值_5pct算涨停(self):
        rows = _rows([("2026-11-13", 10.0), ("2026-11-14", 10.50)])
        r = limitup_phase(rows, "2026-11-14", code="600000", name="*ST某某")
        self.assertEqual(r["phase"], "今日首板")

    def test_主板5pct不算涨停(self):
        # 非 ST 主板阈值 9.5%，+5% 不够 ⇒ 无近期涨停
        rows = _rows([("2026-11-13", 10.0), ("2026-11-14", 10.50)])
        r = limitup_phase(rows, "2026-11-14", code="600000", name="")
        self.assertEqual(r["phase"], "无近期涨停")

    def test_未封板不算涨停(self):
        # 涨幅达标但收盘没封板（high > close）⇒ 不算涨停
        rows = [{"date": "2026-11-13", "open": 10.0, "close": 11.0, "high": 11.5, "low": 10.0, "volume": 0},
                {"date": "2026-11-14", "open": 10.0, "close": 10.50, "high": 10.60, "low": 10.0, "volume": 0}]
        r = limitup_phase(rows, "2026-11-14", code="600000")
        self.assertEqual(r["phase"], "无近期涨停")

    def test_缓存停在更早日_以最后一行作今日(self):
        # 目标日 11-15 缺行，缓存最后一行是 11-13（涨停）⇒ 以 11-13 作「今日」
        rows = _rows([("2026-11-12", 10.0), ("2026-11-13", 11.0)])
        r = limitup_phase(rows, "2026-11-15", code="600000")
        self.assertEqual(r["phase"], "今日首板")


class TestPhaseOrder(unittest.TestCase):
    def test_阶段权重单调(self):
        self.assertEqual(PHASE_ORDER["连板中"], 0)
        self.assertEqual(PHASE_ORDER["反包"], 1)
        self.assertEqual(PHASE_ORDER["今日首板"], 2)
        self.assertEqual(PHASE_ORDER["断板D+1"], 3)
        self.assertEqual(PHASE_ORDER["断板回踩"], 4)
        self.assertEqual(PHASE_ORDER["断板走远"], 5)
        self.assertEqual(PHASE_ORDER["无近期涨停"], 6)
        self.assertEqual(PHASE_ORDER["无数据"], 7)
        self.assertLess(PHASE_ORDER["连板中"], PHASE_ORDER["断板D+1"])


class TestPoolWiring(unittest.TestCase):
    """池表接线：`_pool_rows` 每行带 `limitup_phase`，排序按阶段分组（连板中 在 断板 前）。"""

    def _api(self, tmp):
        api = gui.Api()
        # 禁网：conformance / 标签 / MA 回站全打空，只留阶段分类读本地日线缓存
        api._pool_conformance_budgeted = lambda codes, date, budget=24: (list(codes), 0)
        api._hunter_build_conformance = lambda codes, date=None: {}
        api.load_stock_tags_batch = lambda *a, **k: {"tags": {}}
        api._attach_ma_reclaim = lambda rows, date=None: None
        gui._DAILY_KLINE_DIR = tmp
        gui._concept_names = lambda: {"600000": "浦发银行", "600001": "平安银行"}
        return api

    def test_每行带阶段字段且按阶段分组排序(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "600000.json").write_text(json.dumps({"rows": _rows([
                ("2026-11-11", 10.0), ("2026-11-12", 11.0), ("2026-11-13", 12.10)])}),
                encoding="utf-8")
            (tmp / "600001.json").write_text(json.dumps({"rows": _rows([
                ("2026-11-11", 20.0), ("2026-11-12", 22.0), ("2026-11-13", 21.0)])}),
                encoding="utf-8")
            old_dir, old_names = gui._DAILY_KLINE_DIR, gui._concept_names
            try:
                api = self._api(tmp)
                stocks = {
                    "600000": {"name": "浦发银行", "limit_board": 2, "limit_date": "2026-11-13"},
                    "600001": {"name": "平安银行", "limit_board": 1, "limit_date": "2026-11-12"},
                }
                rows = api._pool_rows(stocks, "2026-11-13", "")["rows"]
                self.assertEqual([r["code"] for r in rows], ["600000", "600001"],
                                 "连板中(权重0)要排在断板D+1(权重3)前面")
                self.assertEqual(rows[0]["limitup_phase"]["phase"], "连板中")
                self.assertEqual(rows[1]["limitup_phase"]["phase"], "断板D+1")
            finally:
                gui._DAILY_KLINE_DIR, gui._concept_names = old_dir, old_names

    def test_无缓存票阶段兜底为无数据(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old_dir, old_names = gui._DAILY_KLINE_DIR, gui._concept_names
            try:
                api = self._api(tmp)
                stocks = {"999999": {"name": "不存在", "limit_board": 1, "limit_date": "2026-11-13"}}
                rows = api._pool_rows(stocks, "2026-11-13", "")["rows"]
                self.assertEqual(rows[0]["limitup_phase"]["phase"], "无数据")
            finally:
                gui._DAILY_KLINE_DIR, gui._concept_names = old_dir, old_names


if __name__ == "__main__":
    unittest.main(verbosity=2)
