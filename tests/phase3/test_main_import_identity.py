# -*- coding: utf-8 -*-
"""main 正常 import 重构的身份与防回归断言。"""
import ast
import importlib
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class MainImportIdentityTest(unittest.TestCase):
    def test_exec_loader_removed_statically(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        ast.parse(source, filename="main.py")  # 不依赖运行态即可验证语法
        self.assertNotIn("exec(compile", source)
        self.assertNotIn("globals().update", source)
        self.assertNotIn("module_order = [", source)

    def test_main_uses_canonical_module_objects(self):
        # main 的 __main__ 守卫保证不启动主循环/GUI。子进程隔离其既有 stdout
        # 重包装副作用，避免关闭 pytest 当前进程的捕获器。
        code = r'''
import main
import analysis.indicators as indicators
import analysis.index_regime as index_regime
import config
import core.market_regime as market_regime
import core.position_sizer as position_sizer
import core.signal_engine as signal_engine
import core.utils as utils
import execution.daily_sentiment as daily_sentiment
import src.data_fetcher as data_fetcher

assert main.SignalEngine is signal_engine.SignalEngine
assert main.EngineContext is signal_engine.EngineContext
assert main.detect_index_regime is index_regime.detect_index_regime
assert daily_sentiment._detect_index_regime is index_regime.detect_index_regime
assert main.MarketRegime is market_regime.MarketRegime
assert main.detect_regime is market_regime.detect_regime
assert main.calc_buy_qty is position_sizer.calc_buy_qty
assert main.calc_sell_qty is position_sizer.calc_sell_qty
assert main.HOLDINGS is config.HOLDINGS
assert main.INDEX_REGIME_CONTEXT is config.INDEX_REGIME_CONTEXT
assert main.SIGNAL_OUTCOME_TRACKER is config.SIGNAL_OUTCOME_TRACKER
assert main.AI_REVIEW_STATS is utils.AI_REVIEW_STATS
assert main.DAILY_DECISION_STATS is utils.DAILY_DECISION_STATS
assert main.DAILY_CONTEXT_CACHE is data_fetcher.DAILY_CONTEXT_CACHE
assert main.MINUTE_FETCH_STATUS is data_fetcher.MINUTE_FETCH_STATUS
assert main.MINUTE_FETCH_DETAIL is data_fetcher.MINUTE_FETCH_DETAIL
for name in ("add_indicators", "add_5min_indicators", "add_15min_indicators"):
    assert getattr(main, name) is getattr(indicators, name)
print("import main OK; canonical identities OK")
'''
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertIn("canonical identities OK", result.stdout)

    def test_data_fetcher_indicator_delegates_are_identical(self):
        data_fetcher = importlib.import_module("src.data_fetcher")
        indicators = importlib.import_module("analysis.indicators")
        for name in ("add_indicators", "add_5min_indicators",
                     "add_15min_indicators"):
            self.assertIs(getattr(data_fetcher, name), getattr(indicators, name))


if __name__ == "__main__":
    unittest.main()
