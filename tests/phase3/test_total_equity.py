# -*- coding: utf-8 -*-
"""账户总资产计算单测 —— core/total_equity.py（2026-11-13 补）。

P0-9(B1) 单股上限需要真实总资产：账户A现金+持仓市值 + 账户B现金+持仓市值。
口径（钉死，别改坏）：
  现金 = max(0, total_capital − Σ(cost×qty))    # 持仓成本，忽略已实现盈亏/手续费
  市值 = Σ(最新价×qty)                           # 最新价取 last_price → pre_close → cost 三级兜底
  归属：holdings.json 不再存 account 字段 → 由 accounts_config.json 各账户的 holdings 清单派生，
        清单里没有的代码落到 default（账户A）。
本测试用临时 accounts_config.json + monkeypatch HOLDINGS/DAILY_DECISION_STATS，纯离线算。

运行：python tests/phase3/test_total_equity.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.total_equity as te  # noqa: E402


class TestComputeTotalEquity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (te._ROOT, te.HOLDINGS, te.DAILY_DECISION_STATS,
                       dict(te._TOTAL_EQUITY_CACHE))
        te._ROOT = self._tmp.name

    def tearDown(self):
        te._ROOT, te.HOLDINGS, te.DAILY_DECISION_STATS, cache = self._saved
        te._TOTAL_EQUITY_CACHE.update(cache)
        self._tmp.cleanup()

    def _write_accounts(self, cfg):
        os.makedirs(os.path.join(self._tmp.name, "t_io", "state"), exist_ok=True)
        with open(os.path.join(self._tmp.name, "t_io", "state", "accounts_config.json"),
                  "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

    def _reset_cache(self):
        te._TOTAL_EQUITY_CACHE.update({"ts": 0.0, "value": 0.0})

    def test_two_account_equity_math(self):
        self._write_accounts({"accounts": {
            "账户A": {"total_capital": 100000, "holdings": ["600000", "300750"]},
            "账户B": {"total_capital": 50000, "holdings": ["002451"]},
        }})
        te.HOLDINGS = {
            "600000": {"qty": 1000, "cost": 10},
            "300750": {"qty": 500, "cost": 20},
            "002451": {"qty": 200, "cost": 30},
        }
        te.DAILY_DECISION_STATS = {
            "600000": {"last_price": 11},
            "300750": {"last_price": 22},
            "002451": {"last_price": 31},
        }
        self._reset_cache()
        # A: cost=20000, mv=22000, cash=80000 → 102000
        # B: cost=6000,  mv=6200,  cash=44000 → 50200
        self.assertAlmostEqual(te._compute_total_equity(), 152200.0, places=2)

    def test_cash_clamped_at_zero(self):
        self._write_accounts({"accounts": {
            "账户A": {"total_capital": 1000, "holdings": ["600000"]},
        }})
        te.HOLDINGS = {"600000": {"qty": 100, "cost": 20}}  # cost=2000 > capital
        te.DAILY_DECISION_STATS = {"600000": {"last_price": 30}}  # mv=3000
        self._reset_cache()
        # cash = max(0, 1000-2000)=0 → equity = 0 + 3000 = 3000
        self.assertAlmostEqual(te._compute_total_equity(), 3000.0, places=2)

    def test_unlisted_code_falls_to_default_account(self):
        """不在任何账户 holdings 清单里的代码 → 归 default（账户A）。"""
        self._write_accounts({"accounts": {
            "账户A": {"total_capital": 5000, "holdings": []},
            "账户B": {"total_capital": 9000, "holdings": []},
        }})
        te.HOLDINGS = {"999999": {"qty": 100, "cost": 10}}
        te.DAILY_DECISION_STATS = {"999999": {"last_price": 12}}
        self._reset_cache()
        # 999999 → 账户A: cost=1000, mv=1200, cash=4000 → 5200；B 空 → 9000；总 14200
        self.assertAlmostEqual(te._compute_total_equity(), 14200.0, places=2)

    def test_zero_qty_holding_skipped(self):
        self._write_accounts({"accounts": {
            "账户A": {"total_capital": 5000, "holdings": ["600000"]},
        }})
        te.HOLDINGS = {"600000": {"qty": 0, "cost": 10}}  # 清仓 → 跳过
        te.DAILY_DECISION_STATS = {}
        self._reset_cache()
        self.assertAlmostEqual(te._compute_total_equity(), 5000.0, places=2)

    def test_missing_config_returns_zero(self):
        """accounts/portfolio 都不存在 → 返回 0（本轮单股上限检查跳过，不抛）。"""
        self._reset_cache()
        self.assertEqual(te._compute_total_equity(), 0.0)

    def test_cache_short_circuit(self):
        te._TOTAL_EQUITY_CACHE.update({"ts": time.time(), "value": 4242.0})
        self.assertEqual(te._compute_total_equity(), 4242.0)  # 不读文件直接返回


if __name__ == "__main__":
    unittest.main(verbosity=2)
