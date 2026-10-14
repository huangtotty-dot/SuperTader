# -*- coding: utf-8 -*-
"""超跌跳空确认 · **两日全周期纸面演练**（上线前总验证，2026-10-12）。**全离线**。

## 为什么要有它

`test_gm_main_osc_hooks` 只验"钩子闸与定序"（钩子被换成记录器）；`test_osc_regression`
只验纯函数口径。两者都**证明不了"真跑一天会怎样"**——而钩子内部（取数→执行器→网关→
账本→成交回报）恰恰是搬家/改名后最容易断的地方。

本测试用可编程假 GM（取数/账户/下单全假）在**沙箱目录**走完两天真实代码路径：
    第 1 天 09:31 预筛 → 10:00 买入 → 成交回报 → 入账本 → 14:50（T+1 不该卖）
    第 2 天 10:00 早盘冲高卖 + 加仓 → 14:50 SL5 只深止损 → 账本与账户对齐

## 它上线前抓到的两个真缺陷（都在本测试里被钉住）

1. **`rt.gm.history` 没暴露**：宿主 `_GmPrimitives` 只给了 `history_n`，而三个取数钩子用的是
   列表式 `history` ⇒ AttributeError ⇒ 钩子各自 fail-closed ⇒ **当天永不下单**。
2. **跨日加仓把账本条目整体覆盖**（原有缺陷）：数量只记新买的、entry_date/entry_price 被重置
   ⇒ 账本恒小于账户，而卖侧只卖 `min(账本, available)` ⇒ 那部分股票**永远卖不出去**。
   修法：同日/跨日一律合并（数量相加、入场价按量加权、**保留原 entry_date**）。

运行：python -m unittest tests.phase3.test_osc_full_cycle -v
"""
import json
import os
import subprocess
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DRIVER = os.path.join(_ROOT, "tests", "phase3", "osc_cycle_driver.py")

_CACHE = {}


def _run() -> dict:
    if "r" in _CACHE:
        return _CACHE["r"]
    env = dict(os.environ)
    env["SMOKE_ROOT"] = _ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)          # 裸环境（严苛：见 test_gm_main_osc_hooks 的说明）
    proc = subprocess.run([sys.executable, _DRIVER], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=300)
    line = next((ln for ln in (proc.stdout or "").splitlines()
                 if ln.startswith("RESULT ")), None)
    if line is None:
        raise AssertionError("演练驱动未产出 RESULT\nrc=%s\nOUT:\n%s\nERR:\n%s"
                             % (proc.returncode, (proc.stdout or "")[-3000:],
                                (proc.stderr or "")[-3000:]))
    _CACHE["r"] = json.loads(line[len("RESULT "):])
    return _CACHE["r"]


class TestFullCycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _run()
        cls.d1 = cls.r["days"]["d1"]
        cls.d2 = cls.r["days"]["d2"]

    # ── 第 1 天 ────────────────────────────────────────────
    def test_t1_prescreen_picks_weak_and_hot_board(self):
        """预筛：候选 = 热门板块（涨停家数 top-N）内 **且 昨收<MA20** 的票。

        造场景时让 600004/5/6 昨日涨停（只负责把「半导体」顶成热门板块），它们自己上行、
        不是超跌票 ⇒ **不得进候选**。600099 在第 1 天没进"可交易集"（无 10:00 棒）⇒ 不下单。
        """
        cand = set(self.d1["candidates"])
        self.assertTrue({"600001", "600002", "600003"} <= cand, "超跌票应在候选内")
        for jumper in ("600004", "600005", "600006"):
            self.assertNotIn(jumper, cand, "提供涨停家数的上行票不该是候选")
        ordered = {o[0] for o in self.d1["orders"]}
        self.assertTrue(ordered <= cand, "只能对候选下单")
        self.assertNotIn("600099", ordered, "第 1 天无 10:00 棒的票不下单")

    def test_t2_buy_side_places_top_n_and_tags_strategy(self):
        """买侧：3 只候选各下一笔 BUY；事件桥 order_type 必须是策略 id（孤儿闸归属）。"""
        self.assertEqual(len(self.d1["orders"]), 3)
        self.assertTrue(all(o[2] == 1 for o in self.d1["orders"]), "都是买腿")
        self.assertEqual({o[0] for o in self.d1["orders"]}, {"600001", "600002", "600003"})
        self.assertEqual(self.d1["summary"]["buys_placed"], 3)
        self.assertEqual(self.d1["summary"]["rejected_count"], 0)
        self.assertEqual(self.d1["order_event_types"], ["osc"])

    def test_t2b_backfill_hooks_wired_and_noop_when_all_filled(self):
        """2026-10-14 买不进替补：10:08 / 10:14 两格必须在**真 gm_main.on_bar** 里被分发到；
        本场景三笔首单全部回报成交 ⇒ 补单轮应**空转**（不撤单、不下新单、不留 pending）。"""
        self.assertEqual(self.d1["backfill_rounds_done"], [0, 1],
                         "两个补单时点都应跑到（接线证据）")
        self.assertEqual(self.d1["backfill_pending_left"], 0,
                         "首轮全成 ⇒ 无待补单")
        self.assertEqual(len(self.d1["orders"]), 3,
                         "补单轮不得凭空多下单")
        self.assertTrue(self.d1["day_state_exists"],
                        "当日状态必须落盘（进程重启后靠它防重复下单/找回挂单）")

    def test_t3_book_records_entry_price_date_dev20(self):
        """成交后入账本：entry_date=当日、entry_price=10:00 收盘、qty、dev20（SL5 判据）。"""
        bk = self.d1["book_after_fill"]
        self.assertEqual(set(bk), {"600001", "600002", "600003"})
        for c, v in bk.items():
            self.assertEqual(v["entry_date"], self.d1["date"])
            self.assertGreater(v["qty"], 0)
            self.assertIsNotNone(v["dev20"])
        self.assertLess(bk["600002"]["dev20"], -0.07, "深超跌票的 dev20 必须 < −7%（SL5 只深判据）")

    def test_t4_t1_no_same_day_sell(self):
        """A 股 T+1：买入当日 14:50 不得卖出（全部 t1_same_day）。"""
        self.assertEqual(self.d1["sell_d1"]["sells_placed"], 0)
        self.assertEqual(self.d1["sell_d1"]["open_after"], 3)

    # ── 第 2 天 ────────────────────────────────────────────
    def test_t5_next_day_morning_pop_sell(self):
        """次日早盘冲高卖：只对 days_held==0 的持仓、且 high ≥ 入场价×(1+3%)。"""
        m = {x[0]: x[1] for x in self.d2["morning_sells"]}
        self.assertEqual(m.get("600001"), "pop_sell")
        self.assertEqual(m.get("600002"), "hold", "没冲到 +3% 的不卖")
        self.assertEqual(self.d2["morning_summary"]["pop_count"], 1)
        self.assertEqual(self.d2["morning_summary"]["sells_filled"], 1)

    def test_t6_cross_day_add_merges_instead_of_overwriting(self):
        """**回归守卫**（上线前演练抓到的真缺陷）：跨日加仓必须合并、且保留原 entry_date。

        覆盖式的旧实现会让账本恒小于账户（差额永远卖不出去）。
        """
        bk = self.d2["book_after"]
        self.assertIn("600002", bk)
        self.assertEqual(bk["600002"]["entry_date"], self.d1["date"],
                         "合并后 entry_date 必须是**首次入场日**（被覆盖会变成次日）")
        self.assertEqual(bk["600002"]["days_held"], 1, "持有天数按首次入场算")

    def test_t7_sl5_deep_only_deep(self):
        """14:50 止损：深超跌（dev20<−7%）跌破 −5% ⇒ sl5_deep；浅/中不挂。"""
        c = {x[0]: x[1] for x in self.d2["close_sells"]}
        self.assertEqual(c.get("600002"), "sl5_deep")
        self.assertEqual(c.get("600003"), "hold")
        self.assertEqual(self.d2["close_summary"]["stop_count"], 1)

    def test_t8_book_matches_account_at_end(self):
        """**账本 ↔ 账户一致**（第 1 条缺陷的终态证据）：账本里的票数量必须与账户一致。

        允许账户多出「非本策略」的票（本场景的 600099）——那是被归属锁保护的对象。
        """
        bk = self.d2["book_after"]
        acct = {s.split(".")[-1]: q for s, q in self.d2["acct"].items()}
        for code, rec in bk.items():
            self.assertEqual(rec["qty"], acct.get(code),
                             f"{code} 账本 {rec['qty']} ≠ 账户 {acct.get(code)}")

    def test_t9_ownership_lock_blocks_unclaimed_account_position(self):
        """一票一策略硬锁：账户持有但无人认领的票 ⇒ 买单被拒、不送柜台、留痕可查。"""
        self.assertNotIn("600099", [o[0] for o in self.d2["orders"]],
                         "被锁的票绝不能出现在委托里")
        self.assertTrue(self.r["lock_conflicts"])
        self.assertEqual(self.r["lock_conflicts"][0]["code"], "600099")
        self.assertIn("unclaimed_account_position", self.r["lock_conflicts"][0]["reason"])
        self.assertEqual(self.d2["acct"].get("SHSE.600099"), 500, "被锁票的账户持仓不受影响")

    def test_t10_artifacts_written(self):
        """产物齐全（复盘要按文件名找）：fills / exec_report / sells_morning / sells_close / positions。"""
        f = self.r["book_files"]
        for pat in ("positions.json", "fills_%s.json" % self.d1["date"],
                    "sells_morning_%s.json" % self.d2["date"],
                    "sells_close_%s.json" % self.d2["date"],
                    "exec_report_%s.json" % self.d2["date"]):
            self.assertIn(pat, f, f"缺产物 {pat}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
