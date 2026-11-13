# -*- coding: utf-8 -*-
"""统一告警流（2026-11-14，TradingView「Alert Webhook 统一事件流」落地）。

把散在各处的告警（画框上破/下破、建仓/加仓/买卖信号）归一成一个规范事件，滚进时间线
面板，并可序列化成统一 JSON 供 webhook 消费。node 跑 app.js 里的两个纯函数。

口径（钉住）：
  · normalizeAlertEvent：side=up/down ⇒ price 事件；有 type/decision ⇒ 信号事件；否则 null
  · 建仓/加仓 ⇒ kind jiancang/add、side buy、score 取 composite_score
  · SELL_HIGH/PANIC_SELL ⇒ side sell，其余决策 ⇒ buy
  · buildWebhookPayload：统一 {event,ts,code,name,side,price,level,score,text}
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _extract():
    i = _JS.index("function normalizeAlertEvent")
    j = _JS.index("let _alertStream", i)
    return _JS[i:j]


def _run(expr, arg):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify({expr}({json.dumps(arg, ensure_ascii=False)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestNormalizeAlertEvent(unittest.TestCase):
    def test_坏输入为_null(self):
        self.assertIsNone(_run("normalizeAlertEvent", None))
        self.assertIsNone(_run("normalizeAlertEvent", "x"))

    def test_价格上破(self):
        evt = _run("normalizeAlertEvent",
                   {"id": "e1", "ts": "2026-11-14 09:35:00", "side": "up",
                    "name": "贵州茅台", "code": "600519", "level": 1500, "price": 1505})
        self.assertEqual(evt["id"], "e1")
        self.assertEqual(evt["kind"], "price")
        self.assertEqual(evt["code"], "600519")
        self.assertEqual(evt["side"], "up")
        self.assertEqual(evt["level"], 1500)
        self.assertEqual(evt["price"], 1505)
        self.assertIn("上破", evt["text"])
        self.assertIn("贵州茅台", evt["text"])

    def test_价格下破(self):
        evt = _run("normalizeAlertEvent", {"side": "down", "name": "X", "code": "000001", "level": 10, "price": 9.8})
        self.assertEqual(evt["side"], "down")
        self.assertIn("下破", evt["text"])

    def test_建仓信号(self):
        evt = _run("normalizeAlertEvent",
                   {"type": "建仓", "name": "某票", "code": "000001",
                    "composite_score": 85.5, "scan_time": "2026-11-14 09:30:00"})
        self.assertEqual(evt["kind"], "jiancang")
        self.assertEqual(evt["side"], "buy")
        self.assertEqual(evt["score"], 85.5)
        self.assertIn("建仓信号", evt["text"])

    def test_加仓信号(self):
        evt = _run("normalizeAlertEvent", {"type": "加仓", "name": "某票", "code": "000001", "composite_score": 60})
        self.assertEqual(evt["kind"], "add")
        self.assertEqual(evt["side"], "buy")

    def test_卖出信号(self):
        evt = _run("normalizeAlertEvent",
                   {"decision": "SELL_HIGH", "name": "某票", "code": "000001", "score": 70})
        self.assertEqual(evt["kind"], "signal")
        self.assertEqual(evt["side"], "sell")
        self.assertEqual(evt["score"], 70)
        self.assertIn("卖出信号", evt["text"])

    def test_买入决策为_buy(self):
        evt = _run("normalizeAlertEvent", {"decision": "BUY_LOW", "name": "某票", "code": "000001", "score": 55})
        self.assertEqual(evt["side"], "buy")

    def test_恐慌卖为_sell(self):
        evt = _run("normalizeAlertEvent", {"decision": "PANIC_SELL", "name": "某票", "code": "000001", "score": 40})
        self.assertEqual(evt["side"], "sell")


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestWebhookPayload(unittest.TestCase):
    def test_统一_json(self):
        out = _run("buildWebhookPayload",
                   {"kind": "price", "ts": "T", "code": "600519", "name": "贵州茅台",
                    "side": "up", "price": 1505, "level": 1500, "score": None, "text": "..."})
        self.assertEqual(out, {"event": "price", "ts": "T", "code": "600519", "name": "贵州茅台",
                               "side": "up", "price": 1505, "level": 1500, "score": None, "text": "..."})

    def test_null_事件为_null(self):
        self.assertIsNone(_run("buildWebhookPayload", None))


class TestAlertStreamWiring(unittest.TestCase):
    def test_按钮接_toggleAlertStream(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="alertStreamBtn"' in ln:
                found = True
                self.assertIn("toggleAlertStream()", ln)
        self.assertTrue(found, "index.html 缺 id=alertStreamBtn 按钮")

    def test_函数存在(self):
        for fn in ("function alertStreamAdd(raw)", "function toggleAlertStream()",
                   "function _renderAlertStream()"):
            self.assertIn(fn, _JS)

    def test_pushAlert_入流(self):
        # pushAlert 里第一条就该把信号滚进告警流
        self.assertIn("alertStreamAdd(s);   // 统一告警流", _JS)

    def test_价格穿越_入流(self):
        self.assertIn("fresh.forEach(e => alertStreamAdd(e));", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
