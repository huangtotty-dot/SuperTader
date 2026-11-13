# -*- coding: utf-8 -*-
"""Bar Replay 逐根回放（2026-11-14，TradingView「Bar Replay」落地）。

核心纯函数 `_replaySlice(period, cursor)`：把当前周期数据截到回放游标为止（未来那几根
**真的不存在**，不是遮罩）。用 node 跑 app.js 里的真函数。

口径（钉住）：
  · cursor=null 或 dates 为空 ⇒ 返回 null（不截）
  · end = max(1, min(n, floor(cursor)+1))，越界自动夹紧、至少留 1 根
  · 截 dates/ohlc/volume/amount/turnover/rsi/vwap/zero + ma[·] + macd{·} + boll{·}
  · 浅拷贝，绝不改动原 period（原数组长度不变）
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
    i = _JS.index("function _replaySlice")
    j = _JS.index("// 回放游标", i)
    return _JS[i:j]


def _run(period, cursor):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify(_replaySlice({json.dumps(period)}, {json.dumps(cursor)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _fixture():
    return {
        "dates": ["d0", "d1", "d2", "d3", "d4"],
        "ohlc": [[0, 1, 2, 3], [1, 2, 3, 4], [2, 3, 4, 5], [3, 4, 5, 6], [4, 5, 6, 7]],
        "volume": [10, 20, 30, 40, 50],
        "amount": [100, 200, 300, 400, 500],
        "ma": [[1, 2, 3, 4, 5], [2, 3, 4, 5, 6]],
        "macd": {"dif": [1, 2, 3, 4, 5], "dea": [1, 1, 1, 1, 1], "hist": [0, 0, 0, 0, 0]},
        "boll": {"mid": [1, 2, 3, 4, 5], "up": [2, 3, 4, 5, 6], "dn": [0, 1, 2, 3, 4]},
        "rsi": [50, 51, 52, 53, 54],
        "last_close": 5,
    }


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestReplaySlice(unittest.TestCase):
    def test_null_与空_dates_返回_null(self):
        self.assertIsNone(_run(_fixture(), None))
        self.assertIsNone(_run({"dates": []}, 0))

    def test_中间游标截到含游标根(self):
        out = _run(_fixture(), 2)
        self.assertEqual(out["dates"], ["d0", "d1", "d2"])
        self.assertEqual(len(out["ohlc"]), 3)
        self.assertEqual(len(out["volume"]), 3)
        self.assertEqual(len(out["amount"]), 3)
        self.assertEqual(len(out["rsi"]), 3)

    def test_嵌套数组一起截(self):
        out = _run(_fixture(), 2)
        self.assertEqual(out["ma"], [[1, 2, 3], [2, 3, 4]])
        self.assertEqual(out["macd"], {"dif": [1, 2, 3], "dea": [1, 1, 1], "hist": [0, 0, 0]})
        self.assertEqual(out["boll"], {"mid": [1, 2, 3], "up": [2, 3, 4], "dn": [0, 1, 2]})

    def test_标量字段保留(self):
        out = _run(_fixture(), 2)
        self.assertEqual(out["last_close"], 5)

    def test_游标_0_至少留_1_根(self):
        out = _run(_fixture(), 0)
        self.assertEqual(out["dates"], ["d0"])
        self.assertEqual(len(out["ohlc"]), 1)

    def test_越界夹紧(self):
        self.assertEqual(_run(_fixture(), 999)["dates"], ["d0", "d1", "d2", "d3", "d4"])  # 上夹到 n
        self.assertEqual(_run(_fixture(), 4)["dates"], ["d0", "d1", "d2", "d3", "d4"])   # n-1 = 全量
        self.assertEqual(_run(_fixture(), -5)["dates"], ["d0"])                            # 下夹到 1 根

    def test_不改原_period(self):
        p = _fixture()
        _run(p, 2)
        self.assertEqual(len(p["dates"]), 5, "浅拷贝 + slice 绝不能动原 period")
        self.assertEqual(len(p["ma"][0]), 5)


class TestReplayWiring(unittest.TestCase):
    def test_按钮接_toggleReplay(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="replayBtn"' in ln:
                found = True
                self.assertIn("toggleReplay()", ln)
        self.assertTrue(found, "index.html 缺 id=replayBtn 按钮")

    def test_控制函数存在(self):
        for fn in ("function toggleReplay()", "function _replayExit()",
                   "function _replayStep(delta)", "function _replayPlayPause()",
                   "function _replayToStart()", "function _replayToEnd()", "function _replayPaint()"):
            self.assertIn(fn, _JS)

    def test_渲染里接切片(self):
        # renderStockChart 必须把 period 换成截断后的副本，否则回放不隐藏未来根
        self.assertIn("const _rp = _replaySlice(period, _replayCursor);", _JS)
        self.assertIn("if (_rp) period = _rp;", _JS)

    def test_换周期与关窗退出回放(self):
        self.assertIn("if (_replayCursor != null) _replayExit();   // 回放游标", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
