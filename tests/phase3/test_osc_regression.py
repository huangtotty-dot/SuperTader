# -*- coding: utf-8 -*-
"""超跌跳空确认 · 冻结口径回归测试 —— 2026-10-12 补（原名「弱转强」）。

**为什么现在补**：owner 决策「启超跌跳空确认 / 关 S1」，而超跌跳空确认此前**没有任何专属单测**
（全仓只有一条配置断言）。要在它身上动刀（改名 + 删 OGR/旧引擎），必须先有行为兜底；
有了它，「改名」与「删除」两件事才有判据（改动前后这份测试都必须绿）。

**钉住的是"冻结口径"**（`oversold_gap_confirm/decision.py` 文档串自述：改动常量/信号口径 = 换策略，
须重做 exp 系列检验）：

  W1 超跌: prev_close < MA20
  10:00 棒: low ≥ prev_close（缺口未回补）∧ close > prev_close ∧ close > VWAP
  ⇒ 10:00 收盘买入；按 gap 降序取 top-N；`pool_n < MIN_POOL` fail-closed
  卖侧三件套: 次日早盘冲高 +3% 卖 / **SL5 只对深超跌(dev20<−7%)** / 满 5 交易日到期平仓
  额度闸: 单票 ≤10 万、单票占用现金 ≤ 可用×0.95、整手 100

运行：python -m unittest tests.phase3.test_osc_regression -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 2026-10-12 策略归拢：实现全部在 `execution/auto/oversold_gap_confirm/` 包内
from oversold_gap_confirm.decision import MIN_POOL, signal_ok, evaluate   # noqa: E402
from oversold_gap_confirm import executor as wse                     # noqa: E402


def _bar(low=11.0, close=11.5, amount=11.4 * 1000, volume=1000):
    """一根 09:30~10:00 的棒；默认 VWAP = 11.4 < close ⇒ 站上均价。"""
    return {"low": low, "close": close, "amount": amount, "volume": volume}


class TestSignalFrozenRule(unittest.TestCase):
    """A. 单票信号：四个条件缺一不可。"""

    def test_all_conditions_met(self):
        ok, why, gap = signal_ok(10.0, 12.0, _bar())
        self.assertTrue(ok, why)
        self.assertAlmostEqual(gap, 0.15, places=6)      # 11.5/10.0 − 1

    def test_not_weak_when_prev_close_above_ma20(self):
        ok, why, _ = signal_ok(13.0, 12.0, _bar(low=13.0, close=13.5, amount=13.4 * 1000))
        self.assertFalse(ok)
        self.assertIn("not_weak", why, "W1 超跌是硬前提：昨收必须 < MA20")

    def test_gap_must_be_held_low_ge_prev_close(self):
        """⚠️ 核心条件：高开后**回补缺口**（low < 昨收）即否决 —— 哪怕收盘仍高于昨收。"""
        ok, why, _ = signal_ok(10.0, 12.0, _bar(low=9.9, close=11.5))
        self.assertFalse(ok)
        self.assertIn("gap_not_held", why)

    def test_must_close_above_prev_close(self):
        ok, why, _ = signal_ok(10.0, 12.0, _bar(low=10.0, close=10.0, amount=9.9 * 1000))
        self.assertFalse(ok)
        self.assertIn("not_above_prev_close", why)

    def test_must_close_above_vwap(self):
        """站上分时均价（VWAP=amount/volume）才是"强"。"""
        ok, why, _ = signal_ok(10.0, 12.0, _bar(low=10.0, close=11.0, amount=11.5 * 1000))
        self.assertFalse(ok)
        self.assertIn("not_above_vwap", why)

    def test_missing_inputs_fail_closed(self):
        for args in ((None, 12.0, _bar()), (10.0, 12.0, None), (10.0, 12.0, {}),
                     (10.0, 12.0, {"low": 11.0})):      # 缺 close
            ok, why, gap = signal_ok(*args)
            self.assertFalse(ok, f"缺输入必须 fail-closed: {args}")
            self.assertFalse(gap == gap and gap > 0, "缺失时 gap 应为 nan")


class TestPoolEvaluate(unittest.TestCase):
    """B. 池级：排序 / top-N / fail-closed / 板块过滤。"""

    @staticmethod
    def _pool(n=5):
        pc, ma, bars = {}, {}, {}
        for i in range(n):
            c = f"60{i:04d}"
            pc[c] = 10.0
            ma[c] = 12.0                      # 全部超跌
            # gap 递增：close = 11.0 + i*0.1
            # ⚠️ VWAP = amount/volume 必须**低于** close，否则 `close > vwap` 不成立、无信号
            bars[c] = _bar(low=10.0, close=11.0 + i * 0.1,
                           amount=(11.0 + i * 0.1 - 0.1) * 1000)
        return pc, ma, bars

    def test_sorted_by_gap_desc_and_top_n(self):
        pc, ma, bars = self._pool(5)
        r = evaluate(pc, ma, bars, top_n=2)
        self.assertEqual([p["code"] for p in r["picks"]], ["600004", "600003"],
                         "按 gap 降序取前 N")

    def test_fail_closed_when_pool_too_small(self):
        pc, ma, bars = self._pool(MIN_POOL - 1)
        r = evaluate(pc, ma, bars, top_n=4)
        self.assertEqual(r["picks"], [], f"池 < MIN_POOL({MIN_POOL}) ⇒ 宁可不买")

    def test_top_n_non_positive(self):
        pc, ma, bars = self._pool(5)
        self.assertEqual(evaluate(pc, ma, bars, top_n=0)["picks"], [])

    def test_board_filter_excludes_and_is_observable(self):
        pc, ma, bars = self._pool(3)
        board = {"600000": "半导体", "600001": "电力", "600002": "半导体"}
        r = evaluate(pc, ma, bars, board=board, allowed_boards=["半导体"], top_n=4)
        self.assertEqual({p["code"] for p in r["picks"]}, {"600000", "600002"})
        self.assertEqual(r["rejected_board"], 1, "被过滤的信号仍要出现在 signals 里（可观测）")
        self.assertTrue(any(s["reason"] == "board_filtered" for s in r["signals"]))

    def test_board_filter_off_by_default(self):
        pc, ma, bars = self._pool(3)
        for allow in (None, []):
            r = evaluate(pc, ma, bars, board={"600000": "半导体"},
                         allowed_boards=allow, top_n=4)
            self.assertEqual(len(r["picks"]), 3, "allowed_boards 为空 ⇒ 不过滤")

    def test_n_tradable_and_signals_relation(self):
        """2026-10-14 补：`n_tradable` / `signals` / `picks` 三者的关系**此前零覆盖**，
        而买不进替补机制正是靠 `signals`（全排序候选）取顺移候补 ⇒ 必须先钉死。

        不变量：`signals ⊇ 可交易候选`；`n_tradable == #{signals | decision=='trade'}`；
        `picks == 可交易候选的前 top_n`。
        """
        pc, ma, bars = self._pool(5)
        # 4 只落在允许板块内、1 只被过滤 ⇒ top_n=2 后还剩 2 只候补（顺移要有米下锅）
        board = {"600000": "半导体", "600001": "半导体", "600002": "半导体",
                 "600003": "半导体", "600004": "电力"}
        r = evaluate(pc, ma, bars, board=board, allowed_boards=["半导体"], top_n=2)
        tradable = [s for s in r["signals"] if s["decision"] == "trade"]
        self.assertEqual(r["n_tradable"], len(tradable))
        self.assertEqual([p["code"] for p in r["picks"]],
                         [s["code"] for s in tradable[:2]])
        # 顺移候补 = signals 里去掉 picks 之后的可交易候选（gap 降序）
        spare_codes = [s["code"] for s in tradable[2:]]
        self.assertTrue(spare_codes, "本 fixture 应留下候补，否则顺移无米下锅")
        gaps = [s["gap"] for s in tradable]
        self.assertEqual(gaps, sorted(gaps, reverse=True), "signals 必须 gap 降序")


class TestBuyBudgetGates(unittest.TestCase):
    """C. 额度闸：单票 10 万 / 现金×0.95 / 整手 / T+1 / 无价。"""

    def _picks(self, *codes, px=10.0):
        return [{"code": c, "gap": 0.1, "price": px} for c in codes]

    def test_single_budget_caps_qty(self):
        plan = wse.build_buy_plan(self._picks("600000"), {}, cash_budget=1e9,
                                  single_budget=100000.0)
        self.assertEqual(plan[0]["qty_target"], 10000)          # 10万 / 10元
        self.assertEqual(plan[0]["qty_place"], 10000)

    def test_cash_headroom_caps_before_single_budget(self):
        """现金只有 5 万 ⇒ 单票占用封顶 5万×0.95=4.75万 ⇒ 4700 股（不是 1 万股）。"""
        plan = wse.build_buy_plan(self._picks("600000"), {}, cash_budget=50000.0,
                                  single_budget=100000.0, cash_headroom=0.95)
        self.assertEqual(plan[0]["qty_place"], 4700)

    def test_integral_lot_100(self):
        plan = wse.build_buy_plan(self._picks("600000", px=33.3), {}, cash_budget=1e9,
                                  single_budget=100000.0)
        self.assertEqual(plan[0]["qty_place"] % 100, 0, "必须整手")

    def test_cash_consumed_sequentially(self):
        """逐票扣减：第 2 票只能吃剩下的钱。"""
        plan = wse.build_buy_plan(self._picks("600000", "600001"), {}, cash_budget=120000.0,
                                  single_budget=100000.0)
        # 票1: min(单票10万, 12万×0.95=11.4万) = 10万 ⇒ 10000 股，余 2万
        # 票2: min(单票10万, 剩余2万×0.95=1.9万) = 1.9万 ⇒ 1900 股（被现金而非单票上限截断）
        self.assertEqual(plan[0]["qty_place"], 10000)
        self.assertEqual(plan[1]["qty_place"], 1900, "第 2 票必须被**剩余现金**截断")
        self.assertEqual(plan[1]["truncated"], False, "1.9万/10元=1900 正好整手 ⇒ 不算截断")

    def test_t1_locked_defers(self):
        pos = {"600000": {"qty": 1000, "available": 0}}
        plan = wse.build_buy_plan(self._picks("600000"), pos, cash_budget=1e6,
                                  single_budget=100000.0)
        self.assertEqual(plan[0]["qty_place"], 0)
        self.assertIn("t1_locked", plan[0]["defer_reason"])

    def test_no_price_defers(self):
        plan = wse.build_buy_plan([{"code": "600000", "gap": 0.1, "price": 0}], {},
                                  cash_budget=1e6, single_budget=100000.0)
        self.assertEqual(plan[0]["qty_place"], 0)
        self.assertIn("no_price", plan[0]["defer_reason"])

    def test_code_committed_subtracts_single_budget(self):
        """2026-10-14（买不进替补）：`code_committed` 扣的是**单票**额度 ——
        顺移时同一票今日已下过单，不能再给它满额。"""
        picks = self._picks("600000", px=10.0)          # 满额 10 万 ⇒ 10000 股
        base = wse.build_buy_plan(picks, {}, cash_budget=1e9, single_budget=100000.0)
        self.assertEqual(base[0]["qty_place"], 10000)
        # 已占用 6 万 ⇒ 只剩 4 万 ⇒ 4000 股
        part = wse.build_buy_plan(picks, {}, cash_budget=1e9, single_budget=100000.0,
                                  code_committed={"600000": 60000.0})
        self.assertEqual(part[0]["qty_place"], 4000)
        # 已占满 ⇒ 一股不买（且要显式记账，不是静默跳过）
        full = wse.build_buy_plan(picks, {}, cash_budget=1e9, single_budget=100000.0,
                                  code_committed={"600000": 100000.0})
        self.assertEqual(full[0]["qty_place"], 0)
        self.assertTrue(full[0]["defer_reason"])

    def test_code_committed_default_none_unchanged(self):
        """默认 None ⇒ 与加该参数**之前逐位一致**（防回归）。"""
        picks = self._picks("600000", "600001", px=10.0)
        a = wse.build_buy_plan(picks, {}, cash_budget=150000.0, single_budget=100000.0)
        b = wse.build_buy_plan(picks, {}, cash_budget=150000.0, single_budget=100000.0,
                               code_committed=None)
        self.assertEqual(a, b)
        # 首票吃满单票上限 10 万（10000 股）；余 5 万现金 ×0.95 = 47500 ⇒ 4700 股
        self.assertEqual([r["qty_place"] for r in a], [10000, 4700])


class TestEvaluateFullAndSparesStamp(unittest.TestCase):
    """2026-10-14：`evaluate_full` 保留全排序候选 + `_write_fills` 落 spares。"""

    @staticmethod
    def _pc_ma_bars(n=6):
        pc, ma, bars = {}, {}, {}
        for i in range(n):
            c = f"60{i:04d}"
            pc[c], ma[c] = 10.0, 12.0
            bars[c] = _bar(low=10.0, close=11.0 + i * 0.1,
                           amount=(11.0 + i * 0.1 - 0.1) * 1000)
        return pc, ma, bars

    def test_evaluate_full_keeps_signals_and_matches_picks(self):
        pc, ma, bars = self._pc_ma_bars()
        full = wse.evaluate_full(pc, ma, bars, top_n=2)
        self.assertEqual([p["code"] for p in full["picks"]],
                         [p["code"] for p in wse.evaluate_picks(pc, ma, bars, top_n=2)])
        self.assertEqual(len(full["signals"]), 6, "signals 必须是全排序候选，不能只有 top-N")
        self.assertEqual(full["n_tradable"], 6)

    def test_write_fills_stamps_spares(self):
        """spares 必须落进 fills —— 进程重启后补单钩子靠它回读（否则挂单冻结到 15:00）。"""
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = wse._write_fills("2026-10-15", "dry", "t0", [], [], 1e6, 0.0,
                                 dict(wse.DEFAULTS), td, note="x",
                                 spares=[{"code": "600005", "gap": 0.06,
                                          "board": "半导体", "price": 11.5}])
            self.assertEqual(f["spares"][0]["code"], "600005")
            with open(f["_paths"]["fills"], encoding="utf-8") as _fh:
                on_disk = _json.loads(_fh.read())
            self.assertEqual(on_disk["spares"][0]["price"], 11.5)
            # 不传 spares ⇒ 落空表（向后兼容：老 fills 没有这个键，新代码读时按空处理）
            f2 = wse._write_fills("2026-10-15", "dry", "t0", [], [], 1e6, 0.0,
                                  dict(wse.DEFAULTS), td)
            self.assertEqual(f2["spares"], [])


class TestSellTrio(unittest.TestCase):
    """D. 卖侧三件套（exp19 owner 拍板 SL5 只深）。"""

    @staticmethod
    def _book(entry_date="2026-10-09", entry_price=10.0, dev20=-0.20, days_held=1):
        return {"600000": {"entry_date": entry_date, "entry_price": entry_price,
                           "dev20": dev20, "days_held": days_held}}

    def test_deep_oversold_stops_out(self):
        plan = wse.build_sell_plan(self._book(dev20=-0.20), {"600000": 9.4}, "2026-10-12")
        self.assertTrue(plan[0]["stop_hit"])
        self.assertEqual(plan[0]["reason"], "sl5_deep")

    def test_deep_oversold_not_stopped_above_line(self):
        plan = wse.build_sell_plan(self._book(dev20=-0.20), {"600000": 9.6}, "2026-10-12")
        self.assertFalse(plan[0]["stop_hit"], "深档跌破 −5% 才止损（9.6 = −4%）")

    def test_shallow_never_stops_out(self):
        """⚠️ owner 拍板的关键规则：**浅/中超跌不挂止损** —— 即使跌破 −5% 也持有到期。"""
        plan = wse.build_sell_plan(self._book(dev20=-0.03), {"600000": 8.0}, "2026-10-12")
        self.assertFalse(plan[0]["stop_hit"], "dev20 ≥ −7% ⇒ 不挂止损，只等到期")
        self.assertEqual(plan[0]["reason"], "hold")

    def test_horizon_expiry_takes_precedence(self):
        """满 5 个交易日 ⇒ 到期平仓；即便同日也满足止损条件，reason 仍是 horizon_expired。"""
        plan = wse.build_sell_plan(self._book(dev20=-0.20, days_held=5),
                                   {"600000": 8.0}, "2026-10-12")
        self.assertTrue(plan[0]["expired"])
        self.assertEqual(plan[0]["reason"], "horizon_expired")

    def test_t1_same_day_never_sells(self):
        plan = wse.build_sell_plan(self._book(entry_date="2026-10-12"), {"600000": 8.0},
                                   "2026-10-12")
        self.assertEqual(plan[0]["reason"], "t1_same_day")

    def test_missing_price_holds(self):
        plan = wse.build_sell_plan(self._book(), {}, "2026-10-12")
        self.assertEqual(plan[0]["reason"], "no_price")

    def test_morning_pop_sell_next_day(self):
        """次日早盘冲高 +3% ⇒ 卖出（D+1 早盘；days_held 早盘时仍为 0）。"""
        plan = wse.build_morning_sell_plan(self._book(days_held=0), {"600000": 10.35},
                                           "2026-10-12")
        self.assertEqual(plan[0]["reason"], "pop_sell")

    def test_morning_pop_not_triggered_below_target(self):
        plan = wse.build_morning_sell_plan(self._book(days_held=0), {"600000": 10.25},
                                           "2026-10-12")
        self.assertEqual(plan[0]["reason"], "hold", "+2.5% 未达 +3% 目标")

    def test_morning_sell_only_for_next_day(self):
        for bk, why in ((self._book(entry_date="2026-10-12", days_held=0), "t1_same_day"),
                        (self._book(days_held=2), "not_next_day")):
            plan = wse.build_morning_sell_plan(bk, {"600000": 12.0}, "2026-10-12")
            self.assertEqual(plan[0]["reason"], why)


class TestSymbolHelpers(unittest.TestCase):
    def test_code_of(self):
        self.assertEqual(wse.code_of("SHSE.600000"), "600000")
        self.assertEqual(wse.code_of("600000.SH"), "600000")
        self.assertEqual(wse.code_of("600000"), "600000")
        self.assertEqual(wse.code_of(""), "")
        self.assertEqual(wse.code_of("abc"), "")

    def test_gm_symbol_of(self):
        self.assertEqual(wse.gm_symbol_of("600000"), "SHSE.600000")
        self.assertEqual(wse.gm_symbol_of("000001"), "SZSE.000001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
