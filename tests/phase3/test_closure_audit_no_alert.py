# -*- coding: utf-8 -*-
"""闭环审计：**只记数据、不再告警**（2026-10-12）。

owner：「因为已经没有手动做T了，因此这些提示以及相关的代码可以删除」——
当时收到的是「🚨 闭环审计异常」飞书红卡，内容是
「江西铜业 qty=500 与 base=0 不一致」「【模拟盘 auto】卖出 X / 未接回 X」等，
这些口径都是**手动做T时代**的闭环检查，现在只会产生假警报。

**但整段不能删**：`closure_audit.jsonl` 是
`execution/daily_sentiment.load_loss_streaks`（V2c 连亏熔断，同一标的连续 N 日做T亏损 → 次日 hold）
的**唯一数据源**，删掉会**静默掐断**那条闸 ——
这与本仓那条老教训同型：「用静态证据判断"某开关没生效"很容易，但**删之前要先确认还有谁在读它**」。

故本次只删**提示/推送**：qty/base 一致性、模拟盘缺口、虚报闭环、做T收益明细卡、异常红卡。

运行：python -m unittest tests.phase3.test_closure_audit_no_alert -v
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


class TestAuditNoLongerAlerts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (_ROOT / "main.py").read_text(encoding="utf-8")
        i = cls.src.index("def _maybe_audit_closure")
        j = cls.src.index("def _closure_tail_reconcile")
        cls.audit = cls.src[i:j]

    def test_no_feishu_push_in_audit(self):
        self.assertNotIn("send_feishu_payload", self.audit,
                         "闭环审计不得再推飞书（提示已随手动做T下线移除）")
        self.assertNotIn("_feishu_card_header", self.audit, "红卡头已删")

    def test_alert_mechanism_removed(self):
        """钉**告警机制**与**告警原文**，而不是随便禁某个词 ——
        「模拟盘缺口」在 docstring 里作为"哪些告警已移除"的说明合法存在。"""
        self.assertNotIn("problems.append", self.audit, "告警收集机制应已移除")
        for phrase in ("请尾盘手动处理", "不入实盘台账", "请核对 holdings_manual.json"):
            self.assertNotIn(phrase, self.audit, f"手动做T时代的告警原文应已移除: {phrase}")

    def test_收益明细卡已删除(self):
        self.assertNotIn("_push_daily_pnl_feishu", self.src, "做T收益明细卡函数应已整段删除")

    def test_still_writes_details_for_loss_streak(self):
        """**关键护栏**：数据记录必须留着 —— 连亏熔断在读它。"""
        self.assertIn("closure_audit.jsonl", self.audit, "仍要落盘")
        self.assertIn('"details": details', self.audit, "仍要写 details")
        # est_pnl 是 load_loss_streaks 唯一关心的字段，来自 compute_t0_pnl
        self.assertIn("compute_t0_pnl", self.audit, "est_pnl 的计算不能删")


class TestLossStreakStillParsesNewShape(unittest.TestCase):
    """新记录（不带 problems）必须仍被 `load_loss_streaks` 正确解析。"""

    def test_two_day_streak_detected(self):
        from execution.daily_sentiment import load_loss_streaks
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "closure_audit.jsonl"
            recs = [
                # 单日：亏损
                {"date": "2026-10-09", "time": "14:50:00", "ok": True,
                 "details": [{"code": "600362", "name": "江西铜业", "sold": 500, "bought": 500,
                              "est_pnl": -120.0}]},
                # 次日：又亏
                {"date": "2026-10-12", "time": "14:50:00", "ok": True,
                 "details": [{"code": "600362", "name": "江西铜业", "sold": 500, "bought": 500,
                              "est_pnl": -80.0}]},
                # 另一只只亏一天 ⇒ 不成连亏
                {"date": "2026-10-12", "time": "14:50:00", "ok": True,
                 "details": [{"code": "600584", "name": "长电科技", "sold": 100, "bought": 100,
                              "est_pnl": -5.0}]},
            ]
            with open(fp, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            got = load_loss_streaks(params={"closure_audit_file": str(fp)})
        self.assertEqual(got.get("600362"), 2, "两日连亏应被识别（新记录形状不含 problems）")
        # ⚠️ 本函数返回的是**任意长度**的连亏计数，阈值（>=loss_streak_days）由调用方判
        #（daily_sentiment:614 `rank > 6 and ls >= p["loss_streak_days"]`）⇒ 只亏一天要返回 1
        self.assertEqual(got.get("600584"), 1, "只亏一天的也应返回 1（阈值不在本函数内判）")

    def test_no_trade_day_does_not_break_streak(self):
        """无成交日跳过、不打断连续性（与既有语义一致）。"""
        from execution.daily_sentiment import load_loss_streaks
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "closure_audit.jsonl"
            with open(fp, "w", encoding="utf-8") as f:
                for d, pnl, qty in (("2026-10-09", -50.0, 100), ("2026-10-10", 0.0, 0),
                                    ("2026-10-12", -30.0, 100)):
                    f.write(json.dumps({"date": d, "ok": True,
                                        "details": [{"code": "600362", "sold": qty,
                                                     "bought": qty, "est_pnl": pnl}]},
                                       ensure_ascii=False) + "\n")
            got = load_loss_streaks(params={"closure_audit_file": str(fp)})
        self.assertEqual(got.get("600362"), 2, "中间无成交日不应打断连亏计数")


if __name__ == "__main__":
    unittest.main(verbosity=2)
