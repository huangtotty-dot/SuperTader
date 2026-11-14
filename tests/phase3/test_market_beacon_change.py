# -*- coding: utf-8 -*-
"""市场风向标 · 变化感知（2026-11-14，owner：「发生了变化如何能让我感知到」）。

把上一轮的板块序与这一轮对比，只在**高信号**变化时提醒（前3名易主 / 前10进出 /
龙头易主 / 头部均分漂移≥0.5），避免 60s 轮询下 0.1 级均分漂移刷屏。
核心纯函数 `beaconOrd` / `beaconChangeSummary`，node 跑 app.js 真函数。

口径（钉住）：
  · prev/next 任一侧为空 ⇒ null（首次加载/切日不报）
  · 无实质变化 ⇒ null
  · 均分漂移阈值 0.5（只报前10、且板块名顺序未变）
  · 结果截断到最多 3 条（`；` 分隔）
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
    i = _JS.index("function beaconOrd")
    j = _JS.index("function renderMarketBeacon", i)
    return _JS[i:j]


def _run(*ds):
    """ds = (d1, d2) 两个风向标 payload；返回 beaconChangeSummary(beaconOrd(d1), beaconOrd(d2))。"""
    code = (_extract()
            + "\nconsole.log(JSON.stringify(beaconChangeSummary(beaconOrd("
            + json.dumps(ds[0], ensure_ascii=False) + "), beaconOrd("
            + json.dumps(ds[1], ensure_ascii=False) + "))));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _S(name, avg=10.0, limitup=3, leads=("龙头A",)):
    # beaconOrd 从 `rows` 里筛 tier=="龙头" 取名单（与后端 payload 同形），不走独立的 leads 字段
    rows = [{"tier": "龙头", "name": x} for x in leads]
    return {"name": name, "avg": avg, "limitup": limitup, "rows": rows}


def _D(*sectors):
    return {"sectors": list(sectors)}


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestBeaconChangeSummary(unittest.TestCase):
    def test_首次加载为null(self):
        self.assertIsNone(_run(_D(), _D(_S("A"))))          # 空 prev
        self.assertIsNone(_run(_D(_S("A")), _D()))          # 空 next

    def test_无变化为null(self):
        d = _D(_S("A"), _S("B"), _S("C"))
        self.assertIsNone(_run(d, d))

    def test_均分小漂移不算变化(self):
        self.assertIsNone(_run(_D(_S("A", avg=10.0)), _D(_S("A", avg=10.3))))

    def test_前3易主(self):
        out = _run(_D(_S("A"), _S("B"), _S("C")), _D(_S("B"), _S("A"), _S("C")))
        self.assertIn("第1名：A → B", out)

    def test_前10进出(self):
        prev = _D(*[_S(f"S{i}") for i in range(10)])
        nxt = _D(*([_S(f"S{i}") for i in range(9)] + [_S("NEW")]))
        out = _run(prev, nxt)
        self.assertIn("进前10：NEW", out)
        self.assertIn("出前10：S9", out)

    def test_龙头易主(self):
        out = _run(_D(_S("A", leads=("X",))), _D(_S("A", leads=("Y",))))
        self.assertIn("「A」龙头：X → Y", out)

    def test_龙头从无到有(self):
        out = _run(_D(_S("A", leads=())), _D(_S("A", leads=("X",))))
        self.assertIn("「A」龙头：无 → X", out)

    def test_均分漂移过阈值(self):
        out = _run(_D(_S("A", avg=10.0)), _D(_S("A", avg=10.6)))
        self.assertIn("「A」均分", out)

    def test_上限截断3条(self):
        prev = _D(*[_S(f"S{i}", avg=1.0) for i in range(10)])
        nxt = _D(*[_S(f"S{i}", avg=5.0, leads=("Z",)) for i in range(10)])
        out = _run(prev, nxt)
        self.assertIsNotNone(out)
        self.assertLessEqual(len(out.split("；")), 3)


class TestMarketBeaconChangeWiring(unittest.TestCase):
    def test_纯函数存在(self):
        for fn in ("function beaconOrd(d)", "function beaconChangeSummary(prev, next)"):
            self.assertIn(fn, _JS)

    def test_变化toast接线(self):
        self.assertIn('toast("🧭 风向标：" + _change, "beacon-change")', _JS)

    def test_上一轮板块序状态存在(self):
        self.assertIn("let _beaconLastOrd = null, _beaconLastOrdDate = null;", _JS)

    def test_切日不报(self):
        self.assertIn("if (_beaconLastOrdDate !== _dd) { _beaconLastOrdDate = _dd; _beaconLastOrd = null; }", _JS)

    def test_头部刷新时间戳(self):
        self.assertIn("刷新 <b>${esc(_nowTxt)}</b>", _JS)

    def test_头部变化摘要(self):
        self.assertIn("变化：${esc(_change)}", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
