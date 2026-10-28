# -*- coding: utf-8 -*-
"""超跌跳空确认 · 每日选股记录落盘（2026-10-17）。

**为什么需要**：owner 要 GUI 能回答「每天早上候选了多少、最后选了哪些、**原因是什么**」。
在此之前，逐只原因（`decision.evaluate` 的 `diag`）出过内存就没了 —— 只把漏斗和最接近的
6 只打进日志。本测试钉住两件事：

  ① `diag['failures']` 是**全量**未过票（含「10:00 棒缺失」那批，主循环会提前 continue），
     且**纯附加**：`signals` / `picks` / `n_tradable` / `pool_n` 一个都不许变；
  ② `_write_decision` 在 **no-picks 与正常两条路径**都能产出文件，NaN 不会写出非法 JSON，
     写失败**绝不抛**（这条是 fail-closed 的安全底线：留痕炸了也不能让当日不下单）。

运行：python -m unittest tests.phase3.test_osc_decision_record -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import executor as ex                     # noqa: E402
from oversold_gap_confirm.decision import evaluate                  # noqa: E402


def _bar(low=11.0, close=11.5, amount=11.4 * 1000, volume=1000):
    """VWAP = 11.4 < close ⇒ 站上均价。"""
    return {"low": low, "close": close, "amount": amount, "volume": volume}


class TestFailuresCompleteness(unittest.TestCase):
    """A. 逐只原因必须覆盖**盘前候选**，而不是只覆盖拿到了 10:00 棒的票。"""

    def _run(self):
        prev_close = {
            "A00001": 10.0,     # 三关全过
            "B00002": 10.0,     # ①破昨收（low < prev_close）
            "C00003": 10.0,     # ②未站上昨收
            "D00004": 10.0,     # ③未站上 VWAP
            "E00005": 10.0,     # 10:00 棒缺失（不进主循环）
        }
        ma20 = {c: 12.0 for c in prev_close}          # 全部超跌
        bars = {
            "A00001": _bar(low=10.1, close=11.5),
            "B00002": _bar(low=9.9, close=11.5),
            "C00003": _bar(low=10.1, close=9.8),
            "D00004": _bar(low=10.1, close=11.5, amount=11.9 * 1000),   # VWAP 11.9 > close
        }
        return evaluate(prev_close, ma20, bars, board={}, top_n=2)

    def test_failures_are_complete(self):
        r = self._run()
        f = r["diag"]["failures"]
        codes = {x["code"]: x for x in f}
        self.assertIn("B00002", codes)
        self.assertIn("C00003", codes)
        self.assertIn("D00004", codes)
        self.assertIn("E00005", codes, "棒缺失的候选也必须出现在逐只名单里")
        self.assertEqual(codes["E00005"]["reason"], "bar_missing")
        self.assertEqual(r["diag"]["n_failures"], 4)
        # 逐关布尔值供 GUI 展示"卡在第几关"
        self.assertFalse(codes["B00002"]["gates"]["g1"])
        self.assertTrue(codes["B00002"]["gates"]["weak"])
        self.assertIsNone(codes["E00005"]["gates"])

    def test_failures_purely_additive(self):
        """加 failures 前后，判定结果必须逐位一致（这里直接钉死期望值）。"""
        r = self._run()
        self.assertEqual(r["pool_n"], 4, "棒缺失的票不计入 pool_n")
        self.assertEqual([p["code"] for p in r["picks"]], ["A00001"])
        self.assertEqual(r["n_tradable"], 1)
        self.assertEqual(r["rejected_board"], 0)

    def test_nan_is_sanitized(self):
        """NaN 不许原样落到 JSON 里（json.dump 会把 NaN 写成非法字面量）。"""
        r = self._run()
        for x in r["diag"]["failures"]:
            for k in ("gap", "low", "close", "vwap", "prev_close", "ma20"):
                v = x.get(k)
                self.assertTrue(v is None or isinstance(v, (int, float)), (k, v))
                if isinstance(v, float):
                    self.assertEqual(v, v, "%s 是 NaN" % k)


class TestWritePrescreen(unittest.TestCase):
    """C. 盘前候选**名单**落盘（此前只有审计里的一个数量，名单只在内存）。"""

    def _rt(self, book_dir):
        class _RT:
            log = staticmethod(lambda *a, **k: None)

        r = _RT()
        r.book_dir = book_dir
        return r

    def test_writes_candidates(self):
        from oversold_gap_confirm import hooks
        cand = {"603168": {"prev_close": 5.67, "ma20": 6.1, "board": "医药生物", "name": "莎普爱思"},
                "600127": {"prev_close": 6.5, "ma20": 7.0, "board": "大农业", "name": "金健米业"}}
        with tempfile.TemporaryDirectory() as td:
            fp = hooks._write_prescreen(self._rt(td), "2026-10-17", cand,
                                        {"universe": "full_market", "board_top_n": 5})
            self.assertTrue(fp and Path(fp).exists())
            d = json.loads(Path(fp).read_text(encoding="utf-8"))
            self.assertEqual(d["candidates_n"], 2)
            self.assertEqual(d["universe"], "full_market")
            self.assertEqual(d["board_top_n"], 5)
            self.assertEqual(d["candidates"]["603168"]["name"], "莎普爱思")

    def test_write_failure_never_raises(self):
        from oversold_gap_confirm import hooks
        fp = hooks._write_prescreen(self._rt("\x00bad"), "2026-10-17", {"X": {}}, {})
        self.assertIsNone(fp)


class TestWriteDecision(unittest.TestCase):
    """B. 落盘：两条分支都写、可读、不含 NaN、失败不抛。"""

    def _full(self):
        return {
            "pool_n": 3, "n_tradable": 1, "rejected_board": 0,
            "signals": [{"code": "A00001", "gap": 0.15, "board": "医药",
                         "decision": "trade", "reason": "ok"}],
            "picks": [],
            "diag": {"gates": {"weak": 3, "g1": 2, "g2": 1, "g3": 1, "all": 1},
                     "reasons": {"not_above_vwap": 2},
                     "near": [], "n_near": 2, "n_failures": 2,
                     "failures": [
                         {"code": "B00002", "board": "医药", "gap": float("nan"),
                          "reason": "bar_missing", "gates": None,
                          "low": None, "close": None, "vwap": None,
                          "prev_close": 10.0, "ma20": None},
                         {"code": "C00003", "board": "农业", "gap": -0.02,
                          "reason": "not_above_vwap",
                          "gates": {"weak": True, "g1": True, "g2": True, "g3": False},
                          "low": 10.1, "close": 11.5, "vwap": 11.9,
                          "prev_close": 10.0, "ma20": 12.0},
                     ]},
        }

    def test_writes_no_picks_path(self):
        full = self._full()          # picks 为空 = no-picks 分支
        with tempfile.TemporaryDirectory() as td:
            fp = ex._write_decision("2026-10-17", "live", "2026-10-17 10:00:00",
                                    full, [], [], prev_close={}, ma20={},
                                    board={}, names={"A00001": "测试股"},
                                    book_dir=td, prescreen_meta={"universe": "full_market",
                                                                 "board_top_n": 5},
                                    note="no_picks", log=lambda *_: None)
            self.assertTrue(fp and Path(fp).exists())
            d = json.loads(Path(fp).read_text(encoding="utf-8"))
            self.assertEqual(d["note"], "no_picks")
            self.assertEqual(d["funnel"]["pool_n"], 3)
            self.assertEqual(d["funnel"]["n_failures"], 2)
            self.assertEqual(d["premarket"]["universe"], "full_market")
            self.assertEqual([s["code"] for s in d["picks"]], [])
            # 逐只明细：2 只未过 + 1 只触发但未入 picks（无 spares 时不在明细里）
            self.assertEqual({s["code"] for s in d["stocks"]}, {"B00002", "C00003"})
            self.assertEqual(d["reason_counts"], {"not_above_vwap": 2})
            # NaN 必须已清洗（文件能正常解析即证明不是 NaN 字面量）
            self.assertIsNone(next(s for s in d["stocks"] if s["code"] == "B00002")["gap"])

    def test_writes_normal_path_with_spares(self):
        full = self._full()
        picks = [{"code": "A00001", "gap": 0.15, "board": "医药"}]
        spares = [{"code": "Z00009", "gap": 0.01, "board": "医药", "price": 10.5}]
        with tempfile.TemporaryDirectory() as td:
            fp = ex._write_decision("2026-10-17", "live", "2026-10-17 10:00:00",
                                    full, picks, spares, prev_close={}, ma20={},
                                    board={}, names={"A00001": "测试股", "Z00009": "候补股"},
                                    book_dir=td, prescreen_meta=None, note="",
                                    log=lambda *_: None)
            d = json.loads(Path(fp).read_text(encoding="utf-8"))
            self.assertEqual([p["code"] for p in d["picks"]], ["A00001"])
            self.assertEqual(d["picks"][0]["status"], "picked")
            self.assertEqual([s["code"] for s in d["spares"]], ["Z00009"])
            st = {s["code"]: s["status"] for s in d["stocks"]}
            self.assertEqual(st["A00001"], "picked")
            self.assertEqual(st["Z00009"], "spare")
            self.assertEqual(st["C00003"], "failed")
            # gap 降序：A(0.15) > Z(0.01) > C(-0.02) > B(None 垫底)
            self.assertEqual([s["code"] for s in d["stocks"]],
                             ["A00001", "Z00009", "C00003", "B00002"])

    def test_write_failure_never_raises(self):
        """留痕写失败绝不能让当日不下单 ⇒ 传一个不可写路径，必须返回 None 而不抛。"""
        fp = ex._write_decision("2026-10-17", "live", "t", self._full(), [], [],
                                book_dir="\x00bad", log=lambda *_: None)
        self.assertIsNone(fp)


class TestRunBuyExecWiresIt(unittest.TestCase):
    """D. 接线：`run_buy_exec` 的两条分支都必须真的落下 decision 文件。"""

    def _run(self, with_bars):
        from tests.phase3.test_osc_backfill_shift import FakeGateway
        gw = FakeGateway(cash=1_000_000.0, place_status="submitted")
        ok_bar = _bar(low=10.1, close=11.5)          # 三关全过 ⇒ 会选中
        # ⚠️ `MIN_POOL=3`：pool_n<3 时 picks 强制为空 ⇒ 正常分支至少要 3 只带棒的票
        codes = ["A00001", "B00002", "C00003"]
        prev_close = {c: 10.0 for c in codes}
        ma20 = {c: 12.0 for c in codes}
        bars = {c: ok_bar for c in codes} if with_bars else {}
        with tempfile.TemporaryDirectory() as td:
            ex.run_buy_exec(gw, "2026-10-17", prev_close, ma20, bars,
                            board={c: "医药" for c in codes}, book_dir=td,
                            dry_run=True, sleep_fn=lambda *_: None,
                            log=lambda *_: None,
                            names={c: "测试股" for c in codes},
                            prescreen_meta={"universe": "full_market", "board_top_n": 5})
            fp = Path(td) / "decision_2026-10-17.json"
            self.assertTrue(fp.exists(), "decision 文件没落盘")
            return json.loads(fp.read_text(encoding="utf-8"))

    def test_no_picks_branch(self):
        d = self._run(with_bars=False)               # 棒缺失 ⇒ 无候选
        self.assertEqual(d["note"], "no_picks")
        self.assertEqual(d["picks"], [])
        # 棒缺失的候选仍要出现在逐只名单里
        self.assertEqual(sorted(s["code"] for s in d["stocks"]),
                         ["A00001", "B00002", "C00003"])
        self.assertTrue(all(s["reason"] == "bar_missing" for s in d["stocks"]))

    def test_normal_branch(self):
        d = self._run(with_bars=True)
        self.assertEqual(sorted(p["code"] for p in d["picks"]), ["A00001", "B00002", "C00003"])
        self.assertEqual(d["funnel"]["pool_n"], 3)
        self.assertEqual(d["funnel"]["all"], 3)
        self.assertTrue(all(s["status"] == "picked" for s in d["stocks"]))
        self.assertEqual(d["premarket"]["universe"], "full_market")


class TestZeroCandidateBuyRecord(unittest.TestCase):
    def test_no_candidates_writes_fills_and_report_without_market_or_orders(self):
        from datetime import datetime
        from oversold_gap_confirm import hooks

        day = "2026-10-26"
        hooks.reset_day(day)
        self.addCleanup(hooks.reset_day, day)

        class _Gateway:
            def __init__(self):
                self.orders = []

            def get_cash(self):
                raise RuntimeError("现金接口不可用（测试注入）")

            def place_order(self, *args):
                self.orders.append(args)
                raise AssertionError("零候选不得下单")

        class _RT:
            def __init__(self, book_dir, gateway):
                self.book_dir = book_dir
                self._gateway = gateway
                self.sdk_calls = []
                self.audits = []

            def kill_switch(self):
                return False

            def gateway(self, context):
                return self._gateway

            def sdk_call(self, *args):
                self.sdk_calls.append(args)
                raise AssertionError("零候选不得请求个股行情")

            def audit(self, rec):
                self.audits.append(rec)

            def log(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as td:
            gw = _Gateway()
            rt = _RT(td, gw)
            now = datetime(2026, 10, 26, 10, 0)
            self.assertEqual(hooks.try_buy(rt, None, now), 0)

            fills = json.loads(Path(td, "fills_%s.json" % day).read_text(encoding="utf-8"))
            report = json.loads(Path(td, "exec_report_%s.json" % day)
                                .read_text(encoding="utf-8"))
            expected_summary = {
                "buys_planned": 0, "buys_placed": 0, "rejected_count": 0,
                "deferred_count": 0, "notional_placed": 0.0,
                "candidates": 0, "cash_before": 0.0,
            }
            self.assertEqual(len(expected_summary), 7, "正常摘要七字段仍须完整")
            self.assertEqual(set(fills["summary"]), set(expected_summary) | {"skip_reason"})
            for key, value in expected_summary.items():
                self.assertEqual(fills["summary"][key], value)
            self.assertEqual(fills["summary"]["skip_reason"], "no_candidates")
            self.assertEqual(report["summary"], fills["summary"])
            self.assertEqual(fills["note"], "no_picks")
            self.assertEqual(fills["picks"], [])
            self.assertEqual(fills["spares"], [])
            self.assertEqual(fills["buys"], [])
            self.assertTrue(hooks._STATE["buy_done"])
            day_state = json.loads(Path(td, "day_state_%s.json" % day)
                                   .read_text(encoding="utf-8"))
            self.assertTrue(day_state["buy_done"])
            self.assertEqual(gw.orders, [])
            self.assertEqual(rt.sdk_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
