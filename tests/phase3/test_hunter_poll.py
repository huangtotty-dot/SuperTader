# -*- coding: utf-8 -*-
"""选股猎手 30 分钟轮询 + 异常飞书告警 单测（2026-10-11）。**全离线，绝不真发飞书**。

背景：owner「实盘过程中选股猎手每 30 分钟轮询；**重点中的重点：异常信息一定要通过飞书发出来**」。
本仓在这条路上有过两次血案（告警"上线起从未推送成功过一次"，因为少传必传参数被裸 except 吞掉），
故这里把三件事钉死：
  ① 推送**调用契约**：必须传满 (payload, success_log, error_prefix)；
  ② **去重**：同类异常一天只推一次（30 分钟一轮不刷屏）；
  ③ **失败不写去重** ⇒ 下轮重试（不能把"发失败"记成"发过了"）。

运行：python -m unittest tests.phase3.test_hunter_poll
"""
import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402
import core.hunter_poll as HP  # noqa: E402


class _SendSpy:
    """替换 config.send_feishu_payload：记录调用、可控返回值。**不发任何网络请求**。"""

    def __init__(self, ret=True):
        self.calls = []
        self.ret = ret

    def __call__(self, payload, success_log, error_prefix, *a, **kw):
        self.calls.append({"payload": payload, "success_log": success_log,
                           "error_prefix": error_prefix, "args": a, "kwargs": kw})
        return self.ret


class TestHunterPollAlert(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_fp = HP._DEDUP_FP
        HP._DEDUP_FP = os.path.join(self._tmp.name, "alerts.json")
        self._orig_send = config.send_feishu_payload

    def tearDown(self):
        HP._DEDUP_FP = self._orig_fp
        config.send_feishu_payload = self._orig_send
        self._tmp.cleanup()

    @staticmethod
    def _bad(*kinds):
        return {"ok": False, "stats": {"date": "2026-10-11", "pool_rows": 100, "scored": 0,
                                       "coverage": 0.0},
                "anomalies": [{"kind": k, "msg": f"{k} 说明"} for k in kinds]}

    def test_01_no_anomaly_no_push(self):
        spy = _SendSpy()
        config.send_feishu_payload = spy
        self.assertFalse(HP.push_anomalies({"ok": True, "anomalies": []}, "2026-10-11"))
        self.assertEqual(spy.calls, [], "正常轮询不得推送")

    def test_02_push_contract_and_dedup(self):
        """① 契约（三参必传）② 同类一天只推一次 ③ 新类型再推一次。"""
        spy = _SendSpy()
        config.send_feishu_payload = spy
        self.assertTrue(HP.push_anomalies(self._bad("low_coverage"), "2026-10-11"))
        self.assertEqual(len(spy.calls), 1)
        c = spy.calls[0]
        self.assertTrue(c["success_log"], "必须传 success_log（本仓曾因漏传导致告警静默失效）")
        self.assertTrue(c["error_prefix"], "必须传 error_prefix")
        # 通过真实签名绑一次，钉死契约（少传/名字写错会在这里炸）
        inspect.signature(config._send_feishu_payload).bind(
            c["payload"], c["success_log"], c["error_prefix"])
        # 同类再来 → 去重
        self.assertFalse(HP.push_anomalies(self._bad("low_coverage"), "2026-10-11"))
        self.assertEqual(len(spy.calls), 1, "同类异常当天不得重复推")
        # 新类型 → 再推
        self.assertTrue(HP.push_anomalies(self._bad("low_coverage", "empty_summary"), "2026-10-11"))
        self.assertEqual(len(spy.calls), 2)
        # 换一天 → 重新推
        self.assertTrue(HP.push_anomalies(self._bad("low_coverage"), "2026-10-12"))
        self.assertEqual(len(spy.calls), 3)

    def test_03_failed_push_is_retried(self):
        """推送失败**不得**写去重 —— 否则一次网络抖动就永久吞掉这类告警。"""
        spy = _SendSpy(ret=False)
        config.send_feishu_payload = spy
        self.assertFalse(HP.push_anomalies(self._bad("run_exception"), "2026-10-11"))
        spy.ret = True
        self.assertTrue(HP.push_anomalies(self._bad("run_exception"), "2026-10-11"),
                        "上一轮发失败，本轮必须重试")
        self.assertEqual(len(spy.calls), 2)

    def test_04_push_exception_does_not_write_dedup(self):
        def _boom(*a, **kw):
            raise RuntimeError("webhook 炸了")
        config.send_feishu_payload = _boom
        self.assertFalse(HP.push_anomalies(self._bad("empty_pool"), "2026-10-11"))
        spy = _SendSpy()
        config.send_feishu_payload = spy
        self.assertTrue(HP.push_anomalies(self._bad("empty_pool"), "2026-10-11"))

    def test_05_card_is_concise(self):
        """owner：实时性不能动，但**消息要短**（手机上别是一堵墙）。"""
        res = self._bad("low_coverage", "empty_summary")
        res["anomalies"][0]["msg"] = "行情覆盖率仅 43%（阈值 50%）⇒ 行情源可能不可用"
        res["anomalies"][1]["msg"] = "Traceback (most recent call last):\n  File x\n" * 3
        text = HP.format_anomaly_card(res, "2026-10-11",
                                      now=datetime(2026, 10, 11, 10, 30))["content"]["text"]
        self.assertIn("行情覆盖低", text)
        self.assertIn("板块排名空", text)
        self.assertIn("10-11 10:30", text, "带时间（实时性）")
        self.assertNotIn("Traceback", text, "traceback 不得进卡片")
        self.assertLessEqual(len(text.splitlines()), 5, f"卡片过长:\n{text}")
        self.assertLessEqual(len(text), 260, f"卡片过长: {len(text)} 字")

    def test_06_realtime_semantics(self):
        """持续存在不重复报 / 恢复后再现要再报 / 同类有当日上限。"""
        spy = _SendSpy()
        config.send_feishu_payload = spy
        bad = self._bad("low_coverage")
        # 出现 → 报
        self.assertTrue(HP.push_anomalies(bad, "2026-10-11"))
        # 持续（下轮还是同样异常）→ 不重复报
        self.assertFalse(HP.push_anomalies(bad, "2026-10-11"))
        self.assertEqual(len(spy.calls), 1)
        # 恢复（健康轮询）→ 不报，但清掉"上轮出现过"
        self.assertFalse(HP.push_anomalies({"ok": True, "anomalies": []}, "2026-10-11"))
        # 再次坏 → **要再报**（这才叫实时）
        self.assertTrue(HP.push_anomalies(bad, "2026-10-11"))
        self.assertEqual(len(spy.calls), 2)
        # 当日上限：继续"恢复→再现"若干次后不再报
        for _ in range(HP._MAX_PER_KIND_PER_DAY + 2):
            HP.push_anomalies({"ok": True, "anomalies": []}, "2026-10-11")
            HP.push_anomalies(bad, "2026-10-11")
        self.assertLessEqual(len(spy.calls), HP._MAX_PER_KIND_PER_DAY,
                             f"同类当日应封顶，实际 {len(spy.calls)} 次")


class TestHunterPollChecks(unittest.TestCase):
    """数据校验：真实跑猎手太重 ⇒ 用桩数据验判据（不看网络）。"""

    def _poll_with(self, data, scored):
        import core.hunter_poll as hp
        orig_ensure = hp._ensure_path
        hp._ensure_path = lambda: None
        try:
            import stock_hunter.main as H
            orig_pd, orig_ss = H.prepare_data, H.score_stocks
            H.prepare_data = lambda cfg, d: data
            H.score_stocks = lambda d, cfg: scored
            try:
                return hp.poll_once("2026-10-11")
            finally:
                H.prepare_data, H.score_stocks = orig_pd, orig_ss
        finally:
            hp._ensure_path = orig_ensure

    @staticmethod
    def _wl(cov_rows=100, total=100):
        import pandas as pd
        rows = []
        for i in range(total):
            ok = i < cov_rows
            rows.append({"代码": f"{i:06d}", "名称": f"n{i}", "韭研概念": "半导体",
                         "成交额": (1e8 if ok else None), "涨跌幅": (1.0 if ok else None)})
        return pd.DataFrame(rows)

    def test_01_healthy_no_anomaly(self):
        import pandas as pd
        res = self._poll_with({"watchlist": self._wl(), "summary": pd.DataFrame([{"板块": "半导体"}]),
                               "actual_data_dates": {"2026-10-11"}}, [{"x": 1}])
        self.assertTrue(res["ok"], res["anomalies"])
        self.assertAlmostEqual(res["stats"]["coverage"], 1.0, places=2)

    def test_02_low_coverage_flagged(self):
        import pandas as pd
        res = self._poll_with({"watchlist": self._wl(cov_rows=10), "summary": pd.DataFrame([{"板块": "x"}]),
                               "actual_data_dates": set()}, [{"x": 1}])
        self.assertFalse(res["ok"])
        self.assertIn("low_coverage", [a["kind"] for a in res["anomalies"]])

    def test_03_exception_captured_not_raised(self):
        import core.hunter_poll as hp
        hp._ensure_path = lambda: None
        import stock_hunter.main as H
        orig = H.prepare_data
        H.prepare_data = lambda cfg, d: (_ for _ in ()).throw(RuntimeError("行情源挂了"))
        try:
            res = hp.poll_once("2026-10-11")
        finally:
            H.prepare_data = orig
        self.assertFalse(res["ok"])
        self.assertEqual(res["anomalies"][0]["kind"], "run_exception")
        self.assertIn("行情源挂了", res["anomalies"][0]["msg"])


class TestSchedulerGates(unittest.TestCase):
    """时段闸 + 30 分钟节流（判据在 `core.hunter_poll`，故这里**不导入根 main.py**：
    它导入很重，且与 `stock_hunter/main.py` 同名——`sys.path` 顺序一变 `import main` 就解析错）。"""

    def test_01_trading_hours(self):
        self.assertFalse(HP.in_trading_hours(datetime(2026, 10, 11, 10, 0)), "周六不算交易时段")
        self.assertFalse(HP.in_trading_hours(datetime(2026, 10, 12, 9, 0)), "开盘前")
        self.assertTrue(HP.in_trading_hours(datetime(2026, 10, 12, 9, 30)), "09:30 起")
        self.assertTrue(HP.in_trading_hours(datetime(2026, 10, 12, 11, 30)), "11:30 止")
        self.assertFalse(HP.in_trading_hours(datetime(2026, 10, 12, 12, 0)), "午休不跑")
        self.assertTrue(HP.in_trading_hours(datetime(2026, 10, 12, 13, 0)), "13:00 起")
        self.assertTrue(HP.in_trading_hours(datetime(2026, 10, 12, 15, 0)), "15:00 止")
        self.assertFalse(HP.in_trading_hours(datetime(2026, 10, 12, 15, 30)), "收盘后")

    def test_02_throttle_30min(self):
        last = datetime(2026, 10, 12, 10, 0)
        self.assertFalse(HP.should_poll(datetime(2026, 10, 12, 10, 10), last), "10 分钟内不重复")
        self.assertFalse(HP.should_poll(datetime(2026, 10, 12, 10, 29), last))
        self.assertTrue(HP.should_poll(datetime(2026, 10, 12, 10, 30), last), "满 30 分钟再跑")
        self.assertTrue(HP.should_poll(datetime(2026, 10, 12, 14, 59), last))
        self.assertEqual(HP.POLL_INTERVAL_SEC, 1800, "owner 指定 30 分钟")

    def test_03_no_last_runs_immediately_in_hours(self):
        self.assertTrue(HP.should_poll(datetime(2026, 10, 12, 10, 0), None))
        self.assertFalse(HP.should_poll(datetime(2026, 10, 12, 12, 0), None), "非交易时段即使没跑过也不跑")


if __name__ == "__main__":
    unittest.main(verbosity=2)
