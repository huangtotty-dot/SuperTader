# -*- coding: utf-8 -*-
"""掘金数据源单测 —— core/market_data/gm_provider.py（2026-11-14 补）。

按「传输边界 mock」原则：**不 import 真 gm SDK、不发网络**，用 `object.__new__` 绕过
`__init__`（其内 `import gm.api`），注入一个假 `_gma`，返回**真实形状**的 DataFrame/字典，
断言 GmProvider 把它转成正确结果。钉死的历史易错契约：

① 股→手收敛：所有 volume ÷100（daily/minute/index_daily/index_minute/snapshot/index_snapshot 六处统一）；
② `_gm_index_symbol` 前缀路由（sh/sz → SHSE./SZSE.，已是 GM 格式原样返回）；
③ `daily_batch` 走**窗口式** `history` 收列表（不是 `history_n`），symbol 列拆回裸码 + 账户后缀剥离；
④ `minute`/`index_minute` 只保留目标日当日 bar（count=240 会回溢前一日）+ 时间戳去 tz；
⑤ `snapshot`/`index_snapshot` price<=0 跳过、symbol→裸码、`created_at`→日期字符串；
⑥ 空/None 响应 fail-closed 返回**空但列齐全**的 DataFrame（不回退到异常，调用方按列名消费）。

运行：python tests/phase3/test_gm_provider.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import core.market_data.gm_provider as gmp  # noqa: E402
from core.market_data.gm_provider import GmProvider, _gm_index_symbol  # noqa: E402


class _FakeGma:
    """假 gm.api：记录调用、按 test 预设返回值。ADJUST_PREV 用哨兵对象占位。"""
    ADJUST_PREV = object()

    def __init__(self):
        self.history_n_calls = []
        self.history_calls = []
        self.current_calls = []
        self.history_n_result = None
        self.history_result = None
        self.current_result = []

    def history_n(self, **kw):
        self.history_n_calls.append(kw)
        return self.history_n_result

    def history(self, **kw):
        self.history_calls.append(kw)
        return self.history_result

    def current(self, codes):
        self.current_calls.append(codes)
        return self.current_result


def _provider(fake):
    p = object.__new__(GmProvider)  # 绕过 __init__（其内 import gm.api）
    p._gma = fake
    p._ready = True
    return p


class TestGmIndexSymbol(unittest.TestCase):
    def test_sh_sz_routing(self):
        self.assertEqual(_gm_index_symbol("sh000001"), "SHSE.000001")
        self.assertEqual(_gm_index_symbol("sz399001"), "SZSE.399001")

    def test_already_gm_format_passthrough(self):
        self.assertEqual(_gm_index_symbol("SHSE.000001"), "SHSE.000001")
        self.assertEqual(_gm_index_symbol("SZSE.399001"), "SZSE.399001")

    def test_whitespace_stripped(self):
        self.assertEqual(_gm_index_symbol("  sh000001 "), "SHSE.000001")

    def test_unknown_prefix_passthrough(self):
        """北交所等非 sh/sz 前缀不在此函数职责内，原样返回。"""
        self.assertEqual(_gm_index_symbol("bj430047"), "bj430047")


class TestDaily(unittest.TestCase):
    def test_transforms_and_sorts(self):
        fake = _FakeGma()
        fake.history_n_result = pd.DataFrame({
            "eob": ["2026-01-06", "2026-01-05"],
            "open": [10.5, 10.0], "high": [10.9, 10.8],
            "low": [10.2, 9.9], "close": [10.8, 10.5],
            "volume": [200000, 100000],  # 股
        })
        out = _provider(fake).daily("600481", days=800)
        self.assertEqual(list(out.columns), ["date", "open", "high", "low", "close", "volume"])
        self.assertEqual(list(out["date"]), ["2026-01-05", "2026-01-06"])  # 已排序
        self.assertEqual(list(out["volume"]), [1000.0, 2000.0])            # ÷100 股→手

    def test_calls_history_n_with_contract(self):
        fake = _FakeGma()
        fake.history_n_result = pd.DataFrame(
            {"eob": ["2026-01-05"], "open": [10.0], "high": [10.8],
             "low": [9.9], "close": [10.5], "volume": [100000]})
        _provider(fake).daily("600481_A", days=30)
        kw = fake.history_n_calls[0]
        self.assertEqual(kw["symbol"], "SHSE.600481")  # to_gm 剥账户后缀
        self.assertEqual(kw["frequency"], "1d")
        self.assertEqual(kw["count"], 30)
        self.assertIs(kw["adjust"], _FakeGma.ADJUST_PREV)
        self.assertIs(kw["df"], True)
        self.assertEqual(kw["fields"], "eob,open,high,low,close,volume")

    def test_none_and_empty_fail_closed(self):
        for result in (None, pd.DataFrame()):
            fake = _FakeGma()
            fake.history_n_result = result
            out = _provider(fake).daily("600481")
            self.assertEqual(list(out.columns), ["date", "open", "high", "low", "close", "volume"])
            self.assertEqual(len(out), 0)


class TestDailyBatch(unittest.TestCase):
    def test_empty_codes_no_call(self):
        fake = _FakeGma()
        self.assertEqual(_provider(fake).daily_batch([]), {})
        self.assertEqual(fake.history_calls, [])

    def test_group_by_code_and_strip_suffix(self):
        fake = _FakeGma()
        fake.history_result = pd.DataFrame({
            "symbol": ["SHSE.600481", "SZSE.002639"],
            "eob": ["2026-01-05", "2026-01-05"],
            "open": [10.0, 20.0], "high": [10.8, 20.8],
            "low": [9.9, 19.9], "close": [10.5, 20.5],
            "volume": [100000, 200000],
        })
        out = _provider(fake).daily_batch(["600481_A", "002639"], days=250)
        # history 收的是 to_gm 列表（账户后缀已剥），fields 带 symbol 列
        kw = fake.history_calls[0]
        self.assertEqual(kw["symbol"], ["SHSE.600481", "SZSE.002639"])
        self.assertEqual(kw["frequency"], "1d")
        self.assertIn("symbol", kw["fields"])
        self.assertEqual(set(out.keys()), {"600481", "002639"})
        self.assertEqual(out["600481"]["volume"].iloc[0], 1000.0)
        self.assertEqual(out["002639"]["volume"].iloc[0], 2000.0)

    def test_none_fail_closed(self):
        fake = _FakeGma()
        fake.history_result = None
        self.assertEqual(_provider(fake).daily_batch(["600481"]), {})


class TestMinute(unittest.TestCase):
    def test_keeps_only_target_day_and_divides_volume(self):
        fake = _FakeGma()
        fake.history_n_result = pd.DataFrame({
            "eob": ["2026-01-06 09:31:00", "2026-01-06 09:32:00", "2026-01-05 15:00:00"],
            "open": [10.0, 10.1, 9.9], "high": [10.05, 10.15, 9.95],
            "low": [9.98, 10.08, 9.88], "close": [10.04, 10.12, 9.92],
            "volume": [10000, 12000, 8000], "amount": [1e6, 1.2e6, 0.8e6],
        })
        out = _provider(fake).minute("600481", "2026-01-06")
        self.assertEqual(list(out.columns), ["time", "open", "high", "low", "close", "volume", "amount"])
        self.assertEqual(len(out), 2)  # 前一日 15:00 根被滤掉
        self.assertEqual(list(out["volume"]), [100.0, 120.0])  # ÷100

    def test_calls_with_end_of_day_and_count(self):
        fake = _FakeGma()
        fake.history_n_result = None
        _provider(fake).minute("600481", "2026-01-06")
        kw = fake.history_n_calls[0]
        self.assertEqual(kw["symbol"], "SHSE.600481")
        self.assertEqual(kw["frequency"], "60s")
        self.assertEqual(kw["count"], 240)
        self.assertEqual(kw["end_time"], "2026-01-06 15:00:00")

    def test_none_fail_closed(self):
        fake = _FakeGma()
        fake.history_n_result = None
        out = _provider(fake).minute("600481", "2026-01-06")
        self.assertEqual(list(out.columns), ["time", "open", "high", "low", "close", "volume", "amount"])
        self.assertEqual(len(out), 0)


class TestSnapshot(unittest.TestCase):
    def test_empty_codes(self):
        self.assertEqual(_provider(_FakeGma()).snapshot([]), {})

    def test_price_filter_and_volume_divide(self):
        fake = _FakeGma()
        fake.current_result = [
            {"price": 10.5, "symbol": "SHSE.600481", "open": 10.0, "high": 10.8,
             "low": 9.9, "cum_volume": 50000, "created_at": datetime(2026, 1, 6, 15, 0, 0)},
            {"price": 0.0, "symbol": "SZSE.002639", "open": 0, "high": 0, "low": 0,
             "cum_volume": 0, "created_at": datetime(2026, 1, 6, 15, 0, 0)},
        ]
        out = _provider(fake).snapshot(["600481", "002639"])
        self.assertEqual(list(out.keys()), ["600481"])  # price<=0 被跳过
        self.assertEqual(out["600481"]["price"], 10.5)
        self.assertEqual(out["600481"]["volume"], 500.0)      # ÷100
        self.assertEqual(out["600481"]["ts_date"], "2026-01-06")


class TestIndexDaily(unittest.TestCase):
    def test_volume_divide_and_amount_kept(self):
        fake = _FakeGma()
        fake.history_n_result = pd.DataFrame({
            "eob": ["2026-01-05"],
            "open": [10.0], "high": [10.8], "low": [9.9], "close": [10.5],
            "volume": [100000], "amount": [1e9],
        })
        out = _provider(fake).index_daily("sh000001", days=800)
        self.assertEqual(list(out.columns), ["date", "open", "high", "low", "close", "volume", "amount"])
        self.assertEqual(out["volume"].iloc[0], 1000.0)
        self.assertEqual(out["amount"].iloc[0], 1e9)
        self.assertEqual(fake.history_n_calls[0]["symbol"], "SHSE.000001")

    def test_end_date_override(self):
        fake = _FakeGma()
        fake.history_n_result = None
        _provider(fake).index_daily("sh000001", end_date="2026-01-01")
        self.assertEqual(fake.history_n_calls[0]["end_time"], "2026-01-01")


class TestIndexMinute(unittest.TestCase):
    def test_default_freq_and_volume_divide(self):
        fake = _FakeGma()
        fake.history_n_result = pd.DataFrame({
            "eob": ["2026-01-06 09:35:00"],
            "open": [10.0], "high": [10.05], "low": [9.98], "close": [10.02],
            "volume": [10000], "amount": [1e6],
        })
        out = _provider(fake).index_minute("sh000001")
        kw = fake.history_n_calls[0]
        self.assertEqual(kw["frequency"], "300s")  # 默认 5 分钟
        self.assertEqual(kw["symbol"], "SHSE.000001")
        self.assertEqual(out["volume"].iloc[0], 100.0)


class TestIndexSnapshot(unittest.TestCase):
    def test_change_pct_from_pre_close(self):
        fake = _FakeGma()
        fake.current_result = [
            {"price": 10.5, "symbol": "SHSE.000001", "pre_close": 10.0, "open": 10.1,
             "high": 10.8, "low": 9.9, "cum_volume": 50000,
             "created_at": datetime(2026, 1, 6, 15, 0, 0)},
        ]
        out = _provider(fake).index_snapshot(["sh000001"])
        # 走 _gm_index_symbol，不是 to_gm（后者会把 sh000001 编成 SZSE.sh000001）
        self.assertEqual(fake.current_calls[0], ["SHSE.000001"])
        r = out["000001"]
        self.assertEqual(r["change"], 0.5)
        self.assertAlmostEqual(r["change_pct"], 5.0)
        self.assertEqual(r["volume"], 500.0)

    def test_missing_pre_close_yields_none_change(self):
        fake = _FakeGma()
        fake.current_result = [
            {"price": 10.5, "symbol": "SHSE.000001", "open": 10.1, "high": 10.8,
             "low": 9.9, "cum_volume": 50000, "created_at": datetime(2026, 1, 6, 15, 0, 0)},
        ]
        out = _provider(fake).index_snapshot(["sh000001"])
        self.assertIsNone(out["000001"]["change"])
        self.assertIsNone(out["000001"]["change_pct"])


class TestLookbackStart(unittest.TestCase):
    def test_calendar_window(self):
        """days*1.5+5 日历日（交易日/日历日 ≈0.666 反推）。固定 now 断言精确日期。"""
        class _FixedDT(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 1, 15, 0, 0, 0)
        with mock.patch.object(gmp, "datetime", _FixedDT):
            self.assertEqual(GmProvider._lookback_start(250), "2024-12-31")


if __name__ == "__main__":
    unittest.main(verbosity=2)
