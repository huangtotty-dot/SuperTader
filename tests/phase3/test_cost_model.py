# -*- coding: utf-8 -*-
"""交易成本单一真源单测 —— core/cost_model.py（2026-11-13 补）。

## 为什么要有这个测试

`cost_model` 是全仓实验与生产共用的成本口径。曾有一次口径事故：全仓做T实验
一度按 **往返 0.136%** 跑（内含 2023 年前已废止的 0.1% 印花税 + 万1.5 佣金假设），
owner 实际费率是 **往返 0.069%（股票）/ 0.019%（ETF）**。修法不是改数学，而是：
- 当前口径 `stock/etf` 用真费率（0.069%/0.019%）；
- 旧口径被隔离成 `legacy` venue（0.136%），仅供回归对照。

本测试钉住三件事，防止有人把默认口径退回 legacy 或改坏费后收益率数学：
1. 费率常量与文档口径一致（佣金万0.854 / 印花税0.05%卖 / 过户费0.001%双边）。
2. 默认 venue 走**当前**真费率，不是 legacy。
3. `leg_pnl` 的费后收益率数学（正T/反T 两种方向）与 `run_experiment._leg_pnl` 同构。

运行：python tests/phase3/test_cost_model.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.cost_model as cm  # noqa: E402


class TestFeeConstants(unittest.TestCase):
    def test_round_trip_matches_documented_rates(self):
        """当前口径：股票往返 0.06908%，ETF 往返 0.01908%（doc 里的真费率）。"""
        self.assertAlmostEqual(cm.ROUND_TRIP["stock"], 0.0006908, places=9)
        self.assertAlmostEqual(cm.ROUND_TRIP["etf"], 0.0001908, places=9)
        # 逐项拆解：卖=佣金+过户+印花(0.05%)，买=佣金+过户
        self.assertAlmostEqual(cm.FEE_SELL_STOCK, cm.COMMISSION + cm.TRANSFER_FEE + cm.STAMP_TAX_SELL, places=12)
        self.assertAlmostEqual(cm.FEE_BUY_STOCK, cm.COMMISSION + cm.TRANSFER_FEE, places=12)
        # ETF 卖出免征印花税
        self.assertAlmostEqual(cm.FEE_SELL_ETF, cm.FEE_BUY_ETF, places=12)

    def test_legacy_round_trip_is_isolated(self):
        """legacy 往返 0.136% 仍保留，但只作回归对照，不参与默认路径。"""
        self.assertAlmostEqual(cm.ROUND_TRIP["legacy"], 0.00136, places=9)
        self.assertAlmostEqual(cm.LEGACY_FEE_S + cm.LEGACY_FEE_B, 0.00136, places=9)


class TestFees(unittest.TestCase):
    def test_fees_stock(self):
        fs, fb = cm.fees("stock")
        self.assertAlmostEqual(fs, cm.FEE_SELL_STOCK, places=12)
        self.assertAlmostEqual(fb, cm.FEE_BUY_STOCK, places=12)

    def test_fees_etf(self):
        fs, fb = cm.fees("etf")
        self.assertAlmostEqual(fs, cm.FEE_SELL_ETF, places=12)
        self.assertAlmostEqual(fb, cm.FEE_BUY_ETF, places=12)

    def test_fees_legacy(self):
        fs, fb = cm.fees("legacy")
        self.assertEqual((fs, fb), (cm.LEGACY_FEE_S, cm.LEGACY_FEE_B))

    def test_fees_default_is_current_stock(self):
        """默认不传 venue 时必须走当前真费率，绝不能回退 legacy。"""
        fs, fb = cm.fees()
        self.assertAlmostEqual(fs, cm.FEE_SELL_STOCK, places=12)
        self.assertNotEqual(fs, cm.LEGACY_FEE_S, "默认口径泄漏成 legacy 了")

    def test_fees_unknown_venue_falls_back_stock(self):
        fs, fb = cm.fees("crypto")
        self.assertAlmostEqual(fs, cm.FEE_SELL_STOCK, places=12)


class TestVenue(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ST_COST_VENUE", None)

    def test_default_venue_is_stock(self):
        os.environ.pop("ST_COST_VENUE", None)
        self.assertEqual(cm.venue(), "stock")

    def test_env_override_etf(self):
        os.environ["ST_COST_VENUE"] = "etf"
        self.assertEqual(cm.venue(), "etf")

    def test_env_override_legacy(self):
        os.environ["ST_COST_VENUE"] = "LEGACY"  # 大小写不敏感
        self.assertEqual(cm.venue(), "legacy")

    def test_invalid_env_falls_back_stock(self):
        os.environ["ST_COST_VENUE"] = "  option "  # 非法值（含空白）
        self.assertEqual(cm.venue(), "stock")


class TestLegPnl(unittest.TestCase):
    def test_long_breakeven_is_zero(self):
        """正T：买卖同价，扣除双边费后为负（不是 0，也不是翻倍）。"""
        pnl = cm.leg_pnl("long", fill=10.0, out=10.0)
        self.assertLess(pnl, 0.0)
        self.assertAlmostEqual(pnl, -100.0 * (cm.FEE_BUY_STOCK + cm.FEE_SELL_STOCK), places=8)

    def test_long_profit_math(self):
        """正T：10 买 10.5 卖，净 = (卖×(1−费卖) − 买×(1+费买))/买。"""
        fs, fb = cm.fees()
        pnl = cm.leg_pnl("long", fill=10.0, out=10.5)
        expected = 100.0 * (10.5 * (1 - fs) - 10.0 * (1 + fb)) / 10.0
        self.assertAlmostEqual(pnl, expected, places=9)

    def test_short_math(self):
        """反T：卖 10 买回 9.8，净 = (卖×(1−费卖) − 买回×(1+费买))/卖。"""
        fs, fb = cm.fees()
        pnl = cm.leg_pnl("short", fill=10.0, out=9.8)
        expected = 100.0 * (10.0 * (1 - fs) - 9.8 * (1 + fb)) / 10.0
        self.assertAlmostEqual(pnl, expected, places=9)

    def test_explicit_fees_override(self):
        """显式传 fee_s/fee_b 时不再查环境，纯按给定费算。"""
        pnl = cm.leg_pnl("long", fill=100.0, out=101.0, fee_s=0.0, fee_b=0.0)
        self.assertAlmostEqual(pnl, 1.0, places=9)  # 无费时 +1 元 = +1%

    def test_round_trip_helper(self):
        self.assertAlmostEqual(cm.round_trip("stock"), 0.0006908, places=9)
        self.assertAlmostEqual(cm.round_trip(), 0.0006908, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
