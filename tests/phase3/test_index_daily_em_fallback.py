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

    @staticmethod
    def _row(date="2026-11-18", close=3587.83, volume=423934832, amount=650231688071.30):
        return {"date": date, "open": close - 1, "high": close + 2, "low": close - 3,
                "close": close, "volume": volume, "amount": amount}

    def test_stale_nonempty_tencent_falls_to_em_in_strict_mode(self):
        prov = MarketDataFacade()
        prov._gm = mock.Mock()  # 避免参数求值时访问 None.index_daily
        stale = pd.DataFrame([self._row(date="2026-11-17")])
        fresh = pd.DataFrame([self._row()])
        with mock.patch.object(prov, "_gm_ok", return_value=False), \
                mock.patch.object(prov._tx, "index_daily", return_value=stale), \
                mock.patch.object(em_index, "index_daily_em", return_value=fresh) as em:
            df = prov.index_daily("sh000300", days=10, end_date="2026-11-18", require_date=True)
        em.assert_called_once()
        self.assertEqual(df.attrs.get("source"), "eastmoney")
        self.assertEqual(df["date"].iloc[-1], "2026-11-18")

    def test_zero_volume_placeholder_falls_to_em(self):
        prov = MarketDataFacade()
        placeholder = pd.DataFrame([self._row(close=2189.82, volume=0, amount=0)])
        placeholder[["open", "high", "low"]] = 2189.82
        fresh = pd.DataFrame([self._row()])
        with mock.patch.object(prov, "_gm_ok", return_value=False), \
                mock.patch.object(prov._tx, "index_daily", return_value=placeholder), \
                mock.patch.object(em_index, "index_daily_em", return_value=fresh):
            df = prov.index_daily("sz399006", days=10, end_date="2026-11-18", require_date=True)
        self.assertEqual(df.attrs.get("source"), "eastmoney")

    def test_gm_missing_target_cannot_short_circuit_backup(self):
        prov = MarketDataFacade()
        prov._gm = mock.Mock()  # 避免参数求值时访问 None.index_daily
        stale = pd.DataFrame([self._row(date="2026-11-17")])
        fresh = pd.DataFrame([self._row()])
        with mock.patch.object(prov, "_gm_ok", return_value=True), \
                mock.patch.object(prov, "_gm_call", return_value=stale), \
                mock.patch.object(prov, "_gm_ok_reset"), \
                mock.patch.object(prov._tx, "index_daily",
                                  return_value=pd.DataFrame(columns=em_index._COLS)), \
                mock.patch.object(em_index, "index_daily_em", return_value=fresh) as em:
            df = prov.index_daily("sh000300", days=10, end_date="2026-11-18", require_date=True)
        em.assert_called_once()
        self.assertEqual(df.attrs.get("source"), "eastmoney")

    def test_non_strict_historical_query_keeps_gm_previous_day(self):
        """昨收/历史接口允许自然停在上一交易日，不被严格目标日语义误伤。"""
        prov = MarketDataFacade()
        prov._gm = mock.Mock()
        previous = pd.DataFrame([self._row(date="2026-11-17")])
        with mock.patch.object(prov, "_gm_ok", return_value=True), \
                mock.patch.object(prov, "_gm_call", return_value=previous), \
                mock.patch.object(prov, "_gm_ok_reset"), \
                mock.patch.object(em_index, "index_daily_em") as em:
            df = prov.index_daily("sh000300", days=10, end_date="2026-11-18")
        em.assert_not_called()
        self.assertEqual(df.attrs.get("source"), "gm")


class TestIndexReturnIntegrity(unittest.TestCase):
    def test_zero_volume_placeholder_never_produces_alpha_benchmark(self):
        from scripts.equity_daily import index_ret
        placeholder = pd.DataFrame([
            {"date": "2026-11-26", "open": 2100, "high": 2120, "low": 2090,
             "close": 2110, "volume": 1e8, "amount": 2e11},
            {"date": "2026-11-27", "open": 2189.82, "high": 2189.82,
             "low": 2189.82, "close": 2189.82, "volume": 0, "amount": 0},
        ])
        ret, note = index_ret(placeholder, "2026-11-27")
        self.assertIsNone(ret)
        self.assertIn("benchmark_unverified", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
