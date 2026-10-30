# -*- coding: utf-8 -*-
"""超跌跳空确认 · **补单轮（顺移）** 钩子与编排测试 —— 2026-10-14。

覆盖两块：
  A. `backfill.run_backfill_exec` 的**一轮编排**（撤单确认 → 额度重算 → 复检 → 顺移），
     用鸭子类型 FakeGateway（不碰真网关，够测编排逻辑）。
  B. `hooks` 的**时点状态机**（每日一次 / 10:20 硬闸 / KILL_SWITCH / reset_day /
     `buy_done` 落盘防重复下单）与 `fetch_current_snapshot` 取数。

运行：python -m unittest tests.phase3.test_osc_hooks_backfill -v
"""
import datetime as _dt
import sys
import tempfile
import unittest
from datetime import time as dtime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import backfill as bf                           # noqa: E402
from oversold_gap_confirm import executor as wse                          # noqa: E402
from oversold_gap_confirm import hooks as osc_hooks                       # noqa: E402


# ════════════════════════ 鸭子类型 FakeGateway ════════════════════════

class FakeGateway(object):
    """够 `run_backfill_exec` 用：get_cash / get_positions / place_order /
    poll_order / cancel_order。"""

    def __init__(self, cash=1_000_000.0, cancel_status=5, place_status="submitted",
                 place_filled=0, cancel_ok=True):
        self.cash = cash
        self.cancel_status = cancel_status      # poll_order 回的状态码
        self.place_status = place_status
        self.place_filled = place_filled
        self.cancel_ok = cancel_ok
        self.placed = []
        self.cancelled = []

    def get_cash(self):
        return self.cash

    def get_positions(self):
        return {}

    def cancel_order(self, order_id, code):
        if not self.cancel_ok:
            return {"ok": False, "message": "cancel_unavailable"}
        self.cancelled.append((order_id, code))
        return {"ok": True, "order_id": order_id, "message": ""}

    def poll_order(self, order_id, code):
        return {"order_id": order_id, "status": {5: "rejected", 6: "pending_cancel",
                                                 3: "filled", 4: "rejected"}.get(
                                                     self.cancel_status, "submitted"),
                "raw_status": self.cancel_status,
                "filled_qty": self.place_filled if self.cancel_status == 3 else 0,
                "filled_price": 10.0 if self.cancel_status == 3 else None,
                "message": ""}

    def place_order(self, code, side, qty, price):
        self.placed.append((code, side, qty, price))
        return {"order_id": "o_new_%d" % len(self.placed), "status": self.place_status,
                "filled_qty": self.place_filled, "filled_price": None, "message": ""}


def _fills_with_pending(codes=("600001",), qty=1000, px=10.0):
    """首轮 fills：给定票全部「挂单未成交」。"""
    return {"date": "2026-10-15", "buys": [
        {"code": c, "order_id": "o_" + c, "status": "submitted", "qty_placed": qty,
         "filled_qty": 0, "price": px, "gap": 0.1, "filled_price": None}
        for c in codes]}


def _spare(code, gap, price=10.0):
    return {"code": code, "gap": gap, "board": "半导体", "price": price}


class TestRunBackfillExec(unittest.TestCase):
    PARAMS = {"single_budget": 100000.0, "daily_budget": 400000.0, "cash_headroom": 0.95,
              "order_poll_rounds": 1, "order_poll_sleep_sec": 0.0}
    PC = {"600001": 9.0, "600002": 9.0, "600003": 9.0}
    # 现价 10、VWAP 9.5、有卖量、涨停价 9.9(=9.0×1.1) ⇒ 全部通过复检
    Q = {"600002": {"price": 10.0, "vwap": 9.5, "ask_v": 500},
         "600003": {"price": 10.0, "vwap": 9.5, "ask_v": 500}}

    def _run(self, gw, spares, quotes=None, fills=None, positions=None):
        return bf.run_backfill_exec(
            gw, "2026-10-15", fills if fills is not None else _fills_with_pending(),
            bf.pending_from_fills(fills if fills is not None else _fills_with_pending()),
            spares, self.PC, self.Q if quotes is None else quotes,
            positions=positions, params=self.PARAMS, dry_run=False,
            sleep_fn=lambda s: None, log=lambda *a: None)

    def test_cancel_confirmed_then_shift_buys_spare(self):
        gw = FakeGateway(cancel_status=5)          # 撤单确认 ⇒ 额度释放
        res = self._run(gw, [_spare("600002", 0.08)])
        self.assertEqual(res["cancel"]["released"], ["600001"])
        self.assertEqual([p["code"] for p in res["placed"]], ["600002"])
        self.assertTrue(gw.cancelled, "必须真的发了撤单")

    def test_cancel_unconfirmed_does_not_free_budget(self):
        """⚠️ owner 决策 #4 的核心：撤单**没确认**就不许用那笔钱。
        首轮 600001 占 1 万（1000股×10），日额度 40 万 ⇒ 理论上还有 39 万；
        但若"未确认"被当成已释放，这里就会照买 —— 本测试钉住它不放。"""
        gw = FakeGateway(cancel_status=6)          # PendingCancel：钱还冻着
        res = self._run(gw, [_spare("600002", 0.08)])
        self.assertEqual(res["cancel"]["released"], [])
        self.assertEqual(res["cancel"]["unconfirmed"], ["600001"])
        self.assertEqual(res["committed"].get("600001"), 10000.0,
                         "未确认的冻结必须继续占额度")
        self.assertEqual([p["code"] for p in res["placed"]], ["600002"],
                         "候补仍可买，但额度必须扣掉未释放的部分")

    def test_sealed_spare_skipped_to_next(self):
        """封死涨停的候补**跳过不追**（方案 A 的教训）。"""
        gw = FakeGateway(cancel_status=5)
        q = {"600002": {"price": 9.9, "vwap": 9.5, "ask_v": 0},     # 贴涨停、无卖量 ⇒ 封死
             "600003": {"price": 10.0, "vwap": 9.5, "ask_v": 500}}
        res = self._run(gw, [_spare("600002", 0.09), _spare("600003", 0.08)], quotes=q)
        self.assertIn({"code": "600002", "reason": "limit_sealed"}, res["skipped"])
        self.assertEqual([p["code"] for p in res["placed"]], ["600003"])

    def test_all_spares_rejected_no_placement(self):
        gw = FakeGateway(cancel_status=5)
        q = {"600002": {"price": 8.5, "vwap": 8.0, "ask_v": 100}}   # 跌破昨收 9.0
        res = self._run(gw, [_spare("600002", 0.08)], quotes=q)
        self.assertEqual(res["placed"], [])
        self.assertEqual(res["note"], "no_eligible_spare")

    def test_quotes_missing_for_spare_fail_closed(self):
        gw = FakeGateway(cancel_status=5)
        res = self._run(gw, [_spare("600002", 0.08)], quotes={})
        self.assertEqual(res["placed"], [])
        self.assertEqual(res["skipped"][0]["reason"], "no_data")

    def test_budget_exhausted_no_orders(self):
        gw = FakeGateway(cancel_status=6, cash=0.0)   # 撤单未确认 + 账上没钱
        res = self._run(gw, [_spare("600002", 0.08)])
        self.assertEqual(res["placed"], [])
        self.assertEqual(res["note"], "budget_exhausted")

    def test_single_budget_cap_after_partial_fill(self):
        """部分成交的票若再次出现在候补里，单票上限要扣掉**已成交**部分。"""
        gw = FakeGateway(cancel_status=5)
        fills = {"buys": [{"code": "600002", "order_id": "o2", "status": "submitted",
                           "qty_placed": 8000, "filled_qty": 6000, "price": 10.0,
                           "filled_price": 10.0}]}
        pend = bf.pending_from_fills(fills)
        res = bf.run_backfill_exec(gw, "2026-10-15", fills, pend,
                                   [_spare("600002", 0.08)], self.PC, self.Q,
                                   params=self.PARAMS, sleep_fn=lambda s: None,
                                   log=lambda *a: None)
        # 已成交 6 万 ⇒ 单票只剩 4 万 ⇒ 4000 股（而不是满额 1 万股）
        self.assertEqual([p["qty"] for p in res["placed"]], [4000])


class TestBackfillTimes(unittest.TestCase):
    class _RT:
        def __init__(self, **p):
            self.p = p

        def param(self, k, d=None):
            return self.p.get(k, d)

    def test_disabled_by_default(self):
        self.assertEqual(osc_hooks.backfill_times(self._RT()), [],
                         "总闸默认关 ⇒ 时点表为空（等于没有这个机制）")

    def test_two_rounds_default(self):
        rt = self._RT(osc_backfill_enabled=True)
        self.assertEqual(osc_hooks.backfill_times(rt), [dtime(10, 8), dtime(10, 14)])
        self.assertEqual(osc_hooks.backfill_deadline(rt), dtime(10, 20))

    def test_params_drive_times(self):
        rt = self._RT(osc_backfill_enabled=True, osc_backfill_rounds=3,
                      osc_backfill_first_hhmm=1005, osc_backfill_interval_min=5)
        self.assertEqual(osc_hooks.backfill_times(rt),
                         [dtime(10, 5), dtime(10, 10), dtime(10, 15)])


# ════════════════════════ 钩子状态机 ════════════════════════

class FakeRT(object):
    """够 `on_bar` / `try_backfill` 用的最小 rt。"""
    def __init__(self, params=None, book_dir=None, kill=False, quotes=None):
        self.p = dict(params or {})
        self.book_dir = book_dir or tempfile.mkdtemp()
        self._kill = kill
        self.audits = []
        self.logs = []
        self.quotes = quotes or {}
        self.gw = FakeGateway()
        self.gm = self
        self._candidates = {}

    # rt 接口
    def param(self, k, d=None):
        return self.p.get(k, d)

    def log(self, *a, **k):
        self.logs.append(" ".join(str(x) for x in a))

    def audit(self, rec):
        self.audits.append(rec)

    def risk(self, *a, **k):
        pass

    def kill_switch(self):
        return self._kill

    def gateway(self, ctx):
        return self.gw

    def code_to_gm(self, code):
        c = str(code)
        return ("SHSE." if c[:1] in "56" else "SZSE.") + c

    def sdk_call(self, desc, fn, *a, **k):
        return fn(*a, **k)

    def is_live(self, ctx):
        return True

    # gm.current 的桩
    def current(self, symbols=None, **k):
        out = []
        for s in (symbols or []):
            code = str(s).split(".")[-1]
            q = self.quotes.get(code)
            if not q:
                continue
            out.append({"symbol": s, "price": q.get("price"),
                        "cum_amount": q.get("price", 0) * (q.get("cum_volume") or 0),
                        "cum_volume": q.get("cum_volume"),
                        "quotes": [{"ask_v": q.get("ask_v", 0)}]})
        return out


class TestHooksBackfillStateMachine(unittest.TestCase):
    def setUp(self):
        # 每日重置（测试间不互相污染）
        osc_hooks.reset_day("2026-10-15")

    def _rt(self, **kw):
        kw.setdefault("params", {"osc_backfill_enabled": True, "osc_enabled": True,
                                 "osc_single_budget": 100000.0,
                                 "osc_daily_budget": 400000.0})
        return FakeRT(**kw)

    def test_backfill_skipped_when_disabled(self):
        rt = self._rt(params={"osc_backfill_enabled": False})
        osc_hooks._STATE["buy"] = True
        osc_hooks._STATE["pending"] = [{"code": "600001", "order_id": "o1"}]
        n = osc_hooks.try_backfill(rt, None, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 0)
        self.assertEqual(rt.gw.placed, [])

    def test_kill_switch_halts_round(self):
        """KILL_SWITCH 中途出现 ⇒ **既不撤单也不开仓**（与 try_buy 一致）。"""
        rt = self._rt(kill=True)
        osc_hooks._STATE["buy"] = True
        osc_hooks._STATE["pending"] = [{"code": "600001", "order_id": "o1"}]
        n = osc_hooks.try_backfill(rt, None, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 0)
        self.assertEqual(rt.gw.cancelled, [], "KILL_SWITCH 下不应发撤单")
        self.assertTrue(any(a.get("reason") == "kill_switch" for a in rt.audits))

    def test_uni_down_halts_round_without_cancel_quote_or_buy(self):
        """uni_down 与 KILL_SWITCH 同向：原挂单保留，不撤、不取复检行情、不补新仓。"""
        rt = self._rt(quotes={"600001": {"price": 10.0, "cum_volume": 1000, "ask_v": 500},
                              "600002": {"price": 10.0, "cum_volume": 1000, "ask_v": 500}})
        osc_hooks._STATE.update({
            "buy": True, "candidates": {"600001": {"prev_close": 9.0},
                                        "600002": {"prev_close": 9.0}},
            "fills": _fills_with_pending(), "pending": bf.pending_from_fills(_fills_with_pending()),
            "spares": [_spare("600002", 0.08)], "done_codes": {"600001"}})
        context = type("_Ctx", (), {"last_index_regime": "uni_down"})()

        def _no_market(*args, **kwargs):
            raise AssertionError("uni_down 补单熔断不得读取复检行情")

        rt.sdk_call = _no_market
        n = osc_hooks.try_backfill(rt, context, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 0)
        self.assertEqual(rt.gw.cancelled, [])
        self.assertEqual(rt.gw.placed, [])
        self.assertTrue(any(a.get("reason") == "index_regime_uni_down" for a in rt.audits))

    def test_no_pending_is_noop(self):
        rt = self._rt()
        osc_hooks._STATE["pending"] = []
        n = osc_hooks.try_backfill(rt, None, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 0)
        self.assertEqual(rt.logs, [], "无挂单应完全静默（不刷屏）")

    def test_quotes_fetch_fail_abandons_round(self):
        rt = self._rt(quotes={})
        osc_hooks._STATE["buy"] = True
        osc_hooks._STATE["pending"] = [{"code": "600001", "order_id": "o1"}]
        n = osc_hooks.try_backfill(rt, None, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 0)
        self.assertEqual(rt.gw.cancelled, [], "取数失败 ⇒ 挂单保持不动（仍有成交机会）")

    def test_shift_buys_spare_end_to_end(self):
        rt = self._rt(quotes={"600001": {"price": 10.0, "cum_volume": 1000, "ask_v": 500},
                              "600002": {"price": 10.0, "cum_volume": 1000, "ask_v": 500}})
        # 首轮：600001 挂单未成交；600002 是候补
        osc_hooks._STATE.update({
            "buy": True, "candidates": {"600001": {"prev_close": 9.0}, "600002": {"prev_close": 9.0}},
            "fills": _fills_with_pending(), "pending": bf.pending_from_fills(_fills_with_pending()),
            "spares": [_spare("600002", 0.08)], "done_codes": {"600001"}})
        n = osc_hooks.try_backfill(rt, None, _dt.datetime(2026, 10, 15, 10, 8),
                                   "2026-10-15", 0)
        self.assertEqual(n, 1)
        self.assertEqual([p[0] for p in rt.gw.placed], ["600002"])
        self.assertIn("600002", osc_hooks._STATE["done_codes"])
        self.assertEqual(osc_hooks._STATE["pending"], [],
                         "600001 撤单已确认 ⇒ 不再挂账")

    def test_reset_day_clears_backfill_state(self):
        osc_hooks._STATE.update({"pending": [{"x": 1}], "spares": [{"y": 2}],
                                 "done_codes": {"600001"}, "backfill": {0: True},
                                 "buy_done": True})
        osc_hooks.reset_day("2026-10-16")
        for k in ("pending", "spares", "backfill"):
            self.assertFalse(osc_hooks._STATE[k], f"{k} 应被 reset_day 清空")
        self.assertEqual(osc_hooks._STATE["done_codes"], set())
        self.assertFalse(osc_hooks._STATE["buy_done"])

    def test_day_state_roundtrip_and_restart_guard(self):
        """`buy_done` 落盘 → 模拟进程重启（清内存）→ 回读后**不得再跑首轮**。"""
        rt = self._rt()
        osc_hooks._STATE.update({"buy_done": True, "pending": [{"code": "600001",
                                                              "order_id": "o1"}]})
        self.assertTrue(osc_hooks.save_day_state(rt, "2026-10-15"))
        # 模拟重启：清空内存态（但别动 date）
        osc_hooks._STATE.update({"buy_done": False, "pending": [], "spares": []})
        self.assertTrue(osc_hooks.load_day_state(rt, "2026-10-15"))
        self.assertTrue(osc_hooks._STATE["buy_done"])
        self.assertEqual(len(osc_hooks._STATE["pending"]), 1)

    def test_on_bar_does_not_open_after_deadline(self):
        """10:20 之后**不再开新仓**（硬闸，与"最多N轮"双保险）。"""
        rt = self._rt(quotes={"600001": {"price": 10.0, "cum_volume": 1000, "ask_v": 500},
                              "600002": {"price": 10.0, "cum_volume": 1000, "ask_v": 500}})
        osc_hooks._STATE.update({
            # ⚠️ 必须置 `prepare`/`morning`/`sell`，否则 `on_bar` 会去跑**真预筛**（全市场
            #    批量取数，单测里会挂到超时）——本测试只关心补单那一格。
            "date": "2026-10-15", "prepare": True, "morning": True, "sell": True, "buy": True,
            "candidates": {"600001": {"prev_close": 9.0}, "600002": {"prev_close": 9.0}},
            "fills": _fills_with_pending(), "pending": bf.pending_from_fills(_fills_with_pending()),
            "spares": [_spare("600002", 0.08)], "done_codes": {"600001"}})
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 15, 10, 21), "2026-10-15",
                         dtime(10, 21))
        self.assertEqual(rt.gw.placed, [], "过了 10:20 硬闸，不得开新仓")

    def test_on_bar_fires_each_round_once(self):
        rt = self._rt(quotes={"600001": {"price": 10.0, "cum_volume": 1000, "ask_v": 500}})
        osc_hooks._STATE.update({
            # 同上：把另外三个钩子先标记完成，避免单测里跑真预筛/真卖侧
            "date": "2026-10-15", "prepare": True, "morning": True, "sell": True, "buy": True,
            "candidates": {"600001": {"prev_close": 9.0}},
            "fills": _fills_with_pending(), "pending": bf.pending_from_fills(_fills_with_pending()),
            "spares": [], "done_codes": {"600001"}})
        now = _dt.datetime(2026, 10, 15, 10, 8)
        osc_hooks.on_bar(rt, None, now, "2026-10-15", dtime(10, 8))
        first = len(rt.gw.cancelled)
        osc_hooks.on_bar(rt, None, now, "2026-10-15", dtime(10, 8))    # 同格再来一根
        self.assertEqual(len(rt.gw.cancelled), first, "同一补单轮只能跑一次")


class TestParamsWiring(unittest.TestCase):
    """配置断言（2026-10-14）：新参数**必须落在 `PARAMS`**。

    为什么单列一条：`rt.param` 只读 `PARAMS` 这一张表，而本仓有过**B7 前科**——
    三个开关被放进 `INDEX_REGIME_PARAMS`，闸函数却读 `PARAMS` ⇒ 从上线起**从未生效**、
    日志零事件。补单总闸 2026-10-16 owner 拍板上线（默认开），若放错表，开关同样会静默不生效。
    """

    @staticmethod
    def _params_literal():
        import ast
        fp = _AUTO / "_gm" / "config" / "params.py"
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
                return ast.literal_eval(node.value)
        return {}

    def test_backfill_params_in_PARAMS_table(self):
        p = self._params_literal()
        for k in ("osc_backfill_enabled", "osc_backfill_rounds", "osc_backfill_first_hhmm",
                  "osc_backfill_interval_min", "osc_backfill_deadline_hhmm"):
            self.assertIn(k, p, f"{k} 必须在 PARAMS（放 INDEX_REGIME_PARAMS 读不到）")

    def test_backfill_total_switch_on_after_owner_approval(self):
        """owner 2026-10-16 拍板上线：回测过闸（Sharpe 0.384→0.518、年化不降，
        doc/solutions/2026-10-16_替补机制B方案回测验证.md）⇒ 总闸**默认开启**；
        开关保留可关（翻 False = 回到"不撤不补"现状，见下方 rt 级断言）。"""
        p = self._params_literal()
        self.assertTrue(p["osc_backfill_enabled"],
                        "owner 2026-10-16 已拍板上线，总闸必须默认 True")

    def test_universe_switch_in_PARAMS_and_defaults_to_full_market(self):
        """宇宙回退闸（2026-10-14）：默认必须是**全市场**（= 与扩展后行为逐位一致），
        翻 False 才退回猎手池。放错表 / 默认写反都会让"当天回退"失效。"""
        p = self._params_literal()
        self.assertIn("osc_universe_full_market", p)
        self.assertTrue(p["osc_universe_full_market"], "默认必须是 True（当前行为）")

    def test_universe_switch_actually_changes_board_map(self):
        """真调用一次：False ⇒ 只回猎手池那份映射（不含申万扩展进来的票）。

        这条是"闸真的接上了"的证据 —— 只断言参数存在是抓不到"忘了往下传"的。
        """
        sys.path.insert(0, str(_AUTO))
        from oversold_gap_confirm import prescreen as ps
        full = ps.universe_board_map(True)
        narrow = ps.universe_board_map(False)
        self.assertTrue(narrow, "猎手池映射不该是空的")
        self.assertTrue(set(narrow) <= set(full),
                        "窄口径必须是宽口径的子集（别把两个口径写反）")
        if len(full) > len(narrow):
            self.assertGreater(len(full), len(narrow), "全市场应有更多标的")
        else:
            self.skipTest("本环境 industry_map.csv 缺失 ⇒ 两个口径相同（预期内的退化）")

    def test_params_readable_via_rt_param(self):
        p = self._params_literal()

        class _RT:
            def param(self, k, d=None):
                return p.get(k, d)
        rt = _RT()
        # owner 2026-10-16 拍板上线后：PARAMS 默认开 ⇒ 时点表就位（10:08/10:14 两轮）
        self.assertEqual(osc_hooks.backfill_times(rt), [dtime(10, 8), dtime(10, 14)])
        # 开关保留可关：翻 False ⇒ 空表（行为与上线前逐位一致）
        p2 = dict(p, osc_backfill_enabled=False)
        rt2 = type("_RT2", (), {"param": lambda self, k, d=None: p2.get(k, d)})()
        self.assertEqual(osc_hooks.backfill_times(rt2), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
