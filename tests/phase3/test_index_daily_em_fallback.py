# -*- coding: utf-8 -*-
"""指数日线「东财兜底」单测（2026-11-18，全离线）。

背景（owner 截图报错）：「盘中状态 → 成交量图不可用：指数日线不可得(sh000001)」。
成因：腾讯 `fqkline`（ifzq / web.ifzq 两主机）被 WAF 统一回 **HTTP 501**，而 GM 终端间歇性
`1001 无法连接到终端服务` ⇒ 指数日线两个源同时不可得。东财 `push2his` 仍可用且**带真实成交额**
（上证单腿 ~6.5e11 元，能过 `_hs_daily_from_frames` 的 1e10 量级校验）。

钉住两条契约：
  A. `em_index.index_daily_em` 能把东财 klines 解析成 [date/open/high/low/close/volume/amount]；
  B. `MarketDataFacade.index_daily` 在 GM 不可用 + 腾讯返回空帧时，落到东财并标 source=eastmoney。

运行：python -m pytest tests/phase3/test_index_daily_em_fallback.py -q
"""
import sys
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__)))))

from core.market_data import em_index  # noqa: E402
from core.market_data.facade import MarketDataFacade  # noqa: E402


class _FakeResp:
    status_code = 200

    def __init__(self, payload: str):
        self.content = payload.encode("utf-8")
        self.text = payload


_EM_JSON = ('{"data":{"klines":["2026-11-17,3580.00,3590.00,3595.00,3570.00,410000000,648000000000.00",'
            '"2026-11-18,3596.55,3587.83,3596.55,3560.45,423934832,650231688071.30"]}}')


class TestEmIndexParse(unittest.TestCase):
    def test_parse_klines_to_frame(self):
        with mock.patch.object(em_index.requests, "get", return_value=_FakeResp(_EM_JSON)):
            df = em_index.index_daily_em("sh000001", days=10, end_date="2026-11-18")
        self.assertEqual(list(df.columns), em_index._COLS)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["date"].iloc[-1], "2026-11-18")
        self.assertAlmostEqual(df["amount"].iloc[-1], 650231688071.30, places=2)
        self.assertEqual(df.attrs.get("source"), "eastmoney")

    def test_bad_symbol_or_failure_returns_empty(self):
        self.assertTrue(em_index.index_daily_em("000001").empty)
        with mock.patch.object(em_index.requests, "get", side_effect=RuntimeError("boom")):
            self.assertTrue(em_index.index_daily_em("sh000001").empty)


class TestFacadeFallback(unittest.TestCase):
    def test_gm_down_tencent_empty_falls_to_em(self):
        prov = MarketDataFacade()
        em_df = pd.DataFrame([{"date": "2026-11-18", "open": 1, "high": 2, "low": 0.5,
                               "close": 1.5, "volume": 1e8, "amount": 6.5e11}],
                             columns=em_index._COLS)
        with mock.patch.object(prov, "_gm_ok", return_value=False), \
                mock.patch.object(prov._tx, "index_daily",
                                  return_value=pd.DataFrame(columns=["date", "amount"])), \
                mock.patch.object(em_index, "index_daily_em", return_value=em_df):
            df = prov.index_daily("sh000001", days=10, end_date="2026-11-18")
        self.assertEqual(df.attrs.get("source"), "eastmoney")
        self.assertEqual(len(df), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
