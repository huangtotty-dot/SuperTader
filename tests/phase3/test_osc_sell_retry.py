# -*- coding: utf-8 -*-
"""P1-7（2026-10-26）：卖侧钩子「成功才置位 / 失败下一根 bar 重试 / 上限·时限后放弃」。

缺陷原型（10-26 性能审计 H7）：每日一次标记原先在钩子执行**前置位**
（`_STATE["sell"] = True` 先于 try_sell）——钩子一抛异常，卖侧**当日永久缺席**
（与历史「14:55 尾盘归位未执行」事故同型；卖侧是风控腿）。

口径（断言；try_sell/try_morning_sell 换成记录器，只验 on_bar 的闸行为）：
  T1 成功才置位：钩子正常返回 ⇒ 置位，同一时点第二根 bar 不再放行
  T2 失败回滚 + 下一根 bar 重试：钩子抛异常 ⇒ 不置位，14:51 再试
  T3 重试上限：连抛 ⇒ 首试 + SELL_RETRY_MAX 次后放弃（置位 + risk:osc_sell_giveup），
     之后的 bar 不再调用钩子
  T4 最晚时限只闸重试：首试永远允许（14:57 迟到启动也卖一次）；
     失败后下一根 bar 已过 14:58 ⇒ 不再重试、置位放弃
  T5 早盘冲高卖同型：10:00 抛异常 ⇒ 10:01 重试
  T6 跨日重置：reset_day 清重试计数，次日重新放行

运行：python -m unittest tests.phase3.test_osc_sell_retry -v
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

from oversold_gap_confirm import hooks as osc_hooks                       # noqa: E402

_TODAY = "2026-10-26"


class FakeRT(object):
    """够 on_bar 卖侧两格用的最小 rt（同 test_osc_backfill_shift.FakeRT 风格）。"""
    def __init__(self, params=None):
        self.p = dict(params or {})
        self.book_dir = tempfile.mkdtemp()
        self.logs = []
        self.audits = []
        self.risks = []

    def param(self, k, d=None):
        return self.p.get(k, d)

    def log(self, *a, **k):
        self.logs.append(" ".join(str(x) for x in a))

    def audit(self, rec):
        self.audits.append(rec)

    def risk(self, *a, **k):
        self.risks.append(a)

    def kill_switch(self):
        return False

    def gateway(self, ctx):
        return None

    def is_live(self, ctx):
        return True


def _rt():
    return FakeRT({"osc_enabled": True, "osc_sell_enabled": True})


def _bar(rt, hh, mm, today=_TODAY):
    now = _dt.datetime.strptime("%s %02d:%02d" % (today, hh, mm), "%Y-%m-%d %H:%M")
    osc_hooks.on_bar(rt, None, now, today, now.time())


class TestSellRetry(unittest.TestCase):
    def setUp(self):
        # 钩子换成记录器（只验 on_bar 闸，不验钩子内部）；
        # 其余各格全部预置位/关门，隔离卖侧：prepare/buy 已完成、补单/探测开关关闭。
        self._orig_sell = osc_hooks.try_sell
        self._orig_morning = osc_hooks.try_morning_sell
        osc_hooks.reset_day(_TODAY)
        osc_hooks._STATE.update({"prepare": True, "buy": True, "buy_done": True})
        self.calls = []

    def tearDown(self):
        osc_hooks.try_sell = self._orig_sell
        osc_hooks.try_morning_sell = self._orig_morning
        osc_hooks.reset_day(None)

    def _stub_sell(self, fail=False):
        def _rec(rt, context, now, today):
            self.calls.append(("sell", now.strftime("%H:%M")))
            if fail:
                raise RuntimeError("SDK 抖动（模拟）")
            return 0
        osc_hooks.try_sell = _rec

    def _stub_morning(self, fail=False):
        def _rec(rt, context, now, today):
            self.calls.append(("morning", now.strftime("%H:%M")))
            if fail:
                raise RuntimeError("SDK 抖动（模拟）")
            return 0
        osc_hooks.try_morning_sell = _rec

    # T1：成功才置位（成功后第二根 bar 不再放行——每日一次语义不变）
    def test_t1_success_marks_done(self):
        self._stub_sell(fail=False)
        rt = _rt()
        _bar(rt, 14, 50)
        _bar(rt, 14, 51)
        self.assertEqual(self.calls, [("sell", "14:50")])
        self.assertTrue(osc_hooks._STATE["sell"])
        self.assertEqual(int(osc_hooks._STATE.get("sell_retries") or 0), 0)

    # T2：失败 ⇒ 标记不置位，下一根 bar 重试
    def test_t2_failure_rolls_back_and_retries_next_bar(self):
        self._stub_sell(fail=True)
        rt = _rt()
        _bar(rt, 14, 50)
        self.assertFalse(osc_hooks._STATE["sell"], "抛异常 ⇒ 每日标记不得置位")
        self.assertEqual(int(osc_hooks._STATE.get("sell_retries") or 0), 1)
        _bar(rt, 14, 51)
        self.assertEqual(self.calls, [("sell", "14:50"), ("sell", "14:51")],
                         "下一根 60s bar 必须重试")
        # 第三次成功 ⇒ 置位收官
        self._stub_sell(fail=False)
        _bar(rt, 14, 52)
        self.assertTrue(osc_hooks._STATE["sell"])
        self.assertEqual(self.calls[-1], ("sell", "14:52"))
        _bar(rt, 14, 53)
        self.assertEqual(len(self.calls), 3, "成功后不再放行")

    # T3：重试上限耗尽 ⇒ 置位放弃 + risk 告警，之后不再调用
    def test_t3_give_up_after_retry_cap(self):
        self._stub_sell(fail=True)
        rt = _rt()
        for mm in (50, 51, 52, 53):
            _bar(rt, 14, mm)
        # 14:50 首试失败→r=1；14:51 r=1<3 重试→r=2；14:52 r=2<3 重试→r=3；
        # 14:53 r=3 ≥ SELL_RETRY_MAX ⇒ 放弃（不再调用钩子）
        self.assertEqual(self.calls,
                         [("sell", "14:50"), ("sell", "14:51"), ("sell", "14:52")],
                         "首试 + SELL_RETRY_MAX 次后必须停手")
        self.assertTrue(osc_hooks._STATE["sell"], "放弃也要置位（当日不再进场）")
        self.assertTrue(any("osc_sell_giveup" in str(r) for r in rt.risks),
                        "放弃必须发 risk 告警（watcher 据此推飞书）")
        _bar(rt, 14, 54)
        self.assertEqual(len(self.calls), 3, "放弃后不再调用钩子")

    # T4：最晚时限只闸重试——首试永远允许；过 14:58 不再重试
    def test_t4_deadline_gates_retry_not_first_attempt(self):
        self._stub_sell(fail=True)
        rt = _rt()
        _bar(rt, 14, 57)          # 迟到启动：首试照样允许
        self.assertEqual(self.calls, [("sell", "14:57")])
        _bar(rt, 14, 59)          # 已过 14:58 ⇒ 不重试，置位放弃
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(osc_hooks._STATE["sell"])
        self.assertTrue(any("osc_sell_giveup" in str(r) for r in rt.risks))

    # T5：早盘冲高卖同型（10:00 失败 ⇒ 10:01 重试 ⇒ 成功置位）
    def test_t5_morning_sell_retries_same_shape(self):
        self._stub_morning(fail=True)
        rt = _rt()
        _bar(rt, 10, 0)
        self.assertFalse(osc_hooks._STATE["morning"])
        self._stub_morning(fail=False)
        _bar(rt, 10, 1)
        self.assertEqual(self.calls, [("morning", "10:00"), ("morning", "10:01")])
        self.assertTrue(osc_hooks._STATE["morning"])

    # T6：跨日重置清重试计数
    def test_t6_next_day_rearms(self):
        self._stub_sell(fail=True)
        rt = _rt()
        _bar(rt, 14, 50)
        self.assertEqual(int(osc_hooks._STATE.get("sell_retries") or 0), 1)
        osc_hooks.reset_day("2026-10-27")
        osc_hooks._STATE.update({"prepare": True, "buy": True, "buy_done": True})
        _bar(rt, 14, 50, today="2026-10-27")
        self.assertEqual(self.calls, [("sell", "14:50"), ("sell", "14:50")],
                         "次日必须重新放行（重试计数已清）")
        self.assertEqual(int(osc_hooks._STATE.get("sell_retries") or 0), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
