# -*- coding: utf-8 -*-
"""飞书推送降噪（2026-11-04 owner：「消息太多，看起来很不方便」）。

当天实测 `t_io/logs/pushes_2026-11-04.jsonl` 共 **151 条**，噪音集中在：
  · 突破预警 66 条（同一只票来回穿框反复推，且没有开关）；
  · 异常监控池·刚站上5日线 34 条（池子 141 只，逐只即时推）。

方案 A（owner 选定）：冷却 + 汇总。
  ① 突破预警：同票同向 10 分钟冷却；全局限 60s 一条、每小时 30 条；
     被压住的**攒起来合并**成一条（不丢事件）；状态落盘 `t_io/state/push_rate.json`；
     补上 `notify_on_price_alert` 开关。
  ② 池站上5日线：10 分钟**窗口汇总**一张卡（同票当天只报一次；收盘补发）。

运行：python -m unittest tests.phase3.test_feishu_push_throttle -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from core import hunter_ma5_alert as ha  # noqa: E402
from core import jiuyan_tags as core_label  # noqa: E402
import core.price_alert_watch as paw  # noqa: E402  # 2026-11-13：突破预警 watcher 已抽离 main.py


def setUpModule():
    """闸门隔离：把「只播多头」闸的日线目录指到**空临时目录** ⇒ 判不了 ⇒ fail-open，
    保持本文件原有的节流/汇总断言不变。闸门本身的行为见 test_stage_gate.py。"""
    from core import stage_gate
    stage_gate.DAILY_DIR = Path(tempfile.mkdtemp(prefix="stage_gate_empty_"))
    stage_gate.clear_cache()


def _hit(code="600000", side="up", ts="2026-11-04T10:00:00"):
    return {"code": code, "name": code, "side": side, "price": 10.0,
            "level": 10.0, "n": 1, "ts": ts}


class TestPriceAlertThrottle(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.fp = str(Path(self._td.name) / "push_rate.json")
        self.calls = []
        self._p = [
            mock.patch.object(paw, "_PUSH_RATE_FP", self.fp),
            mock.patch.object(paw, "_price_alert_feishu_enabled", lambda: True),
            mock.patch.object(core_label, "label_of", lambda c: {"board": "", "concepts": []}),
            mock.patch.object(paw, "send_feishu_payload",
                              side_effect=lambda *a, **k: (self.calls.append(a[0]), True)[1]),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self._td.cleanup()

    def _codes(self, card):
        el = card["card"]["elements"][0]
        return el

    def test_首条立即推_同票同向十分钟内不再推(self):
        t0 = datetime(2026, 11, 4, 10, 0).timestamp()
        paw._push_price_alert([_hit()], now_ts=t0)
        self.assertEqual(len(self.calls), 1)
        paw._push_price_alert([_hit()], now_ts=t0 + 120)     # 同票同向，冷却中
        self.assertEqual(len(self.calls), 1, "同票同向 10 分钟内不重复推")

    def test_全局间隔不足时合并到下一拍(self):
        t0 = datetime(2026, 11, 4, 10, 0).timestamp()
        paw._push_price_alert([_hit("600000")], now_ts=t0)
        self.assertEqual(len(self.calls), 1)
        paw._push_price_alert([_hit("300001")], now_ts=t0 + 10)   # 距上条 <60s ⇒ 攒着
        self.assertEqual(len(self.calls), 1)
        paw._push_price_alert([_hit("002929")], now_ts=t0 + 20)   # 继续攒
        self.assertEqual(len(self.calls), 1)
        paw._push_price_alert([], now_ts=t0 + 121)                # 到 120s ⇒ 合并发一条
        self.assertEqual(len(self.calls), 2)
        self.assertIn("合并了 2 次穿越", str(self.calls[1]))

    def test_每小时封顶后先攒着(self):
        t0 = datetime(2026, 11, 4, 10, 0).timestamp()
        with mock.patch.object(paw, "_PRICE_ALERT_MIN_GAP_SEC", 1):
            for i in range(paw._PRICE_ALERT_HOURLY_MAX):
                paw._push_price_alert([_hit(f"60{i:04d}")], now_ts=t0 + i)
            self.assertEqual(len(self.calls), paw._PRICE_ALERT_HOURLY_MAX)
            paw._push_price_alert([_hit("300999")], now_ts=t0 + 40)   # 一小时内已满
        self.assertEqual(len(self.calls), paw._PRICE_ALERT_HOURLY_MAX, "超过 30 条/小时先攒着")
        d = json.loads(Path(self.fp).read_text(encoding="utf-8"))
        self.assertTrue(d.get("pending"), "积压要落盘")
        self.assertEqual(len(d["pending"]), 1)

    def test_开关关掉就不推(self):
        with mock.patch.object(paw, "_price_alert_feishu_enabled", lambda: False):
            paw._push_price_alert([_hit()], now_ts=datetime(2026, 11, 4, 10, 0).timestamp())
        self.assertEqual(self.calls, [])

    def test_发送失败保留积压(self):
        t0 = datetime(2026, 11, 4, 10, 0).timestamp()
        with mock.patch.object(paw, "send_feishu_payload", return_value=False):
            paw._push_price_alert([_hit()], now_ts=t0)
        d = json.loads(Path(self.fp).read_text(encoding="utf-8"))
        self.assertEqual(len(d.get("pending") or []), 1, "失败要保留，下一拍重试")


class TestPoolDigest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dedup_fp = Path(self._td.name) / "dedup.json"
        self.calls = []
        self._p = [
            mock.patch.object(ha, "_DEDUP_FP", self.dedup_fp),
            mock.patch.object(config, "send_feishu_payload",
                              side_effect=lambda *a, **k: (self.calls.append(a[0]), True)[1]),
        ]
        for p in self._p:
            p.start()
        ha._DIGEST.update({"day": "", "pending": [], "last_flush": 0.0})

    def tearDown(self):
        for p in self._p:
            p.stop()
        ha._DIGEST.update({"day": "", "pending": [], "last_flush": 0.0})
        self._td.cleanup()

    def _e(self, code):
        return {"code": code, "name": code, "kind": "overnight",
                "price": 10.0, "ma5": 9.5, "dev5_pct": 5.0}

    def test_十分钟窗口汇总一张卡(self):
        t0 = datetime(2026, 11, 4, 10, 0, 0)
        out = ha.push_events_batched([self._e("600000")], date="2026-11-04", now=t0)
        self.assertEqual(out, [], "窗口内先攒着")
        self.assertEqual(self.calls, [])
        out = ha.push_events_batched([self._e("300001")], date="2026-11-04",
                                     now=t0 + timedelta(minutes=5))
        self.assertEqual(out, [])
        out = ha.push_events_batched([], date="2026-11-04", now=t0 + timedelta(minutes=10))
        self.assertEqual([e["code"] for e in out], ["600000", "300001"])
        self.assertEqual(len(self.calls), 1, "10 分钟只发一张卡")
        self.assertIn("10 分钟汇总", json.dumps(self.calls[0], ensure_ascii=False))
        # 已推过的票当天不再进池
        out = ha.push_events_batched([self._e("600000")], date="2026-11-04",
                                     now=t0 + timedelta(minutes=20))
        self.assertEqual(out, [])

    def test_攒够20只提前发(self):
        t0 = datetime(2026, 11, 4, 10, 0, 0)
        events = [self._e(f"60{i:04d}") for i in range(20)]
        out = ha.push_events_batched(events, date="2026-11-04", now=t0)
        self.assertEqual(len(out), 20)
        self.assertEqual(len(self.calls), 1)

    def test_收盘补发(self):
        t0 = datetime(2026, 11, 4, 14, 58, 0)
        ha.push_events_batched([self._e("600000")], date="2026-11-04", now=t0)
        self.assertEqual(self.calls, [])
        out = ha.push_events_batched([], date="2026-11-04", now=t0 + timedelta(minutes=3))
        self.assertEqual([e["code"] for e in out], ["600000"])

    def test_发送失败保留积压(self):
        t0 = datetime(2026, 11, 4, 10, 0, 0)
        with mock.patch.object(config, "send_feishu_payload", return_value=False):
            out = ha.push_events_batched([self._e("600000")], date="2026-11-04",
                                         now=t0 + timedelta(minutes=10))
        self.assertEqual(out, [])
        self.assertEqual(len(ha._DIGEST["pending"]), 1)


class TestSwitch(unittest.TestCase):
    def test_突破预警开关默认开(self):
        with mock.patch.object(paw, "load_runtime_config", lambda: {"feishu": {}}):
            self.assertTrue(paw._price_alert_feishu_enabled())
        with mock.patch.object(paw, "load_runtime_config",
                               lambda: {"feishu": {"notify_on_price_alert": False}}):
            self.assertFalse(paw._price_alert_feishu_enabled())
        with mock.patch.object(paw, "load_runtime_config", lambda: {"feishu": {"enabled": False}}):
            self.assertFalse(paw._price_alert_feishu_enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
