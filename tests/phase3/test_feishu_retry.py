# -*- coding: utf-8 -*-
"""gm_bridge/feishu.py 重试与错误带出 + watcher 推送留痕 error 字段（2026-10-14）。

背景：2026-10-14 10:00:05 OSC 买入 301373×300 成交，auto 侧成交推送 sent=false
（飞书未收到），记录无 error 字段；同日 manual 侧 71/71 全部 sent=true。
根因：auto 侧 gm_bridge/feishu.py 单次尝试、无重试，瞬时网络异常直接判负且
错误只 print 到 watcher stdout（当日 watcher 非 ops_guard 拉起，stdout 未落盘）。
manual 侧 config._send_feishu_payload 有 3 次尝试 + 递增退避。

本测试全程 mock urllib.request.urlopen，不联网、不发任何真实消息。

运行（owner 指定解释器）：
  "C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" -m unittest tests.phase3.test_feishu_retry -v
"""
import importlib
import io
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GM_BRIDGE = os.path.join(_ROOT, "execution", "auto", "_gm", "gm_bridge")
if _GM_BRIDGE not in sys.path:
    sys.path.insert(0, _GM_BRIDGE)

import feishu  # noqa: E402  (gm_bridge/feishu.py)


class _FakeResp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestFeishuRetry(unittest.TestCase):
    def setUp(self):
        # 保证 webhook 可解析（不触网，仅绕过空串短路）
        feishu._WEBHOOK_CACHE = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
        # 重试退避 time.sleep 打桩，测试不等真实秒数
        self._sleep_p = mock.patch("feishu.time.sleep")
        self._sleep = self._sleep_p.start()

    def tearDown(self):
        self._sleep_p.stop()
        feishu._WEBHOOK_CACHE = None

    def test_首次成功_不重试(self):
        with mock.patch("feishu.urllib.request.urlopen",
                        return_value=_FakeResp({"code": 0})) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertTrue(sent)
        self.assertEqual(err, "")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self._sleep.call_count, 0)

    def test_瞬时异常_重试后成功(self):
        """RemoteDisconnected 类瞬时故障：第一次抛异常，第二次成功 → sent=True。
        这就是 10-14 成交推送若带重试本可自愈的场景。"""
        from http.client import RemoteDisconnected
        with mock.patch("feishu.urllib.request.urlopen",
                        side_effect=[RemoteDisconnected("Remote end closed connection without response"),
                                     _FakeResp({"code": 0})]) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertTrue(sent, "瞬时异常重试后成功应判送达")
        self.assertEqual(err, "")
        self.assertEqual(m.call_count, 2)

    def test_持续异常_三次后判负并带出错误(self):
        with mock.patch("feishu.urllib.request.urlopen",
                        side_effect=TimeoutError("timed out")) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertFalse(sent)
        self.assertIn("TimeoutError", err)
        self.assertEqual(m.call_count, 3)

    def test_业务码非零_不重试(self):
        """飞书返回 code!=0（关键词/限流等确定性错误）重试无意义。"""
        with mock.patch("feishu.urllib.request.urlopen",
                        return_value=_FakeResp({"code": 19024, "msg": "Key Words Not Found"})) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertFalse(sent)
        self.assertIn("19024", err)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self._sleep.call_count, 0)

    def test_webhook缺失_不重试且报错(self):
        feishu._WEBHOOK_CACHE = ""
        with mock.patch("feishu.urllib.request.urlopen") as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertFalse(sent)
        self.assertIn("webhook", err)
        self.assertEqual(m.call_count, 0)

    def test_旧签名包装_返回bool(self):
        """send_feishu_payload 旧签名不变，返回纯 bool（watcher/guardian 现有调用不炸）。"""
        with mock.patch("feishu.urllib.request.urlopen",
                        return_value=_FakeResp({"StatusCode": 0})):
            sent = feishu.send_feishu_payload(payload={"msg_type": "text"}, success_log="",
                                              error_prefix="t",
                                              trigger_urgent_alarm_after_success=False)
        self.assertIs(sent, True)


class TestWatcherPushLogging(unittest.TestCase):
    """watcher._push：失败时 pushes 留痕必须带 error 字段（离线，BRIDGE_DIR 重定向 tmp）。"""

    def test_失败留痕带error(self):
        import tempfile
        import writer  # gm_bridge/writer.py，watcher 从它取 BRIDGE_DIR
        watcher = importlib.import_module("watcher")
        importlib.reload(watcher)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(watcher, "BRIDGE_DIR", td), \
                 mock.patch.object(watcher, "FEISHU_AVAILABLE", True), \
                 mock.patch.object(watcher, "_send_feishu_ex",
                                   lambda **kw: (False, "TimeoutError: timed out")):
                sent = watcher._push("🔵 成交 — 301373", "买入 300股 @ 109.70", "green")
            self.assertFalse(sent)
            files = [f for f in os.listdir(td) if f.startswith("pushes_")]
            self.assertEqual(len(files), 1)
            rec = json.loads(io.open(os.path.join(td, files[0]), encoding="utf-8").read().strip())
            self.assertFalse(rec["sent"])
            self.assertIn("error", rec, "sent=false 记录必须带 error 字段")
            self.assertIn("TimeoutError", rec["error"])

    def test_成功留痕不带error(self):
        import tempfile
        watcher = importlib.import_module("watcher")
        importlib.reload(watcher)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(watcher, "BRIDGE_DIR", td), \
                 mock.patch.object(watcher, "FEISHU_AVAILABLE", True), \
                 mock.patch.object(watcher, "_send_feishu_ex",
                                   lambda **kw: (True, "")):
                sent = watcher._push("🔵 成交 — 301373", "买入 300股 @ 109.70", "green")
            self.assertTrue(sent)
            files = [f for f in os.listdir(td) if f.startswith("pushes_")]
            rec = json.loads(io.open(os.path.join(td, files[0]), encoding="utf-8").read().strip())
            self.assertTrue(rec["sent"])
            self.assertNotIn("error", rec)

    def test_旧版feishu无_ex时回退旧调用(self):
        """_send_feishu_ex=None（旧 feishu 模块）时回退 _send_feishu，不炸。"""
        import tempfile
        watcher = importlib.import_module("watcher")
        importlib.reload(watcher)
        calls = {}
        with tempfile.TemporaryDirectory() as td:
            def _fake_old(**kw):
                calls.update(kw)
                return True
            with mock.patch.object(watcher, "BRIDGE_DIR", td), \
                 mock.patch.object(watcher, "FEISHU_AVAILABLE", True), \
                 mock.patch.object(watcher, "_send_feishu_ex", None), \
                 mock.patch.object(watcher, "_send_feishu", _fake_old):
                sent = watcher._push("t", "c", "red")
            self.assertTrue(sent)
            self.assertTrue(calls.get("trigger_urgent_alarm_after_success"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
