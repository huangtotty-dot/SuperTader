# -*- coding: utf-8 -*-
"""core/timing_gate.py A-2 分板验收单测（2026-09-07）：时机闸按个股所属板取对应指数。

验收口径（方案 A-2）：
  600xxx → 上证 sh000001；300xxx → 创业板 sz399006；
  688/588 → 科创50 sh000688；002/00x → 深成 sz399001；
  4 指数各走各的缓存/陈旧标记（_STALE 按 symbol 键控，非全局单标量）；
  timing_verdict 返回 index dict 增 index_code/index_name（加键不改结构）。
pytest / unittest 均可运行：python t_io/validation/timing_gate/test_timing_gate_board.py
"""
import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.timing_gate as tg  # noqa: E402


def _mk_index_trend_up(start="2025-06-02"):
    """平稳缓涨 90 天 → close>MA60×1.005 → trend_up。MA60≈10.2。"""
    closes = [10.0 + 0.005 * i for i in range(90)]
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "close": closes})


class FakeFetcher:
    """按 symbol 返回合成日线；sz399006 标记陈旧（验证 _STALE 按 symbol 键控互不污染）。"""

    def __init__(self):
        self.requested = []
        self.stale_syms = {"sz399006"}
        self.frames = {"sh000001": _mk_index_trend_up(),
                       "sz399001": _mk_index_trend_up(),
                       "sz399006": _mk_index_trend_up(),
                       "sh000688": _mk_index_trend_up()}

    def __call__(self, symbol="sh000001"):
        self.requested.append(symbol)
        tg._STALE[symbol] = False
        df = self.frames[symbol]
        if df is None or df.empty:
            tg._STALE[symbol] = True
            raise ValueError(f"{symbol} 获取失败（stub）")
        if symbol in self.stale_syms:
            tg._STALE[symbol] = True
        return df


class TestRegimeBoardResolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_fetch = tg._fetch_index_daily
        cls._orig_stale = dict(tg._STALE)
        tg._STALE.clear()
        tg._fetch_index_daily = FakeFetcher()

    @classmethod
    def tearDownClass(cls):
        tg._fetch_index_daily = cls._orig_fetch
        tg._STALE.clear()
        tg._STALE.update(cls._orig_stale)

    def _fetch(self):
        return tg._fetch_index_daily

    def test_board_index_mapping(self):
        # 方案 A-2 验收主项：各板代码落到对应指数
        cases = {  # code: (index_code, index_name)
            "600519": ("sh000001", "上证指数"),
            "688981": ("sh000688", "科创50"),
            "588170": ("sh000688", "科创50"),
            "300750": ("sz399006", "创业板指"),
            "002451": ("sz399001", "深证成指"),
            "000988": ("sz399001", "深证成指"),
        }
        for code, (ec, en) in cases.items():
            r = tg._regime(code, "2026-09-01")
            self.assertEqual(r["index_code"], ec, f"{code} 板块指数错误")
            self.assertEqual(r["index_name"], en, f"{code} 板块指数名错误")
            self.assertIn(r["regime"], ("trend_up", "trend_dn", "range", "unknown"))

    def test_fetch_requests_per_board_symbol(self):
        # 4 板块各请求各的指数（600 走上证、30 走创业板……）
        tg._regime("600519", "2026-09-01")
        tg._regime("300750", "2026-09-01")
        tg._regime("688981", "2026-09-01")
        tg._regime("002451", "2026-09-01")
        req = tg._fetch_index_daily.requested[-4:]
        self.assertEqual(set(req), {"sh000001", "sz399006", "sh000688", "sz399001"})

    def test_stale_keyed_per_symbol(self):
        # sz399006 陈旧仅标自身；sh000001 不受污染（_STALE 不再全局单标量）
        tg._STALE.clear()
        tg._regime("300750", "2026-09-01")   # sz399006 → stale True
        tg._regime("600519", "2026-09-01")   # sh000001 → stale False
        self.assertTrue(tg._STALE.get("sz399006"))
        self.assertFalse(tg._STALE.get("sh000001"))

    def test_timing_verdict_index_keys(self):
        # timing_verdict index dict 增 index_code/index_name（加键不改旧键）
        tg._stock_features = lambda code, date_str: {  # noqa: E731
            "price": 10.0, "trend_multihead": True, "drawdown": -0.01,
            "macd_golden_5d": False, "rsi": 45.0, "ma20": 9.9, "ma60": 9.8}
        tg._STALE.clear()
        v = tg.timing_verdict("300750", "2026-09-01")
        idx = v.get("index") or {}
        self.assertEqual(idx.get("index_code"), "sz399006")
        self.assertEqual(idx.get("index_name"), "创业板指")
        # 旧键仍在（兼容：GUI/卡点读 close/up_line/dn_line）
        self.assertIn("close", idx)
        self.assertIn("up_line", idx)
        self.assertIn("dn_line", idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
