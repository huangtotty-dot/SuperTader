# -*- coding: utf-8 -*-
"""预挂目标限价 · S0 影子探测测试（2026-10-19）。

覆盖：
  A. `preplace_probe.build_plan` 的资格（D+1 / days_held==0 / 今日入场排除）与取整；
  B. `enrich` 的触及时间、10:00 参照价、保守成交口径（穿过 vs 仅触及）、cutoff；
  C. 原子落盘/回读；
  D. `hooks` 的两个时点钩子（09:31 计划 / 10:00 完成）每日一次，且**不下任何委托**。

运行：python -m unittest tests.phase3.test_osc_preplace_probe -v
"""
import datetime as _dt
import json
import sys
import tempfile
import unittest
from datetime import time as dtime
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
for _p in (str(_ROOT), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from oversold_gap_confirm import executor as wse                          # noqa: E402
from oversold_gap_confirm import hooks as osc_hooks                       # noqa: E402
from oversold_gap_confirm import preplace_probe as pp                    # noqa: E402


class TestBuildPlan(unittest.TestCase):
    def test_only_d1_positions_and_rounding(self):
        book = {
            "600001": {"entry_date": "2026-10-16", "entry_price": 6.53,
                       "qty": 1000, "days_held": 0},
            "600002": {"entry_date": "2026-10-19", "entry_price": 10.0,
                       "qty": 1000, "days_held": 0},          # 今日入场 ⇒ 不可卖
            "600003": {"entry_date": "2026-10-14", "entry_price": 10.0,
                       "qty": 1000, "days_held": 1},          # 非第一个可卖早盘
            "600004": {"entry_date": "2026-10-16", "entry_price": 0,
                       "qty": 1000, "days_held": 0},          # 无有效入场价
        }
        rows = pp.build_plan(book, "2026-10-19", target_pct=0.03)
        self.assertEqual([r["code"] for r in rows], ["600001"])
        self.assertAlmostEqual(rows[0]["limit_px"], 6.73)   # 6.53*1.03=6.7259 → 6.73
        self.assertEqual(rows[0]["qty"], 1000)

    def test_tick_rounding(self):
        rows = pp.build_plan({"600001": {"entry_date": "2026-10-16",
                                         "entry_price": 10.0, "qty": 100,
                                         "days_held": 0}}, "2026-10-19")
        self.assertEqual(rows[0]["limit_px"], 10.30)


def _bars():
    return pd.DataFrame([
        {"symbol": "SHSE.600001", "eob": "2026-10-19 09:59:00",
         "high": 6.72, "low": 6.60, "close": 6.65},
        {"symbol": "SHSE.600001", "eob": "2026-10-19 10:00:00",
         "high": 6.73, "low": 6.72, "close": 6.71},
        # 10:00 之后的 bar 必须被 cutoff 排除
        {"symbol": "SHSE.600001", "eob": "2026-10-19 10:01:00",
         "high": 6.99, "low": 6.90, "close": 6.98},
    ])


class TestEnrich(unittest.TestCase):
    ROW = {"code": "600001", "entry_date": "2026-10-16", "entry_price": 6.53,
           "qty": 1000, "limit_px": 6.73}

    def test_touch_ref_and_cutoff(self):
        out = pp.enrich([self.ROW], _bars(),
                        cutoff=_dt.datetime(2026, 10, 19, 10, 0))
        r = out[0]
        self.assertEqual(r["touch_time"], "2026-10-19 10:00:00")
        self.assertAlmostEqual(r["px_10am_ref"], 6.71)       # 最后一个 ≤10:00 的 close
        self.assertAlmostEqual(r["bar_high_to_10"], 6.73)
        self.assertTrue(r["would_trigger_control"])
        self.assertFalse(r["conservative_fill"],
                         "high=6.73 只是触及，没有高于目标的打印 ⇒ 保守口径不算成交")
        self.assertEqual(r["n_bars"], 2)

    def test_trade_through_counts(self):
        bars = _bars()
        bars.loc[1, "high"] = 6.74          # 有一笔高于目标价的打印
        r = pp.enrich([self.ROW], bars, cutoff=_dt.datetime(2026, 10, 19, 10, 0))[0]
        self.assertTrue(r["conservative_fill"])

    def test_missing_code_is_safe(self):
        r = pp.enrich([dict(self.ROW, code="000999")], _bars())[0]
        self.assertIsNone(r["touch_time"])
        self.assertIsNone(r["px_10am_ref"])
        self.assertFalse(r["would_trigger_control"])
        self.assertEqual(r["n_bars"], 0)


class TestPersistence(unittest.TestCase):
    def test_roundtrip_and_phase(self):
        d = tempfile.mkdtemp()
        fp = pp.write(d, "2026-10-19", "plan",
                      [TestEnrich.ROW], 0.03, extra={"x": 1})
        with open(fp, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["phase"], "plan")
        self.assertEqual(payload["x"], 1)
        self.assertEqual(pp.read(d, "2026-10-19")["rows"][0]["code"], "600001")
        self.assertIsNone(pp.read(d, "2026-10-18"))


class FakeGM(object):
    def __init__(self, df):
        self.df = df
        self.ADJUST_PREV = 1
        self.calls = []

    def history(self, **kw):
        self.calls.append(kw)
        return self.df


class FakeRT(object):
    def __init__(self, book_dir, df, params=None):
        self.book_dir = book_dir
        self.p = dict(params or {})
        self.gm = FakeGM(df)
        self.audits = []
        self.logs = []

    def param(self, k, d=None):
        return self.p.get(k, d)

    def log(self, *a, **k):
        self.logs.append(" ".join(str(x) for x in a))

    def audit(self, rec):
        self.audits.append(rec)

    def risk(self, *a, **k):
        pass

    def code_to_gm(self, code):
        c = str(code)
        return ("SHSE." if c[:1] in "56" else "SZSE.") + c

    def sdk_call(self, desc, fn, *a, **k):
        return fn(*a, **k)

    def is_live(self, ctx):
        return True

    def gateway(self, ctx):
        return None


class TestHooks(unittest.TestCase):
    def _rt(self, enabled=True):
        d = tempfile.mkdtemp()
        wse.save_book({"updated_at": "", "open": {
            "600001": {"entry_date": "2026-10-16", "entry_price": 6.53,
                       "qty": 1000, "days_held": 0},
            "600002": {"entry_date": "2026-10-19", "entry_price": 10.0,
                       "qty": 1000, "days_held": 0},
        }}, d)
        p = {"osc_preplace_probe_enabled": enabled, "osc_pop_target_pct": 0.03,
             "osc_enabled": True, "osc_morning_sell_time_hhmm": 1000}
        return FakeRT(d, _bars(), p)

    def test_plan_then_final(self):
        rt = self._rt()
        self.assertTrue(osc_hooks.preplace_probe_enabled(rt))
        self.assertEqual(osc_hooks.probe_preplace_plan(
            rt, _dt.datetime(2026, 10, 19, 9, 31), "2026-10-19"), 1)
        self.assertEqual(pp.read(rt.book_dir, "2026-10-19")["phase"], "plan")

        n = osc_hooks.probe_preplace_final(
            rt, None, _dt.datetime(2026, 10, 19, 10, 0), "2026-10-19")
        self.assertEqual(n, 1)
        final = pp.read(rt.book_dir, "2026-10-19")
        self.assertEqual(final["phase"], "final")
        self.assertEqual(final["rows"][0]["px_10am_ref"], 6.71)
        self.assertEqual(rt.gm.calls[-1]["frequency"], "60s")
        self.assertEqual(rt.gm.calls[-1]["fields"], "symbol,eob,high,low,close")

    def test_disabled_default_writes_nothing(self):
        rt = self._rt(enabled=False)
        self.assertFalse(osc_hooks.preplace_probe_enabled(rt))
        self.assertEqual(osc_hooks.probe_preplace_plan(
            rt, _dt.datetime(2026, 10, 19, 9, 31), "2026-10-19"), 0)
        self.assertIsNone(pp.read(rt.book_dir, "2026-10-19"))

    def test_on_bar_fires_probe_once_and_does_not_trade(self):
        rt = self._rt()
        osc_hooks.reset_day("2026-10-19")
        # 其余钩子先标记完成，避免单测里跑真预筛/买卖
        osc_hooks._STATE.update({"prepare": True, "morning": True, "buy": True,
                                 "sell": True, "backfill": {0: True, 1: True}})
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 19, 9, 31),
                         "2026-10-19", dtime(9, 31))
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 19, 9, 32),
                         "2026-10-19", dtime(9, 32))
        self.assertEqual(pp.read(rt.book_dir, "2026-10-19")["phase"], "plan")
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 19, 10, 0),
                         "2026-10-19", dtime(10, 0))
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 19, 10, 1),
                         "2026-10-19", dtime(10, 1))
        final = pp.read(rt.book_dir, "2026-10-19")
        self.assertEqual(final["phase"], "final")
        self.assertEqual(len(rt.gm.calls), 1, "取数只应发生一次")
        self.assertFalse(hasattr(rt, "gw") and getattr(rt, "gw", None))


class TestParamWiring(unittest.TestCase):
    def test_probe_switch_in_params_table(self):
        import ast
        fp = _AUTO / "_gm" / "config" / "params.py"
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        p = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
                p = ast.literal_eval(node.value)
        self.assertIn("osc_preplace_probe_enabled", p)
        self.assertTrue(p["osc_preplace_probe_enabled"],
                        "S0 影子探测（只记录不下单）已由 owner 2026-10-19 批准开始")


if __name__ == "__main__":
    unittest.main()
