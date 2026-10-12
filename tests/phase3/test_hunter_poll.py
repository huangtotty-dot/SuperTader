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


class TestRoundHeartbeat(unittest.TestCase):
    """每轮**恰好一条**消息（owner 2026-10-12：异常 + 每轮精简摘要，都发「做T猎手预警」群）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_fp = HP._DEDUP_FP
        HP._DEDUP_FP = os.path.join(self._tmp.name, "alerts.json")
        self._orig_send = config.send_feishu_payload
        self._orig_poll = HP.poll_once
        self.calls = []

        def _spy(payload, success_log="", error_prefix="", *a, **k):
            # ⚠️ 生产代码用**关键字**传 success_log/error_prefix（契约三参必传）
            self.calls.append({"text": payload["content"]["text"],
                               "sl": success_log, "ep": error_prefix})
            return True

        config.send_feishu_payload = _spy

    def tearDown(self):
        HP._DEDUP_FP = self._orig_fp
        config.send_feishu_payload = self._orig_send
        HP.poll_once = self._orig_poll
        self._tmp.cleanup()

    @staticmethod
    def _ok_stats():
        return {"date": "2026-10-12", "sectors": 23, "limit_up": 13, "scored": 1753,
                "coverage": 1.0, "top3": [("大农业", 5, 1.5), ("有机硅", 3, 2.8)],
                "top1_sector": "大农业", "top1_names": ["万向德农", "金健米业"]}

    def _run_round(self, res):
        HP.poll_once = lambda d=None: res
        return HP.run_and_alert("2026-10-12")

    def test_01_healthy_round_sends_exactly_one_summary(self):
        r = self._run_round({"ok": True, "anomalies": [], "stats": self._ok_stats()})
        self.assertEqual(len(self.calls), 1, "健康轮应恰好一条")
        t = self.calls[0]["text"]
        self.assertIn("猎手轮询", t)
        self.assertIn("涨停前三", t)
        self.assertIn("榜首三强", t)
        self.assertIn("覆盖100%", t)
        self.assertTrue(self.calls[0]["sl"] and self.calls[0]["ep"], "推送三参必传")
        self.assertLessEqual(len(t.splitlines()), 5, f"心跳过长:\n{t}")

    def test_02_new_anomaly_sends_one_anomaly_card(self):
        res = {"ok": False, "stats": self._ok_stats(),
               "anomalies": [{"kind": "low_coverage", "msg": "行情覆盖率仅 43%（阈值 50%）"}]}
        self._run_round(res)
        self.assertEqual(len(self.calls), 1, "新异常轮也只发一条（异常卡）")
        self.assertIn("猎手轮询异常", self.calls[0]["text"])
        self.assertIn("行情覆盖低", self.calls[0]["text"])

    def test_03_persisting_anomaly_rides_the_heartbeat(self):
        """异常持续（去重不再单发）⇒ 心跳卡必须**带 ⚠️ 置顶**，不能看起来一切正常。"""
        res = {"ok": False, "stats": self._ok_stats(),
               "anomalies": [{"kind": "low_coverage", "msg": "行情覆盖率仅 43%"}]}
        self._run_round(res)                      # 第 1 轮：异常卡
        self._run_round(res)                      # 第 2 轮：去重 ⇒ 走心跳
        self.assertEqual(len(self.calls), 2, "每轮仍恰好一条")
        t2 = self.calls[1]["text"]
        self.assertTrue(t2.startswith("⚠️"), f"持续异常必须在心跳里标⚠️: {t2[:20]}")
        self.assertIn("行情覆盖低", t2)
        self.assertIn("涨停前三", t2, "心跳内容不能因异常而消失")

    def test_04_recovered_round_back_to_plain_heartbeat(self):
        res_bad = {"ok": False, "stats": self._ok_stats(),
                   "anomalies": [{"kind": "low_coverage", "msg": "覆盖低"}]}
        self._run_round(res_bad)
        self._run_round({"ok": True, "anomalies": [], "stats": self._ok_stats()})
        self.assertTrue(self.calls[1]["text"].startswith("📊"), "恢复后不再带⚠️")


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

    def test_04_coverage_threshold_now_has_headroom(self):
        """阈值 0.5 是**结构性死判据**（实测 32 个存档交易日覆盖恒 1.0000）⇒ 收紧后必须真会响。

        2026-10-12 排查：0.5 与实测差 50 个百分点，等于"整个行情源死掉"才够得着 ⇒ 上线以来
        一次都没触发。这里双向钉死新口径：接近生产实测最低值(0.996)不报，明显退化要报。
        """
        import pandas as pd
        _d = {"summary": pd.DataFrame([{"板块": "x"}]), "actual_data_dates": set()}
        res = self._poll_with({**_d, "watchlist": self._wl(cov_rows=96)}, [{"x": 1}])
        self.assertNotIn("low_coverage", [a["kind"] for a in res["anomalies"]],
                         "96% 覆盖（贴近生产实测最低 99.6%）不该报")
        res = self._poll_with({**_d, "watchlist": self._wl(cov_rows=90)}, [{"x": 1}])
        self.assertIn("low_coverage", [a["kind"] for a in res["anomalies"]],
                      "90% 覆盖应报（阈值 0.5 时代这里会静默放过）")

    def test_05_pipeline_internal_warn_is_flagged(self):
        """管线取数失败**不抛异常**、只 print 一行 WARN 然后继续 ⇒ 必须能从 stdout 捞到。

        这是"轮询报异常效果不好"的主因：真出事那天（北交所整批失败）它三轮全报「正常」。
        """
        import pandas as pd
        import core.hunter_poll as hp
        orig_ensure = hp._ensure_path
        hp._ensure_path = lambda: None
        try:
            import stock_hunter.main as H
            orig_pd, orig_ss = H.prepare_data, H.score_stocks

            def _pd_with_warn(cfg, d):
                print("  [WARN] 拉取超时（>60s），返回部分结果 900/1492 只")
                return {"watchlist": self._wl(), "summary": pd.DataFrame([{"板块": "x"}]),
                        "actual_data_dates": set()}

            H.prepare_data = _pd_with_warn
            H.score_stocks = lambda d, cfg: [{"x": 1}]
            try:
                res = hp.poll_once("2026-10-11")
            finally:
                H.prepare_data, H.score_stocks = orig_pd, orig_ss
        finally:
            hp._ensure_path = orig_ensure
        self.assertIn("data_warn", [a["kind"] for a in res["anomalies"]])
        self.assertIn("拉取超时", res["anomalies"][0]["msg"])

    def test_06_bse_noise_exempt_but_mixed_kept(self):
        """北交所整批不通是**已知常驻** ⇒ 不报；但混着真失败的行不许一起被吞掉。"""
        dead = ["  [WARN] 7 只获取失败（已跳过）: 920371, 920786, 920126",
                "  [WARN] 北交所日线不可得(920126)：RemoteDisconnected",
                "  [OK] 历史数据加载完成: 1492 只"]
        self.assertEqual(HP._pipeline_warnings(dead), [], "纯北交所行不得报警（否则每轮永久噪声）")
        mixed = ["  [WARN] 2 只获取失败（已跳过）: 920371, 000001"]
        self.assertEqual(len(HP._pipeline_warnings(mixed)), 1, "含非北交所代码 ⇒ 必须保留")
        generic = ["  [ERR] 数据加载失败，程序终止"]
        self.assertEqual(len(HP._pipeline_warnings(generic)), 1, "通用告警必须保留")

    def test_07_logging_failures_are_captured(self):
        """走 `log.warning` 的失败也必须被抓到 —— `redirect_stdout` 看不见 logging（2026-10-12）。

        这是"轮询报异常效果不好"的第二个根因：管线有一类失败只记日志（实测
        `bj_daily.py:100` / `facade.py:256`），若只靠 stdout 捕获就永远漏掉。
        """
        import logging as _lg
        import pandas as pd
        import core.hunter_poll as hp
        orig_ensure = hp._ensure_path
        hp._ensure_path = lambda: None
        try:
            import stock_hunter.main as H
            orig_pd, orig_ss = H.prepare_data, H.score_stocks

            def _pd_with_log(cfg, d):
                _lg.getLogger("core.market_data.facade").warning(
                    "daily_many: 500 只取数失败(000001, 600000)，跳过剩余")   # 非北交所 ⇒ 要报
                return {"watchlist": self._wl(), "summary": pd.DataFrame([{"板块": "x"}]),
                        "actual_data_dates": set()}

            H.prepare_data = _pd_with_log
            H.score_stocks = lambda d, cfg: [{"x": 1}]
            try:
                res = hp.poll_once("2026-10-11")
            finally:
                H.prepare_data, H.score_stocks = orig_pd, orig_ss
        finally:
            hp._ensure_path = orig_ensure
        self.assertIn("data_warn", [a["kind"] for a in res["anomalies"]],
                      "log.warning 的失败必须进异常（否则这类失败永远不可见）")
        self.assertIn("daily_many", res["stats"].get("log_warns", [""])[0])

    def test_08_log_capture_is_thread_scoped(self):
        """只收**本线程**的日志：别的线程/任务的告警不该算进本轮轮询。"""
        import logging as _lg
        import threading as _th
        import core.hunter_poll as hp
        cap = hp._ThreadWarnCapture()
        _root = _lg.getLogger()
        _root.addHandler(cap)
        try:
            _lg.getLogger("core.market_data").warning("本线程告警: 100 只失败(000001)")
            done = _th.Event()

            def _other():
                _lg.getLogger("core.market_data").warning("别的线程告警: 999 只失败(600000)")
                done.set()
            _th.Thread(target=_other).start()
            done.wait(3)
        finally:
            _root.removeHandler(cap)
        self.assertEqual(len(cap.lines), 1, f"只应收到本线程那条，实际 {cap.lines}")
        self.assertIn("本线程告警", cap.lines[0])

    def test_09_log_lines_lack_bracket_tag(self):
        """⚠️ 回归：logging 的 `getMessage()` **不带 `[WARN]` 前缀** ⇒ 两个通道判据必须分开。

        实测踩到：`log_warns` 有值、`pipeline_warns` 却是空的 —— 因为把 stdout 的
        "必须含 `[WARN]`"判据套到了 logging 行上，全被滤掉 ⇒ 新判据看似接线、实则永不触发。
        """
        raw = "daily_many: 500 只取数失败(000001)，跳过剩余"      # 无 [WARN] 前缀
        self.assertNotIn("[WARN]", raw)
        self.assertEqual(HP._pipeline_warnings([raw]), [], "stdout 判据要求方括号标签")
        self.assertEqual(HP._log_warnings([raw]), [raw], "logging 判据不得要求方括号标签")
        # 两条通道都要做已知常驻豁免
        self.assertEqual(HP._log_warnings(["北交所日线不可得(920126)：超时"]), [])

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
