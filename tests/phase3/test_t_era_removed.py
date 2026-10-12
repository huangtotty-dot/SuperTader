# -*- coding: utf-8 -*-
"""做T口径已整链退场：守卫它**保持退场**（2026-10-12）。

## 经过（值得记住，因为删错一次就会掐断别的东西）

owner 收到「🚨 闭环审计异常」飞书卡（`qty=base` 不一致 / 模拟盘缺口 / 请尾盘手动处理），
说「**因为已经没有手动做T了，因此这些提示以及相关的代码可以删除了**」。

**第一轮我只删告警、刻意保留数据写入** —— 因为查到 `closure_audit.jsonl` 是
`execution/daily_sentiment.load_loss_streaks`（V2c 连亏熔断）的**唯一数据源**。
owner 说「继续」（= 连同做T口径一起退场）后做了第二轮，但**先验证了三件事**：

1. 连亏熔断**不是死码**：真实数据推出的连亏计数
   `{'600481':1, '000988_B':3, '300153':2, '300364':1, '002639':1}`
   ⇒ 有 2 只 ≥ 阈值 2，**只要 `rank > 6` 就会真的触发** ⇒ 不能当"没在跑"删。
2. 它的「**清仓门控**」**从未门控过任何东西**：全仓 `trade_gate` 7 处引用里，
   唯一非本模块的一处是 `main.py` 写死 `"normal"` ⇒ 没有下游读它，
   实际效果只是往复盘文本里塞一句话。
3. 自动侧（`t_engine_auto.py` / `gm_main.py`）**完全不读** `trade_gate`/`pos_factor`/sentiment
   ⇒ 删除**不影响任何下单路径**，只影响复盘文本。

⇒ 整链可删，且删除是**安全的**（不是靠"看起来没在用"）。

## 本文件守什么

守"退场"本身 —— 防止日后有人把做T口径接回来而无人察觉。

运行：python -m unittest tests.phase3.test_t_era_removed -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestTeraChainRetired(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = (_ROOT / "main.py").read_text(encoding="utf-8")
        cls.ds = (_ROOT / "execution" / "daily_sentiment.py").read_text(encoding="utf-8")
        cls.cfg = (_ROOT / "config.py").read_text(encoding="utf-8")

    def test_closure_audit_removed_from_main(self):
        for name in ("_maybe_audit_closure", "_closure_tail_reconcile", "_closure_audit_date",
                     "closure_audit.jsonl"):
            self.assertNotIn(name, self.main, f"做T闭环审计应已整链移除: {name}")

    def test_no_feishu_cards_for_t(self):
        self.assertNotIn("_push_daily_pnl_feishu", self.main, "做T收益明细卡应已删除")
        self.assertNotIn("闭环审计异常", self.main, "闭环异常红卡应已删除")

    def test_t_pnl_helpers_removed(self):
        """`compute_t0_pnl` / `_t0_fee_rates` / 做T费率常量随做T退场一并删除。"""
        for name in ("compute_t0_pnl", "_t0_fee_rates", "T0_SELL_FEE_RATE",
                     "T0_BUY_FEE_RATE", "_cost_fees"):
            self.assertNotIn(name, self.main, f"做T盈亏/费率工具应已删除: {name}")

    def test_loss_streak_gate_removed_from_sentiment(self):
        for name in ("load_loss_streaks", "_closure_audit_path", "loss_streak_days",
                     "closure_audit_file"):
            self.assertNotIn(name, self.ds, f"V2c 连亏熔断应已移除: {name}")
        self.assertNotIn("连亏熔断", self.ds, "闸门与其文案应一并移除")

    def test_sentiment_params_cleaned(self):
        for name in ("loss_streak_days", "closure_audit_file", "_closure_audit_date"):
            self.assertNotIn(name, self.cfg, f"config 里的 sentiment 做T参数应已清理: {name}")


class TestSentimentStillWorks(unittest.TestCase):
    """退掉一条 P 规则后，`per_stock_decisions` 本身必须仍然可用（别删出引用问题）。"""

    def test_import_and_run(self):
        from execution.daily_sentiment import per_stock_decisions
        out = per_stock_decisions("震荡", 0.0, 0.0, holdings={}, date_str=None, params=None)
        self.assertIsInstance(out, dict, f"应正常返回 dict，实际 {type(out)}")

    def test_priority_comment_no_p6(self):
        cfg = (_ROOT / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("P1>P2>P3>P4>P5>P6>P7", cfg, "优先级说明里的 P6 应已移除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
