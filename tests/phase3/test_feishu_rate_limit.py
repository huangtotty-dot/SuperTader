# -*- coding: utf-8 -*-
"""飞书限频退避 + 死信重投 单测（2026-11-23）。

历史事故：多路推送共用 webhook 触发频率限制时，推送静默丢失（仅 print 留痕）。
本测试钉死：
  auto 侧 gm_bridge/feishu.py
    ① 限频返回体（code 11232 / 频率关键词）→ 5s/15s/30s 长退避，最多重试 3 次；
    ② 限频重试耗尽 → 死信落盘 feishu_deadletter.jsonl（append 一行 JSON）；
    ③ 永久业务错误（关键词/签名）→ 不重试、不落死信（向后兼容）；
    ④ HTTP 429 → 限频通道；网络瞬时异常 → 维持 2s/4s 原节奏（向后兼容）；
    ⑤ resend_deadletters：成功移除 / 失败保留并计数 / 原子重写。
  manual 侧 config._send_feishu_payload：同款判据 + 同一死信文件（channel=manual）。

**一切网络全部 mock**（urlopen / requests.post 层），严禁触发真实 webhook——铁律。

运行：python -m unittest tests.phase3.test_feishu_rate_limit -v
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GM_BRIDGE = os.path.join(_ROOT, "execution", "auto", "_gm", "gm_bridge")
for _p in (_ROOT, _GM_BRIDGE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
import feishu  # noqa: E402  (gm_bridge/feishu.py)
import requests  # noqa: E402


class _FakeResp:
    """auto 侧 urlopen 的假响应（不触网）。"""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CfgResp:
    """manual 侧 requests.post 的假响应（不触网）。"""

    def __init__(self, body: dict = None, status: int = 200):
        self._body = {"code": 0} if body is None else body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Client Error: Too Many Requests")
            setattr(err, "respons" + "e", self)  # requests 异常响应属性（单数 response）
            raise err

    def json(self):
        return self._body


def _waits(sleep_mock):
    """从 time.sleep mock 提取退避序列。"""
    return [c.args[0] for c in sleep_mock.call_args_list]


# ════════════════════════════════════════════════════════════════
# auto 侧：限频判据（纯函数，不触网）
# ════════════════════════════════════════════════════════════════
class TestRateLimitJudgement(unittest.TestCase):
    def test_auto_返回体判据(self):
        T = feishu._is_rate_limit_body
        self.assertTrue(T({"code": 11232, "msg": "send messages too frequent"}))
        self.assertTrue(T({"code": 9499, "msg": "触发频率限制，请稍后重试"}))
        self.assertTrue(T({"code": 1, "msg": "Trigger frequency limit"}))
        self.assertTrue(T({"code": 1, "msg": "send messages Too Frequent"}))
        self.assertTrue(T({"code": 1, "msg": "rate limit reached"}))
        self.assertTrue(T({"StatusCode": 11232}))
        # 永久业务错误不得误判
        self.assertFalse(T({"code": 19024, "msg": "Key Words Not Found"}))
        self.assertFalse(T({"code": 19021, "msg": "sign match fail"}))
        self.assertFalse(T({"code": 1, "msg": "operate normally"}))  # operate 含 rate 子串但非词边界
        self.assertFalse(T("not a dict"))
        self.assertFalse(T(None))

    def test_auto_异常判据(self):
        e429 = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)
        self.assertTrue(feishu._is_rate_limit_exc(e429))
        self.assertTrue(feishu._is_rate_limit_exc(RuntimeError("频率限制")))
        self.assertFalse(feishu._is_rate_limit_exc(TimeoutError("timed out")))
        self.assertFalse(feishu._is_rate_limit_exc(ValueError("sign error")))

    def test_manual_返回体判据(self):
        T = config._is_feishu_rate_limit_result
        self.assertTrue(T({"code": 11232, "msg": "x"}))
        self.assertTrue(T({"code": 0, "msg": "频率"}))
        self.assertTrue(T({"code": 1, "msg": "Too Many Requests"}))
        self.assertFalse(T({"code": 19024, "msg": "Key Words Not Found"}))
        self.assertFalse(T({"code": 1, "msg": "operate normally"}))
        self.assertFalse(T(None))

    def test_manual_异常判据(self):
        err = requests.HTTPError("429 Client Error")
        setattr(err, "respons" + "e", _CfgResp(status=429))  # 单数 response 属性
        self.assertTrue(config._is_feishu_rate_limit_exc(err))
        self.assertFalse(config._is_feishu_rate_limit_exc(TimeoutError("timed out")))


# ════════════════════════════════════════════════════════════════
# auto 侧：限频退避 + 死信
# ════════════════════════════════════════════════════════════════
class _AutoBase(unittest.TestCase):
    def setUp(self):
        # 保证 webhook 可解析（不触网，仅绕过空串短路）
        feishu._WEBHOOK_CACHE = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
        self._tmp = tempfile.TemporaryDirectory()
        self.dl_path = os.path.join(self._tmp.name, "logs", "feishu_deadletter.jsonl")
        self._p_sleep = mock.patch("feishu.time.sleep")       # 退避打桩，不等真实秒数
        self.sleep = self._p_sleep.start()
        self._p_dl = mock.patch("feishu._deadletter_path", return_value=self.dl_path)
        self._p_dl.start()

    def tearDown(self):
        self._p_sleep.stop()
        self._p_dl.stop()
        feishu._WEBHOOK_CACHE = None
        self._tmp.cleanup()

    def read_dl(self):
        if not os.path.exists(self.dl_path):
            return []
        with open(self.dl_path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]


class TestAutoRateLimit(_AutoBase):
    def test_限频11232_长退避后成功(self):
        rl = _FakeResp({"code": 11232, "msg": "send messages too frequent"})
        with mock.patch("feishu.urllib.request.urlopen",
                        side_effect=[rl, rl, _FakeResp({"code": 0})]) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertTrue(sent, "限频重投后成功应判送达")
        self.assertEqual(err, "")
        self.assertEqual(m.call_count, 3)
        self.assertEqual(_waits(self.sleep), [5, 15], "限频必须走 5s/15s 长退避，不是 2s/4s")
        self.assertEqual(self.read_dl(), [], "成功不留死信")

    def test_限频耗尽_落死信(self):
        body = {"code": 11232, "msg": "触发频率限制"}
        with mock.patch("feishu.urllib.request.urlopen", return_value=_FakeResp(body)) as m:
            sent, err = feishu.send_feishu_payload_ex(
                {"msg_type": "text", "content": {"text": "掘金模拟盘 测试"}})
        self.assertFalse(sent)
        self.assertIn("11232", err)
        self.assertEqual(m.call_count, 4, "首试 + 3 次重投 = 4 次")
        self.assertEqual(_waits(self.sleep), [5, 15, 30])
        recs = self.read_dl()
        self.assertEqual(len(recs), 1, "限频耗尽必须落一条死信")
        rec = recs[0]
        self.assertEqual(rec["event"], "feishu_deadletter")
        self.assertEqual(rec["channel"], "auto")
        self.assertEqual(rec["attempts"], 4)
        self.assertEqual(rec["resend_attempts"], 0)
        self.assertIn("11232", rec["error"])
        self.assertEqual(rec["payload"]["msg_type"], "text", "payload 原文必须留存供重投")

    def test_永久错误_不重试不落死信(self):
        with mock.patch("feishu.urllib.request.urlopen",
                        return_value=_FakeResp({"code": 19024, "msg": "Key Words Not Found"})) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertFalse(sent)
        self.assertIn("19024", err)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertEqual(self.read_dl(), [], "永久错误不得落死信")

    def test_http429_走限频长退避(self):
        e429 = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)
        with mock.patch("feishu.urllib.request.urlopen",
                        side_effect=[e429, _FakeResp({"code": 0})]) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertTrue(sent)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(_waits(self.sleep), [5], "HTTP 429 必须识别为限频走 5s，不是 2s")

    def test_网络瞬时异常_维持原节奏(self):
        """向后兼容：非限频异常仍是 2s/4s，不落死信。"""
        with mock.patch("feishu.urllib.request.urlopen",
                        side_effect=[TimeoutError("timed out"), TimeoutError("timed out"),
                                     _FakeResp({"code": 0})]) as m:
            sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertTrue(sent)
        self.assertEqual(m.call_count, 3)
        self.assertEqual(_waits(self.sleep), [2, 4])
        self.assertEqual(self.read_dl(), [])

    def test_死信落盘失败_不阻断判负(self):
        """落盘路径坏了（父路径被文件占用）也不能把判负链路炸掉。"""
        blocker = os.path.join(self._tmp.name, "blocker")
        open(blocker, "w").close()                       # 占位普通文件
        bad = os.path.join(blocker, "dl.jsonl")          # 其父路径是文件 → makedirs/open 必炸
        with mock.patch("feishu._deadletter_path", return_value=bad):
            with mock.patch("feishu.urllib.request.urlopen",
                            return_value=_FakeResp({"code": 11232, "msg": "频率"})):
                sent, err = feishu.send_feishu_payload_ex({"msg_type": "text"})
        self.assertFalse(sent)
        self.assertIn("11232", err)


class TestAutoResend(_AutoBase):
    def _entry(self, i: int) -> dict:
        return {"event": "feishu_deadletter",
                "time": f"2026-11-23 10:00:0{i}", "_ts": 1.0,
                "channel": "auto", "error": '飞书限频: {"code": 11232}',
                "attempts": 4, "resend_attempts": 0, "title": f"t{i}",
                "payload": {"msg_type": "text", "content": {"text": f"掘金模拟盘 补投{i}"}}}

    def _seed(self, entries):
        os.makedirs(os.path.dirname(self.dl_path), exist_ok=True)
        with open(self.dl_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_resend_成功移除(self):
        self._seed([self._entry(1), self._entry(2)])
        with mock.patch("feishu.urllib.request.urlopen",
                        return_value=_FakeResp({"code": 0})) as m:
            r = feishu.resend_deadletters()
        self.assertEqual(m.call_count, 2)
        self.assertEqual((r["total"], r["resent"], r["kept"]), (2, 2, 0))
        self.assertEqual(self.read_dl(), [], "全部重投成功 → 死信文件原子重写为空")

    def test_resend_失败保留并计数(self):
        self._seed([self._entry(1), self._entry(2)])
        with mock.patch("feishu.urllib.request.urlopen", side_effect=TimeoutError("t")):
            r = feishu.resend_deadletters()
        self.assertEqual((r["resent"], r["kept"]), (0, 2))
        recs = self.read_dl()
        self.assertEqual(len(recs), 2, "失败的死信必须保留")
        for rec in recs:
            self.assertEqual(rec["resend_attempts"], 1, "重投失败计数必须 +1")
            self.assertIn("TimeoutError", rec["last_resend_error"])
            self.assertIn("last_resend_time", rec)

    def test_resend_部分成功(self):
        self._seed([self._entry(1), self._entry(2)])
        with mock.patch("feishu.urllib.request.urlopen",
                        side_effect=[_FakeResp({"code": 0}), TimeoutError("t")]):
            r = feishu.resend_deadletters()
        self.assertEqual((r["resent"], r["kept"]), (1, 1))
        recs = self.read_dl()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["title"], "t2", "成功的那条必须移除，失败的保留")

    def test_resend_坏行原样保留(self):
        os.makedirs(os.path.dirname(self.dl_path), exist_ok=True)
        with open(self.dl_path, "w", encoding="utf-8") as f:
            f.write("这不是JSON\n")
            f.write(json.dumps(self._entry(1), ensure_ascii=False) + "\n")
        with mock.patch("feishu.urllib.request.urlopen",
                        return_value=_FakeResp({"code": 0})):
            r = feishu.resend_deadletters()
        self.assertEqual((r["resent"], r["kept"]), (1, 1))
        with open(self.dl_path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "这不是JSON", "坏行必须原样保留不丢数据")

    def test_resend_文件不存在_安全返回(self):
        r = feishu.resend_deadletters()
        self.assertEqual((r["total"], r["resent"], r["kept"]), (0, 0, 0))


# ════════════════════════════════════════════════════════════════
# manual 侧：config._send_feishu_payload 同款限频通道
# ════════════════════════════════════════════════════════════════
class TestManualRateLimit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p_hook = mock.patch.object(
            config, "FEISHU_WEBHOOK",
            "https://open.feishu.cn/open-apis/bot/v2/hook/test-token")
        self._p_sleep = mock.patch("config.time.sleep")      # 退避打桩
        self._p_log = mock.patch.object(config, "LOG_DIR", self._tmp.name)
        self._p_hook.start()
        self.sleep = self._p_sleep.start()
        self._p_log.start()
        self.dl_path = os.path.join(self._tmp.name, "feishu_deadletter.jsonl")

    def tearDown(self):
        self._p_hook.stop()
        self._p_sleep.stop()
        self._p_log.stop()
        self._tmp.cleanup()

    def read_dl(self):
        if not os.path.exists(self.dl_path):
            return []
        with open(self.dl_path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_manual_限频11232_重试后成功(self):
        with mock.patch.object(config.requests, "post",
                               side_effect=[_CfgResp({"code": 11232, "msg": "频率限制"}),
                                            _CfgResp({"code": 0})]) as m:
            ok = config._send_feishu_payload({"msg_type": "text"}, "ok", "test")
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(_waits(self.sleep), [5], "manual 侧限频必须走 5s 长退避")

    def test_manual_限频耗尽_落死信(self):
        with mock.patch.object(config.requests, "post",
                               return_value=_CfgResp({"code": 11232, "msg": "频率限制"})) as m:
            ok = config._send_feishu_payload(
                {"msg_type": "text", "content": {"text": "做T猎手预警 x"}}, "ok", "test")
        self.assertFalse(ok)
        self.assertEqual(m.call_count, 4, "首试 + 3 次重投 = 4 次")
        self.assertEqual(_waits(self.sleep), [5, 15, 30])
        recs = self.read_dl()
        self.assertEqual(len(recs), 1, "manual 侧限频耗尽必须落死信（同一文件）")
        self.assertEqual(recs[0]["channel"], "manual")
        self.assertEqual(recs[0]["attempts"], 4)
        self.assertEqual(recs[0]["payload"]["msg_type"], "text")

    def test_manual_永久错误_不重试不落死信(self):
        with mock.patch.object(config.requests, "post",
                               return_value=_CfgResp({"code": 19024,
                                                      "msg": "Key Words Not Found"})) as m:
            ok = config._send_feishu_payload({"msg_type": "text"}, "ok", "test")
        self.assertFalse(ok)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertEqual(self.read_dl(), [])

    def test_manual_http429_走限频(self):
        with mock.patch.object(config.requests, "post",
                               side_effect=[_CfgResp(status=429), _CfgResp({"code": 0})]) as m:
            ok = config._send_feishu_payload({"msg_type": "text"}, "ok", "test")
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(_waits(self.sleep), [5], "HTTP 429 必须识别为限频走 5s")

    def test_manual_网络异常_维持原节奏(self):
        """向后兼容：非限频异常仍是 2s/4s，共 3 次尝试。"""
        with mock.patch.object(config.requests, "post",
                               side_effect=[requests.ConnectionError("x"),
                                            requests.ConnectionError("x"),
                                            _CfgResp({"code": 0})]) as m:
            ok = config._send_feishu_payload({"msg_type": "text"}, "ok", "test")
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 3)
        self.assertEqual(_waits(self.sleep), [2, 4])
        self.assertEqual(self.read_dl(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
