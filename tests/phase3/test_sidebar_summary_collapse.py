# -*- coding: utf-8 -*-
"""侧栏盈亏汇总默认收起（2026-11-14，owner：红框部分默认隐藏）。

手动盘 + 自动盘的盈利汇总默认收起，只留「盈亏汇总」标题，点标题展开/收起。
核心纯函数 `sumDefaultCollapsed(stored)`：把 localStorage 里存的折叠态映射成「是否默认收起」。

口径（钉住）：
  · 没存过（null/空串/非 "0"）⇒ 默认收起（true）
  · 存过 "0" ⇒ 用户已展开（false）
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
    i = _JS.index("function sumDefaultCollapsed")
    j = _JS.index("function _sumApply", i)
    return _JS[i:j]


def _run(expr, arg):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify({expr}({json.dumps(arg)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestSumDefaultCollapsed(unittest.TestCase):
    def test_没存过默认收起(self):
        self.assertTrue(_run("sumDefaultCollapsed", None))
        self.assertTrue(_run("sumDefaultCollapsed", ""))

    def test_存过0为展开(self):
        self.assertFalse(_run("sumDefaultCollapsed", "0"))

    def test_存过1为收起(self):
        self.assertTrue(_run("sumDefaultCollapsed", "1"))


class TestSidebarSummaryWiring(unittest.TestCase):
    def test_标题接_toggleSidebarSummary(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="sumToggle"' in ln:
                found = True
                self.assertIn("toggleSidebarSummary()", ln)
        self.assertTrue(found, "index.html 缺 id=sumToggle 折叠标题")

    def test_汇总行默认隐藏(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="sumRows"' in ln:
                found = True
                self.assertIn("display:none", ln, "sumRows 默认应 display:none")
        self.assertTrue(found, "index.html 缺 id=sumRows 汇总行容器")

    def test_折叠箭头存在(self):
        self.assertIn('id="sumToggleArrow"', _HTML)

    def test_函数存在(self):
        for fn in ("function sumDefaultCollapsed(stored)", "function initSidebarSummary()",
                   "function toggleSidebarSummary()"):
            self.assertIn(fn, _JS)

    def test_启动时按存储态初始化(self):
        self.assertIn("initSidebarSummary();       // 侧栏盈亏汇总默认收起", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
