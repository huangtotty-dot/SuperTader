# -*- coding: utf-8 -*-
"""筛选预设（2026-11-14，TradingView「Screener 保存筛选」落地）。

把「建仓信号扫描」的筛选状态（池分管/扫描日期/代码·名称搜索）存成命名预设、一键切回。
两个纯函数，node 跑 app.js 真函数：
  · buildScreenerPreset(name, state, savedAt)：归一化一条预设
  · mergeScreenerPresets(list, preset, cap)：同名覆盖、按 savedAt 新→旧、上限截断

口径（钉住）：
  · name 必填（trim 后非空），否则 null
  · pool 只收 all/manual/auto，其它一律归一成 all
  · date 取前 10 字符（YYYY-MM-DD）；code/nameQ trim 后空则剔除
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
    i = _JS.index("function buildScreenerPreset")
    j = _JS.index("function _screenerRead", i)
    return _JS[i:j]


def _run(expr, *args):
    code = (_extract()
            + f"\nconsole.log(JSON.stringify({expr}({', '.join(json.dumps(a, ensure_ascii=False) for a in args)})));")
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestBuildScreenerPreset(unittest.TestCase):
    def test_全字段(self):
        out = _run("buildScreenerPreset", "池A",
                   {"pool": "manual", "date": "2026-11-14", "code": "600519", "nameQ": "茅台"},
                   1700000000000)
        self.assertEqual(out, {"name": "池A", "pool": "manual", "savedAt": 1700000000000,
                               "date": "2026-11-14", "code": "600519", "nameQ": "茅台"})

    def test_name必填(self):
        self.assertIsNone(_run("buildScreenerPreset", None, {}, 1))
        self.assertIsNone(_run("buildScreenerPreset", "", {}, 1))
        self.assertIsNone(_run("buildScreenerPreset", "   ", {}, 1))

    def test_name_trim(self):
        out = _run("buildScreenerPreset", "  池A  ", {}, 1)
        self.assertEqual(out["name"], "池A")

    def test_pool_只收三态(self):
        self.assertEqual(_run("buildScreenerPreset", "a", {"pool": "bogus"}, 1)["pool"], "all")
        self.assertEqual(_run("buildScreenerPreset", "a", {"pool": "auto"}, 1)["pool"], "auto")
        self.assertEqual(_run("buildScreenerPreset", "a", {}, 1)["pool"], "all")

    def test_date_截前10位(self):
        out = _run("buildScreenerPreset", "a", {"date": "2026-11-14T09:30:00"}, 1)
        self.assertEqual(out["date"], "2026-11-14")

    def test_空可选字段剔除(self):
        out = _run("buildScreenerPreset", "a", {"code": "  ", "nameQ": ""}, 1)
        self.assertNotIn("code", out)
        self.assertNotIn("nameQ", out)
        self.assertNotIn("date", out)


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestMergeScreenerPresets(unittest.TestCase):
    def test_追加到空(self):
        p = {"name": "a", "pool": "all", "savedAt": 1}
        self.assertEqual(_run("mergeScreenerPresets", [], p, 20), [p])

    def test_同名覆盖(self):
        a = {"name": "a", "pool": "all", "savedAt": 1}
        b = {"name": "a", "pool": "manual", "savedAt": 2}
        out = _run("mergeScreenerPresets", [a], b, 20)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pool"], "manual")

    def test_按savedAt新到旧(self):
        a = {"name": "a", "savedAt": 1}
        b = {"name": "b", "savedAt": 3}
        c = {"name": "c", "savedAt": 2}
        out = _run("mergeScreenerPresets", [a, b, c], {"name": "d", "savedAt": 5}, 20)
        self.assertEqual([x["name"] for x in out], ["d", "b", "c", "a"])

    def test_上限截断(self):
        arr = [{"name": f"x{i}", "savedAt": i} for i in range(10)]
        out = _run("mergeScreenerPresets", arr, {"name": "new", "savedAt": 99}, 3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["name"], "new")

    def test_null_preset_只截断(self):
        arr = [{"name": f"x{i}", "savedAt": i} for i in range(5)]
        out = _run("mergeScreenerPresets", arr, None, 3)
        self.assertEqual(len(out), 3)

    def test_非数组按空处理(self):
        out = _run("mergeScreenerPresets", None, {"name": "a", "savedAt": 1}, 20)
        self.assertEqual(out, [{"name": "a", "savedAt": 1}])


class TestScreenerPresetWiring(unittest.TestCase):
    def test_控件齐全(self):
        for cid in ("screenerPresetName", "screenerPresetSel"):
            self.assertIn(f'id="{cid}"', _HTML, f"index.html 缺 {cid}")

    def test_按钮接函数(self):
        self.assertIn("saveScreenerPreset()", _HTML)
        self.assertIn("applyScreenerPreset(this.value)", _HTML)
        self.assertIn("deleteScreenerPreset()", _HTML)

    def test_函数存在(self):
        for fn in ("function buildScreenerPreset(name, state, savedAt)",
                   "function mergeScreenerPresets(list, preset, cap)",
                   "function saveScreenerPreset()", "function applyScreenerPreset(name)",
                   "function deleteScreenerPreset()", "function refreshScreenerPresets()"):
            self.assertIn(fn, _JS)

    def test_启动回填下拉(self):
        self.assertIn("refreshScreenerPresets();   // 启动时回填「筛选预设」下拉", _JS)

    def test_应用走_setPoolFilter(self):
        self.assertIn("setPoolFilter(p.pool);", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
