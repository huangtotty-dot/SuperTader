# -*- coding: utf-8 -*-
"""市场风向标排名 15 分钟重跑（2026-11-14，owner：「风向标盘内排名至少15分钟级刷新」）。

真源 = `stock_hunter/history/daily_summary.json`，只有选股猎手跑完才重写；30 分钟轮询
（`poll_once`）只做异常校验、不写回。本模块新增：
  · `regenerate_summary` —— 全池重跑猎手打分 + `compute_heat_scores` 落盘（**不抛**）；
  · `run_summary_regen`  —— 独立 daemon 线程，交易时段每 15 分钟一轮（测试缝 sleep_fn/now_fn）。

口径（钉住）：
  · `SUMMARY_REGEN_INTERVAL_SEC == 900`（15 分钟）
  · 交易时段才调 `regenerate_summary`；午休/收盘只 sleep 不跑
  · `regenerate_summary` 失败/异常都收进返回 dict，不向上抛
"""
import threading
import unittest
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _hp():
    import core.hunter_poll as hp
    return hp


class TestSummaryRegenContract(unittest.TestCase):
    def test_interval_15min(self):
        self.assertEqual(_hp().SUMMARY_REGEN_INTERVAL_SEC, 900)

    def test_函数存在(self):
        hp = _hp()
        self.assertTrue(callable(hp.regenerate_summary))
        self.assertTrue(callable(hp.run_summary_regen))

    def test_regenerate_summary_不抛(self):
        # 打爆 prepare_data ⇒ 异常收进返回 dict 的 error，不向上抛（也不落盘 daily_summary.json）
        from unittest import mock
        import stock_hunter.main as H
        hp = _hp()
        with mock.patch.object(H, "prepare_data", side_effect=RuntimeError("boom")):
            out = hp.regenerate_summary("2026-11-14")
        self.assertIsInstance(out, dict)
        for k in ("ok", "elapsed", "n_sectors", "error"):
            self.assertIn(k, out)
        self.assertIs(out["ok"], False)
        self.assertIn("RuntimeError", out["error"])

    def test_poll_once与regen共锁(self):
        hp = _hp()
        self.assertIsInstance(hp._PIPELINE_LOCK, type(threading.Lock()))


class TestRunSummaryRegenGate(unittest.TestCase):
    def _run(self, when, sleep_fn):
        import core.hunter_poll as hp
        calls = []
        stop = threading.Event()
        orig = hp.regenerate_summary

        def _fake(d):
            calls.append(d)
            stop.set()                       # 跑完一轮即停
            return {"ok": True, "n_sectors": 1, "elapsed": 0.0}

        hp.regenerate_summary = _fake
        try:
            hp.run_summary_regen(poll_sec=900, now_fn=lambda: when,
                                 sleep_fn=sleep_fn, stop_event=stop)
        finally:
            hp.regenerate_summary = orig
        return calls

    def test_交易时段跑一轮(self):
        # 周一 10:00（交易时段）：跑完一轮后 stop 已置位 → 恰好 1 次
        calls = self._run(datetime(2026, 10, 12, 10, 0), lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_午休不跑(self):
        # 周一 12:00（午休）：第一次 _sleep(30) 就置 stop → regenerate_summary 一次都没调
        stop = threading.Event()

        def _sleep(s):
            stop.set()

        import core.hunter_poll as hp
        calls = []
        orig = hp.regenerate_summary

        def _fake(d):
            calls.append(d)
            return {"ok": True, "n_sectors": 1, "elapsed": 0.0}

        hp.regenerate_summary = _fake
        try:
            hp.run_summary_regen(poll_sec=900, now_fn=lambda: datetime(2026, 10, 12, 12, 0),
                                 sleep_fn=_sleep, stop_event=stop)
        finally:
            hp.regenerate_summary = orig
        self.assertEqual(calls, [])


class TestSummaryRegenWiring(unittest.TestCase):
    def test_run_watch_启动重跑(self):
        src = (_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("_start_summary_regen()", src)

    def test_main_import带上启动函数(self):
        src = (_ROOT / "main.py").read_text(encoding="utf-8")
        # import 行里与 _start_ma60_pullback_watch 同簇
        self.assertIn("_start_summary_regen", src)
        self.assertIn("_start_ma60_pullback_watch", src)

    def test_hunter_watch_有启动函数(self):
        src = (_ROOT / "core" / "hunter_watch.py").read_text(encoding="utf-8")
        self.assertIn("def _start_summary_regen", src)
        self.assertIn("run_summary_regen", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
