# -*- coding: utf-8 -*-
"""阶段0-2 回补链状态机持久化 离线单测（2026-09-15，诊断D1/D3）。

覆盖：落盘 → 重开恢复 → 跨日保留（_check_date_reset 不再无脑 clear）→
      双 TTL（当日链 240min / 跨日链 3 交易日，并存取较长者）→
      3 日过期终态 → filled 终态 → 手工清除终态 → B 账户代码 key（000988_B）→
      sell_state events 恢复仅兜底（磁盘权威源优先）。

铁律合规：全部状态文件落在临时目录（SUPERTRADER_ROOT 重定向），不碰 t_io 生产目录。
运行：python tests/stage0_2/test_buyback_chains_persist.py
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTO = os.path.join(_ROOT, "execution", "auto")
_GM = os.path.join(_AUTO, "_gm")
for _p in (_ROOT, _AUTO, _GM):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_TMP = tempfile.mkdtemp(prefix="buyback_chains_test_")
os.environ["SUPERTRADER_ROOT"] = _TMP          # 引擎所有状态文件重定向到临时目录

import t_engine_auto as tea  # noqa: E402
from t_engine_auto import SignalEngine  # noqa: E402

BUYBACK_PATH = os.path.join(_TMP, "t_io", "state", "buyback_chains.json")


def _set_now(y, m, d, hh, mm):
    tea.SIM_NOW = datetime(y, m, d, hh, mm)


def _read_file():
    with open(BUYBACK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class TestBuybackChainsPersist(unittest.TestCase):
    def test_01_arm_persist_atomic(self):
        """armed 落盘：含 code/卖出价/sell_qty/目标价/armed 时间/有效期/状态；原子写无 .tmp 残留。"""
        _set_now(2026, 9, 15, 10, 0)  # 周二
        eng = SignalEngine()
        ret = eng.record_trade_action("600481", "SELL_HIGH", qty=8600, price=8.0)
        self.assertIsNotNone(ret["armed"])
        # B 账户代码 key
        eng.record_trade_action("000988_B", "SELL_HIGH", qty=12100, price=12.0)
        self.assertTrue(os.path.exists(BUYBACK_PATH))
        self.assertFalse(os.path.exists(BUYBACK_PATH + ".tmp"), "原子写 .tmp 残留")
        d = _read_file()
        self.assertIn("600481", d["chains"])
        self.assertIn("000988_B", d["chains"])
        rec = d["chains"]["000988_B"]
        self.assertEqual(rec["sell_qty"], 12100)
        self.assertEqual(rec["sell_price"], 12.0)
        self.assertAlmostEqual(rec["target_price"], round(12.0 * 0.998, 2))  # 目标价=卖出价×0.998
        self.assertEqual(rec["armed_date"], "2026-09-15")
        self.assertEqual(rec["expire_date"], "2026-09-18")  # armed 日起 3 个交易日
        self.assertEqual(rec["status"], "armed")
        self.assertTrue(rec["sell_time"].startswith("2026-09-15"))
        self.eng = eng

    def test_02_reopen_restore(self):
        """重开恢复：新引擎实例从磁盘恢复链（模拟进程重启），persisted 兼容标记保留。"""
        _set_now(2026, 9, 15, 14, 0)
        eng2 = SignalEngine()
        self.assertIn("600481", eng2.awaiting_buyback)
        self.assertIn("000988_B", eng2.awaiting_buyback)
        ab = eng2.awaiting_buyback["000988_B"]
        self.assertEqual(ab["sell_qty"], 12100)
        self.assertTrue(ab.get("persisted"))
        self.assertEqual(ab["armed_date"], "2026-09-15")

    def test_03_intraday_ttl_kept_for_same_day(self):
        """当日链：盘中 TTL（240min）语义保留。"""
        eng = SignalEngine()
        ab = {"sell_price": 8.0, "sell_qty": 100, "sell_time": datetime(2026, 9, 15, 10, 0),
              "armed_date": "2026-09-15", "expire_date": "2026-09-18", "target_price": 7.98}
        expired, reason, _ = eng._buyback_chain_expired(ab, datetime(2026, 9, 15, 13, 0), 240)
        self.assertFalse(expired)  # 180min < 240
        expired, reason, _ = eng._buyback_chain_expired(ab, datetime(2026, 9, 15, 14, 30), 240)
        self.assertTrue(expired)
        self.assertEqual(reason, "intraday_ttl")  # 270min > 240

    def test_04_cross_day_survives_date_reset(self):
        """跨日保留：_check_date_reset 不再无脑 clear；跨日链按 3 交易日（并存取较长者）。"""
        _set_now(2026, 9, 16, 9, 35)  # 次日
        eng = SignalEngine()
        self.assertIn("600481", eng.awaiting_buyback, "跨日恢复失败")
        # 跨日链：盘中 TTL 已自然超时，但 3 交易日规则更长 → 有效
        ab = eng.awaiting_buyback["600481"]
        expired, reason, _ = eng._buyback_chain_expired(ab, datetime(2026, 9, 16, 9, 35), 240)
        self.assertFalse(expired, "跨日链被盘中 TTL 误杀（应取较长的 3 交易日规则）")
        # 模拟跨日 reset
        eng.state_reset_date = "2026-09-15"
        eng._check_date_reset()
        self.assertIn("600481", eng.awaiting_buyback, "_check_date_reset 仍在无脑 clear")
        d = _read_file()
        self.assertIn("600481", d["chains"])

    def test_05_expire_after_3_business_days(self):
        """3 日过期：超过 expire_date 后恢复即转终态，链不入内存。"""
        _set_now(2026, 9, 21, 9, 35)  # 下周一，expire_date=2026-09-18 已过
        eng = SignalEngine()
        self.assertNotIn("600481", eng.awaiting_buyback)
        self.assertNotIn("000988_B", eng.awaiting_buyback)
        terms = [e for e in eng._buyback_terminal_events if e["code"] == "600481"]
        self.assertTrue(terms, "过期终态未留痕")
        self.assertEqual(terms[-1]["status"], "expired")
        d = _read_file()
        self.assertNotIn("600481", d["chains"])
        self.assertTrue(any(e["status"] == "expired" for e in d["terminal_events"]))

    def test_06_filled_terminal(self):
        """filled 终态：回补成交清除链并留终态记录（返回 dict + 诊断 + 落盘）。"""
        _set_now(2026, 9, 15, 11, 0)
        eng = SignalEngine()
        eng.record_trade_action("600481", "SELL_HIGH", qty=8600, price=8.0)
        ret = eng.record_trade_action("600481", "BUY_LOW", qty=8600, price=7.9)
        filled = ret["buyback_filled"]
        self.assertIsNotNone(filled)
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["fill_price"], 7.9)
        self.assertNotIn("600481", eng.awaiting_buyback)
        self.assertEqual(eng.diagnostics["600481"]["buyback_terminal"]["status"], "filled")
        d = _read_file()
        self.assertNotIn("600481", d["chains"])
        self.assertTrue(any(e["code"] == "600481" and e["status"] == "filled"
                            for e in d["terminal_events"]))

    def test_06b_partial_fill_keeps_chain(self):
        """部分回补（阶段0-2b 根治）：只冲减 sell_qty、保留链；全量回补才清链写终态。"""
        _set_now(2026, 9, 15, 11, 0)
        eng = SignalEngine()
        eng.record_trade_action("000988_B", "SELL_HIGH", qty=12100, price=12.0)
        # 第 1 笔部分回补 5000 < 12100 → 链保留，返回 None（不写闭环事件）
        ret = eng.record_trade_action("000988_B", "BUY_LOW", qty=5000, price=11.9)
        self.assertIsNone(ret["buyback_filled"], "部分回补不应返回闭环记录")
        ab = eng.awaiting_buyback.get("000988_B")
        self.assertIsNotNone(ab, "部分回补整链被 pop（旧 bug 复现）")
        self.assertEqual(ab["sell_qty"], 7100)           # 剩余量
        self.assertEqual(ab["filled_qty"], 5000)         # 累计已回补
        self.assertEqual(ab["partial_fills"], 1)
        self.assertEqual(eng.diagnostics["000988_B"]["buyback_partial_fill"]["remain_qty"], 7100)
        # 落盘生效（嵌套 dict 直改不触发 hook 的陷阱：必须整体回写）
        d = _read_file()
        self.assertEqual(d["chains"]["000988_B"]["sell_qty"], 7100, "部分回补未落盘")
        self.assertEqual(d["chains"]["000988_B"]["partial_fills"], 1)
        # 重启恢复：剩余量与累计量随链恢复
        _set_now(2026, 9, 15, 11, 5)
        eng2 = SignalEngine()
        ab2 = eng2.awaiting_buyback.get("000988_B")
        self.assertIsNotNone(ab2)
        self.assertEqual(ab2["sell_qty"], 7100)
        self.assertEqual(ab2["filled_qty"], 5000)
        # 第 2 笔回补 7100 >= 剩余 → 全量闭环，清链写终态
        ret2 = eng2.record_trade_action("000988_B", "BUY_LOW", qty=7100, price=11.9)
        filled = ret2["buyback_filled"]
        self.assertIsNotNone(filled)
        self.assertEqual(filled["status"], "filled")
        self.assertEqual(filled["filled_qty"], 12100)    # 终态注明累计已回补量
        self.assertEqual(filled["partial_fills"], 1)     # 终态注明部分回补次数
        self.assertNotIn("000988_B", eng2.awaiting_buyback)
        d2 = _read_file()
        self.assertNotIn("000988_B", d2["chains"])
        self.assertTrue(any(e["code"] == "000988_B" and e["status"] == "filled"
                            and e["filled_qty"] == 12100 and e["partial_fills"] == 1
                            for e in d2["terminal_events"]))

    def test_07_manual_clear_terminal(self):
        """手工清除终态：clear_awaiting_buyback 留痕 + 落盘。"""
        _set_now(2026, 9, 15, 11, 30)
        eng = SignalEngine()
        eng.record_trade_action("000988_B", "SELL_HIGH", qty=12100, price=12.0)
        rec = eng.clear_awaiting_buyback("000988_B", reason="单测手工清除")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "cleared")
        self.assertNotIn("000988_B", eng.awaiting_buyback)
        d = _read_file()
        self.assertNotIn("000988_B", d["chains"])
        self.assertTrue(any(e["code"] == "000988_B" and e["status"] == "cleared"
                            and e["reason"] == "单测手工清除"
                            for e in d["terminal_events"]))
        # 清除不存在的链 → None，不炸
        self.assertIsNone(eng.clear_awaiting_buyback("000988_B"))

    def test_08_sell_state_disk_authoritative(self):
        """sell_state events 恢复仅兜底：磁盘已有链 → 跳过；磁盘无链 → events 恢复并同步落盘。"""
        import sell_state as ss

        _set_now(2026, 9, 15, 9, 40)
        eng = SignalEngine()
        # 磁盘权威源已有 600481 链（卖出价 8.0）
        eng.record_trade_action("600481", "SELL_HIGH", qty=8600, price=8.0)

        # 伪造 sell_state 状态文件（也放临时目录）
        ss.SELL_STATE_PATH = os.path.join(_TMP, "t_io", "state", "auto_sell_state.json")
        ss._sell_state_save({
            "600481": {"pos_key": "10000@7.5000", "_buyback": {
                "sell_price": 99.0, "sell_qty": 1, "sell_action": "SELL_HIGH",
                "target_price": 98.0, "sell_time": "2026-09-15 09:35:00",
                "expire_date": "2026-09-18"}},
            "300054": {"pos_key": "5000@20.0000", "_buyback": {
                "sell_price": 20.5, "sell_qty": 700, "sell_action": "SELL_HIGH",
                "target_price": 20.46, "sell_time": "2026-09-15 09:36:00",
                "expire_date": "2026-09-18"}},
        })
        # mock GM 命名空间 + context（live 模式）
        ss.GM = types.SimpleNamespace(
            MODE_LIVE=1,
            STOCKS={"600481": "SHSE.600481", "300054": "SZSE.300054"},
            _audit_write=lambda *a, **k: None)
        ctx = types.SimpleNamespace(
            mode=1, engine=eng, bar_cache={},
            manual_position={
                "SHSE.600481": {"qty": 10000, "cost": 7.5, "pre_close": 7.5},
                "SZSE.300054": {"qty": 5000, "cost": 20.0, "pre_close": 20.0},
            })
        ss._sell_state_restore(ctx)
        # 磁盘权威源优先：600481 仍是 8.0，不被 events 旧值 99.0 覆盖
        self.assertEqual(eng.awaiting_buyback["600481"]["sell_price"], 8.0)
        # 兜底：300054 磁盘无链 → events 恢复成功并同步进权威源
        self.assertIn("300054", eng.awaiting_buyback)
        self.assertEqual(eng.awaiting_buyback["300054"]["sell_price"], 20.5)
        d = _read_file()
        self.assertIn("300054", d["chains"], "events 兜底恢复未同步进磁盘权威源")


if __name__ == "__main__":
    print(f"临时目录: {_TMP}")
    unittest.main(verbosity=2)
