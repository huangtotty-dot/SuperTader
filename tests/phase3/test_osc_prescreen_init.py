# -*- coding: utf-8 -*-
"""P0-3（2026-10-26）盘前预筛前移 init —— 行为级测试。

## 背景

原 09:31 钩子在 SDK 事件派发线程上串行跑全市场 ~5000 只 × 25 批日线
（实测 169s~472s，最差 1344.5s），阻塞期间 fill 回调排队、心跳停写。
P0-3 把主路径前移到 `prescreen_init`（init 阶段、事件循环开始前）；
09:31 钩子保留为兜底（init 未跑/失败 ⇒ 照旧执行；init 成功 ⇒ 跳过）。

## 口径（断言）

  T1 init 成功 ⇒ 09:31 钩子跳过（不再重复取数，候选保留，打一行跳过日志）
  T2 init 失败 ⇒ fail-open（不置位、不抛），09:31 钩子兜底重跑一次
  T3 盘中重启 ⇒ 当日新鲜缓存直接装载（**不重新取数**，prescreen_meta 一并恢复）
  T4 隔日缓存 / 失败标记(ok=False) 缓存 ⇒ 不算新鲜，init 现场重跑
  T5 总闸关闭（osc_enabled=False）⇒ prescreen_init 完全空转
  T6 reset_day 清掉 init 标记（跨日后 09:31 钩子恢复主责）
  T7 init 跑出 0 只候选（合法结果）⇒ 仍算成功，09:31 钩子跳过

运行：python -m unittest tests.phase3.test_osc_prescreen_init -v
"""
import datetime as _dt
import json
import os
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

from oversold_gap_confirm import hooks as osc_hooks                       # noqa: E402

_TODAY = "2026-10-26"
_CAND = {"600001": {"prev_close": 9.0, "ma20": 10.0, "board": "半导体", "name": "测试甲"}}


class FakeRT(object):
    """够 prescreen_init / prepare / on_bar(09:31 格) 用的最小 rt。"""

    def __init__(self, params=None, book_dir=None):
        self.p = dict(params or {})
        self.book_dir = book_dir or tempfile.mkdtemp()
        self.logs = []
        self.audits = []

    def param(self, k, d=None):
        return self.p.get(k, d)

    def log(self, *a, **k):
        self.logs.append(" ".join(str(x) for x in a))

    def audit(self, rec):
        self.audits.append(rec)

    def risk(self, *a, **k):
        pass

    def kill_switch(self):
        return False

    def is_live(self, ctx):
        return True


def _write_cache(book_dir, date, cand, ok=True):
    """手写一份 prescreen_{date}.json（与 hooks._write_prescreen 同 schema）。"""
    payload = {"date": str(date), "generated_at": "%s 08:55:00" % date, "ok": bool(ok),
               "universe": "full_market", "board_top_n": 5,
               "candidates_n": len(cand), "candidates": cand}
    with open(os.path.join(book_dir, "prescreen_%s.json" % date), "w",
              encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


class _ProviderSwitch(object):
    """可控的 premarket_candidates 替身：记调用次数，可切 成功/空结果/抛错。"""

    def __init__(self):
        self.calls = 0
        self.mode = "ok"        # ok / empty / boom

    def __call__(self, top_n, date=None, provider=None, full_market=True):
        self.calls += 1
        if self.mode == "boom":
            raise RuntimeError("数据源炸了（测试注入）")
        if self.mode == "empty":
            return {}
        return dict(_CAND)


class TestPrescreenInit(unittest.TestCase):
    def setUp(self):
        osc_hooks.reset_day(_TODAY)
        osc_hooks._STATE["date"] = _TODAY
        self.ps = _ProviderSwitch()
        self._orig = osc_hooks.prescreen.premarket_candidates
        osc_hooks.prescreen.premarket_candidates = self.ps
        self.addCleanup(self._restore)

    def _restore(self):
        osc_hooks.prescreen.premarket_candidates = self._orig
        osc_hooks.reset_day(_TODAY)

    def _rt(self, **kw):
        kw.setdefault("params", {"osc_enabled": True})
        return FakeRT(**kw)

    def _bar_0931(self, rt):
        osc_hooks.on_bar(rt, None, _dt.datetime(2026, 10, 26, 9, 31), _TODAY,
                         dtime(9, 31))

    # T1 ─────────────────────────────────────────────────────────────
    def test_t1_init_success_then_hook_skips(self):
        rt = self._rt()
        self.assertTrue(osc_hooks.prescreen_init(rt, _TODAY))
        self.assertEqual(self.ps.calls, 1, "init 应现场跑一遍预筛")
        self.assertTrue(osc_hooks._STATE["prepare"])
        self.assertEqual(osc_hooks._STATE["prepare_via"], "init")
        self.assertIn("600001", osc_hooks._STATE["candidates"])
        self._bar_0931(rt)
        self.assertEqual(self.ps.calls, 1, "init 成功后 09:31 钩子不得再跑全市场取数")
        self.assertIn("600001", osc_hooks._STATE["candidates"], "候选必须保留给 10:00 买侧")
        self.assertTrue(any("跳过" in ln for ln in rt.logs), "应留一行跳过日志")

    # T2 ─────────────────────────────────────────────────────────────
    def test_t2_init_failure_hook_retries_once(self):
        self.ps.mode = "boom"
        rt = self._rt()
        self.assertFalse(osc_hooks.prescreen_init(rt, _TODAY),
                         "init 失败应返回 False 而不是抛（fail-open）")
        self.assertFalse(osc_hooks._STATE["prepare"],
                         "init 失败不得置位 prepare——否则 09:31 兜底不会触发")
        self.assertIsNone(osc_hooks._STATE["prepare_via"])
        self._bar_0931(rt)
        self.assertEqual(self.ps.calls, 2, "init 失败后 09:31 钩子应兜底重跑一次")
        self.assertTrue(osc_hooks._STATE["prepare"])
        self.assertNotEqual(osc_hooks._STATE["prepare_via"], "init")
        self.assertTrue(any(a.get("event") == "osc_prescreen_init_fail"
                            for a in rt.audits), "init 失败要留审计")

    # T3 ─────────────────────────────────────────────────────────────
    def test_t3_restart_loads_fresh_cache_without_refetch(self):
        rt = self._rt()
        _write_cache(rt.book_dir, _TODAY,
                     {"600002": {"prev_close": 8.0, "ma20": 9.0,
                                 "board": "电池", "name": "测试乙"}})
        self.assertTrue(osc_hooks.prescreen_init(rt, _TODAY))
        self.assertEqual(self.ps.calls, 0, "有当日新鲜缓存 ⇒ 只装载，不重新取数")
        self.assertEqual(osc_hooks._STATE["prepare_via"], "init")
        self.assertIn("600002", osc_hooks._STATE["candidates"])
        self.assertEqual(osc_hooks._STATE["prescreen_meta"].get("universe"),
                         "full_market", "prescreen_meta 要随缓存恢复（10:00 留痕不断档）")
        self._bar_0931(rt)
        self.assertEqual(self.ps.calls, 0, "缓存装载后 09:31 钩子同样跳过")

    # T4 ─────────────────────────────────────────────────────────────
    def test_t4_stale_or_failed_cache_is_not_fresh(self):
        rt = self._rt()
        _write_cache(rt.book_dir, "2026-10-25", dict(_CAND))     # 隔日
        self.assertTrue(osc_hooks.prescreen_init(rt, _TODAY))
        self.assertEqual(self.ps.calls, 1, "隔日缓存不算新鲜 ⇒ 现场重跑")

        osc_hooks.reset_day(_TODAY)
        osc_hooks._STATE["date"] = _TODAY
        self.ps.calls = 0
        rt2 = self._rt()
        _write_cache(rt2.book_dir, _TODAY, {}, ok=False)          # 失败标记的空名单
        self.assertTrue(osc_hooks.prescreen_init(rt2, _TODAY))
        self.assertEqual(self.ps.calls, 1, "ok=False 缓存不算新鲜 ⇒ 现场重跑")

    # T5 ─────────────────────────────────────────────────────────────
    def test_t5_disabled_is_noop(self):
        rt = self._rt(params={"osc_enabled": False})
        self.assertFalse(osc_hooks.prescreen_init(rt, _TODAY))
        self.assertEqual(self.ps.calls, 0, "总闸关闭 ⇒ 完全空转")
        self.assertFalse(osc_hooks._STATE["prepare"])

    # T6 ─────────────────────────────────────────────────────────────
    def test_t6_reset_day_clears_init_marks(self):
        rt = self._rt()
        self.assertTrue(osc_hooks.prescreen_init(rt, _TODAY))
        self._bar_0931(rt)                       # 打出跳过日志、置 prepare_skip_logged
        osc_hooks.reset_day("2026-10-27")
        self.assertIsNone(osc_hooks._STATE["prepare_via"])
        self.assertFalse(osc_hooks._STATE["prepare_skip_logged"])
        self.assertFalse(osc_hooks._STATE["prepare"], "跨日后 09:31 钩子恢复主责")

    # T7 ─────────────────────────────────────────────────────────────
    def test_t7_zero_candidates_is_still_success(self):
        self.ps.mode = "empty"
        rt = self._rt()
        self.assertTrue(osc_hooks.prescreen_init(rt, _TODAY),
                        "0 只候选是合法结果（跑完即成功），不能触发 09:31 重跑")
        self.assertEqual(osc_hooks._STATE["prepare_via"], "init")
        self._bar_0931(rt)
        self.assertEqual(self.ps.calls, 1, "合法的 0 候选日不得再付一次全市场取数成本")


if __name__ == "__main__":
    unittest.main(verbosity=2)
