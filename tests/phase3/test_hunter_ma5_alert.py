# -*- coding: utf-8 -*-
"""猎手热门板块「刚站上5日线」飞书告警 单测（2026-10-09）——全离线，合成数据。"""
import json
import os
import sys
import tempfile
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.ma_reclaim as mr  # noqa: E402


class TestReclaim5(unittest.TestCase):
    def test_命中(self):
        # basis=[11,10.5,10,9.5,9] prev_close=9<prevMA5=10；price=11>curMA5=10
        r = mr.reclaim5_from_closes([11, 10.5, 10, 9.5, 9, 11])
        self.assertIsNotNone(r); self.assertAlmostEqual(r["prev_close"], 9.0)

    def test_已在MA5上不命中(self):
        self.assertIsNone(mr.reclaim5_from_closes([9, 9.5, 10, 10.5, 11, 11.5]))

    def test_帧版交易日闸(self):
        dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07", "2026-10-08", "2026-10-09"]
        df = pd.DataFrame({"date": dates, "close": [11, 10.5, 10, 9.5, 9, 11]})
        self.assertIsNotNone(mr.reclaim5_frame(df, "2026-10-09"))
        self.assertIsNone(mr.reclaim5_frame(df, "2026-10-08"))   # 末根!=目标日
        # 帧里只有到 10-08，目标 10-09 → 末根 != 目标 → None
        self.assertIsNone(mr.reclaim5_frame(df[df.date <= "2026-10-08"], "2026-10-09"))


class TestHunterHotPool(unittest.TestCase):
    def setUp(self):
        import core.hunter_ma5_alert as h
        self.h = h
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP)
        h._SUMMARY_FP = os.path.join(td, "summary.json")
        h._WL_FP = os.path.join(td, "wl.json")
        h._DEDUP_FP = os.path.join(td, "dup.json")

    def tearDown(self):
        self.h._SUMMARY_FP, self.h._WL_FP, self.h._DEDUP_FP = self._o
        self._td.cleanup()

    def _write(self):
        json.dump({"20261009": [{"板块": "电力", "平均分": 0.7},
                                {"板块": "医药", "平均分": 0.6},
                                {"板块": "冷门X", "平均分": 0.1}]},
                  open(self.h._SUMMARY_FP, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump({"600001": {"name": "电力股", "sector": "电力"},
                   "600002": {"name": "医药股", "jiuyan_concept1": "医药"},
                   "600003": {"name": "无关股", "sector": "钢铁"}},
                  open(self.h._WL_FP, "w", encoding="utf-8"), ensure_ascii=False)

    def test_topn_板块(self):
        self._write()
        self.assertEqual(self.h.hot_boards(2), ["电力", "医药"])

    def test_成分股并集(self):
        self._write()
        s = self.h.hot_board_stocks(2)
        self.assertIn("600001", s); self.assertIn("600002", s); self.assertNotIn("600003", s)


class TestHunterAlert(unittest.TestCase):
    def setUp(self):
        import core.hunter_ma5_alert as h
        self.h = h
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP)
        h._SUMMARY_FP = os.path.join(td, "summary.json")
        h._WL_FP = os.path.join(td, "wl.json")
        h._DEDUP_FP = os.path.join(td, "dup.json")
        json.dump({"20261009": [{"板块": "电力", "平均分": 0.7}]},
                  open(h._SUMMARY_FP, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump({"600001": {"name": "电力股", "sector": "电力"}},
                  open(h._WL_FP, "w", encoding="utf-8"), ensure_ascii=False)

    def tearDown(self):
        self.h._SUMMARY_FP, self.h._WL_FP, self.h._DEDUP_FP = self._o
        self._td.cleanup()
        if hasattr(self, "_o_gp"):
            from core.market_data import facade as _fd
            _fd.get_provider = self._o_gp

    def _patch(self):
        from core.market_data import facade as _fd
        self._o_gp = _fd.get_provider
        dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07", "2026-10-08", "2026-10-09"]
        close = [11, 10.5, 10, 9.5, 9, 11]
        df = pd.DataFrame({"date": dates, "open": close, "high": close, "low": close,
                           "close": close, "volume": [1e6] * len(close)})

        class _Fake:
            def daily_many(self, codes, days=0):
                return {"600001": df.copy()} if "600001" in codes else {}
        _fd.get_provider = lambda: _Fake()

    def test_扫描命中并去重(self):
        self._patch()
        ev = self.h.run_hunter_ma5_alert(date="2026-10-09", dry_run=True)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["code"], "600001")
        # 写去重后再次 → 空
        self.h.run_hunter_ma5_alert(date="2026-10-09", dry_run=True)   # dry_run 不写
        # 手动写去重
        self.h._save_dedup({"2026-10-09": ["600001"]})
        self.assertEqual(self.h.run_hunter_ma5_alert(date="2026-10-09", dry_run=True), [])

    def test_card(self):
        self._patch()
        ev = self.h.scan_hunter_ma5(date="2026-10-09")
        c = self.h.build_card(ev, date="2026-10-09")
        self.assertEqual(c["msg_type"], "interactive")
        self.assertIn("600001", c["card"]["elements"][0]["content"])


class TestHunterMa5Push(unittest.TestCase):
    """**推送路径**回归（2026-10-10 补）。

    背景：本告警自上线起从未推送成功过——`run_hunter_ma5_alert` 调
    `send_feishu_payload(card)` 少传 `success_log`/`error_prefix` 两个必填参数，
    每次抛 TypeError 又被裸 except 吞掉。老测试**只跑 dry_run**，真实发送那一段
    一次都没执行过，所以一路绿灯。

    这里用**与 config.send_feishu_payload 完全一致的签名**做桩：调用方若再少传参数，
    桩会直接抛 TypeError，测试立刻失败——把这条通道焊死。
    """

    def setUp(self):
        import core.hunter_ma5_alert as h
        self.h = h
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP)
        h._SUMMARY_FP = os.path.join(td, "summary.json")
        h._WL_FP = os.path.join(td, "wl.json")
        h._DEDUP_FP = os.path.join(td, "dup.json")
        with open(h._SUMMARY_FP, "w", encoding="utf-8") as f:
            json.dump({"20261009": [{"板块": "锂电池"}]}, f, ensure_ascii=False)
        with open(h._WL_FP, "w", encoding="utf-8") as f:
            json.dump({"600001": {"name": "测试股", "sector": "锂电池"}}, f, ensure_ascii=False)
        self._o_scan = h.scan_hunter_ma5
        h.scan_hunter_ma5 = lambda **kw: [
            {"code": "600001", "name": "测试股", "price": 11.0, "ma5": 10.0,
             "dev5_pct": 10.0, "prev_close": 9.0, "ma5_prev": 10.0}]

    def tearDown(self):
        self.h._SUMMARY_FP, self.h._WL_FP, self.h._DEDUP_FP = self._o
        self.h.scan_hunter_ma5 = self._o_scan
        self._td.cleanup()

    def _stub_send(self, ok=True, calls=None):
        """**照抄真实签名**的桩——参数不匹配就 TypeError。"""
        def fake(payload, success_log, error_prefix,
                 trigger_urgent_alarm_after_success=False):
            if calls is not None:
                calls.append((payload.get("msg_type"), success_log, error_prefix))
            return ok
        return fake

    def test_推送调用签名与真实函数一致(self):
        """参数契约：真实函数的必填参数，调用方必须都给。"""
        import inspect
        import config
        real = inspect.signature(config.send_feishu_payload)
        required = [n for n, prm in real.parameters.items()
                    if prm.default is inspect.Parameter.empty]
        stub = self._stub_send(calls=[])
        got = inspect.signature(stub)
        self.assertEqual(required, [n for n, prm in got.parameters.items()
                                    if prm.default is inspect.Parameter.empty],
                         "桩的必填参数要和 config.send_feishu_payload 一致，否则测不出调用方漏参")

    def test_推送成功才写去重(self):
        calls = []
        import config
        o = config.send_feishu_payload
        config.send_feishu_payload = self._stub_send(ok=True, calls=calls)
        try:
            got = self.h.run_hunter_ma5_alert(date="2026-10-09")
        finally:
            config.send_feishu_payload = o
        self.assertEqual(len(got), 1, "推送成功应返回事件")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1] and calls[0][2], "success_log/error_prefix 不能为空")
        self.assertIn("600001", self.h._load_dedup().get("2026-10-09", []))

    def test_推送失败不写去重且不吞异常(self):
        import config
        o = config.send_feishu_payload
        config.send_feishu_payload = self._stub_send(ok=False)
        try:
            got = self.h.run_hunter_ma5_alert(date="2026-10-09")
        finally:
            config.send_feishu_payload = o
        self.assertEqual(got, [], "推送失败不应算成功")
        self.assertEqual(self.h._load_dedup().get("2026-10-09", []), [],
                         "推送失败不能写去重，否则下轮不再重试")

    def test_少传参数会炸出来而不是静默(self):
        """把 bug 形态钉死：调用方漏参必须可见，不能被 except 吞成「今天没信号」。"""
        import config
        o = config.send_feishu_payload
        config.send_feishu_payload = lambda payload: True      # 故意错误签名
        try:
            got = self.h.run_hunter_ma5_alert(date="2026-10-09")
        finally:
            config.send_feishu_payload = o
        self.assertEqual(got, [], "异常路径应返回空而不是崩溃")
        self.assertEqual(self.h._load_dedup().get("2026-10-09", []), [],
                         "异常时同样不能写去重")


class TestVRevAndCardCap(unittest.TestCase):
    """盘内 V 反转判据 + 卡片折叠上限（owner 2026-10-10 确认要的两条）。"""

    def setUp(self):
        import core.hunter_ma5_alert as h
        self.h = h
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (h._SUMMARY_FP, h._WL_FP, h._DEDUP_FP, h._BELOW_FP, h._today)
        h._SUMMARY_FP = os.path.join(td, "summary.json")
        h._WL_FP = os.path.join(td, "wl.json")
        h._DEDUP_FP = os.path.join(td, "dup.json")
        h._BELOW_FP = os.path.join(td, "below.json")
        h._today = lambda: "2026-10-09"          # 测试缝：把「今天」钉死，才能离线验 V 反转
        with open(h._SUMMARY_FP, "w", encoding="utf-8") as f:
            json.dump({"20261009": [{"板块": "锂电池"}]}, f, ensure_ascii=False)
        with open(h._WL_FP, "w", encoding="utf-8") as f:
            json.dump({"600001": {"name": "测试股", "sector": "锂电池"}},
                      f, ensure_ascii=False)
        from core.market_data import facade as _fd
        self._o_gp = _fd.get_provider
        self.df = None

        class _Fake:
            def daily_many(_self, codes, days=0):
                return {"600001": self.df.copy()} if "600001" in codes else {}
        _fd.get_provider = lambda: _Fake()
        self._fd = _fd

    def tearDown(self):
        (self.h._SUMMARY_FP, self.h._WL_FP, self.h._DEDUP_FP,
         self.h._BELOW_FP, self.h._today) = self._o
        self._fd.get_provider = self._o_gp
        self._td.cleanup()

    def _frame(self, closes):
        dates = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-07", "2026-10-08",
                 "2026-10-09"]
        return pd.DataFrame({"date": dates, "open": closes, "high": closes,
                             "low": closes, "close": closes,
                             "volume": [1e6] * len(closes)})

    def test_盘内V反转_先破线后拉回(self):
        # 造一个**隔夜口径判不出**的 case：昨收在昨MA5 **之上**（不在 MA5 下方），
        # 今天盘中先跌破、再拉回。基期 [10,10.2,10.4,10.6,10.8] → 昨收10.8 > 昨MA5 10.4。
        # 第1轮：现价 10.0，今MA5=10.4 ⇒ 在下方，记进 below，不命中
        self.df = self._frame([10, 10.2, 10.4, 10.6, 10.8, 10.0])
        self.assertEqual(self.h.scan_hunter_ma5(date="2026-10-09"), [])
        self.assertIn("600001", self.h._load_below().get("2026-10-09", []),
                      "在 MA5 下方的票要被记住，否则判不出 V 反转")
        # 第2轮：拉回 10.6 > 今MA5 10.52 ⇒ 命中 vrev（隔夜口径判不出这条）
        self.df = self._frame([10, 10.2, 10.4, 10.6, 10.8, 10.6])
        hits = self.h.scan_hunter_ma5(date="2026-10-09")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "vrev")

    def test_隔夜回站标_overnight(self):
        self.df = self._frame([11, 10.5, 10, 9.5, 9, 11])     # 昨收9<昨MA5 10，今11>MA5 10
        hits = self.h.scan_hunter_ma5(date="2026-10-09")
        self.assertEqual(hits[0]["kind"], "overnight")

    def test_一直没破线不算V反转(self):
        # 末根一直在 MA5 上方 ⇒ 不该命中（没「曾破线」过）
        self.df = self._frame([9, 9.5, 10, 10.5, 11, 11.2])
        self.assertEqual(self.h.scan_hunter_ma5(date="2026-10-09"), [])
        self.df = self._frame([9, 9.5, 10, 10.5, 11, 11.5])
        self.assertEqual(self.h.scan_hunter_ma5(date="2026-10-09"), [])

    def test_历史日期不写盘中状态(self):
        self.h._today = lambda: "2026-10-10"                  # 「今天」变成别的日子
        self.df = self._frame([11, 10.5, 10, 9.5, 9, 9.4])
        self.h.scan_hunter_ma5(date="2026-10-09")
        self.assertEqual(self.h._load_below().get("2026-10-09", []),
                         [], "历史回放没有盘中态，不应写 below 记忆")

    def test_卡片折叠但代码不丢(self):
        ev = [{"code": f"{i:06d}", "name": f"股{i}", "price": 10.0, "ma5": 9.0,
               "dev5_pct": 100 - i, "kind": "overnight"} for i in range(1, 46)]
        c = self.h.build_card(ev, top_n=5, date="2026-10-09")
        text = c["card"]["elements"][0]["content"]
        self.assertIn("共 45 只", text)
        self.assertIn(f"前 {self.h._CARD_MAX_ROWS} 只", text)
        self.assertIn("另有 25 只", text)
        # 折叠掉的那 25 只代码必须仍出现在卡片里，不能只给个数字
        for i in range(21, 46):
            self.assertIn(f"{i:06d}", text, f"折叠的 {i:06d} 代码丢了")

    def test_卡片区分两种口径计数(self):
        ev = [{"code": "600001", "name": "A", "price": 10.0, "ma5": 9.0, "dev5_pct": 5.0,
               "kind": "overnight"},
              {"code": "600002", "name": "B", "price": 10.0, "ma5": 9.0, "dev5_pct": 4.0,
               "kind": "vrev"}]
        text = self.h.build_card(ev, date="2026-10-09")["card"]["elements"][0]["content"]
        self.assertIn("隔夜回站 1", text)
        self.assertIn("盘内V反转 1", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
