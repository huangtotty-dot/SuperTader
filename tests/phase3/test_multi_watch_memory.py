# -*- coding: utf-8 -*-
"""多窗口「记忆上次打开的股票/布局」（2026-11-04 owner）。

为什么不能只靠 localStorage：多窗口常用**独立原生窗口**，与主窗口可能是不同 origin
（file:// vs http）⇒ localStorage 不通；而且主窗口开窗时会用内存里的 codes 当 seed
传过去，反而覆盖掉看盘窗自己存的那份。改成后端共享状态文件后两个窗口/重启都以它为准。

守三条：
  ① `save_multi_watch` / `load_multi_watch` 往返一致，原子写；
  ② codes 过滤空位、最多 9 个；layout 只认 3x3/3x2/2x2，非法值忽略；
  ③ 前端优先级 = 后端记忆 > 开窗 seed > localStorage；改票/改布局都会回写后端。

运行：python -m unittest tests.phase3.test_multi_watch_memory -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gui  # noqa: E402

JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


class TestBackendMemory(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.api = gui.Api()
        self.api._MW_STATE_FP = Path(self._td.name) / "multi_watch.json"

    def tearDown(self):
        self._td.cleanup()

    def test_往返一致(self):
        r = self.api.save_multi_watch(["600000", "300001"], "2x2")
        self.assertTrue(r["ok"])
        d = self.api.load_multi_watch()
        self.assertEqual(d["codes"], ["600000", "300001"])
        self.assertEqual(d["layout"], "2x2")
        self.assertTrue(d["updated_at"])

    def test_部分更新不互相覆盖(self):
        self.api.save_multi_watch(["600000"], "3x2")
        self.api.save_multi_watch(None, "2x2")          # 只改布局
        d = self.api.load_multi_watch()
        self.assertEqual(d["codes"], ["600000"], "只改布局不能把票冲掉")
        self.assertEqual(d["layout"], "2x2")
        self.api.save_multi_watch(["300001", "002929"], None)   # 只改票
        d = self.api.load_multi_watch()
        self.assertEqual(d["layout"], "2x2", "只改票不能把布局冲掉")
        self.assertEqual(d["codes"], ["300001", "002929"])

    def test_保留空位与上限九只(self):
        self.api.save_multi_watch(["", "600000", "", "300001"] + ["002929"] * 12, None)
        d = self.api.load_multi_watch()
        self.assertEqual(d["codes"][:4], ["", "600000", "", "300001"],
                         "空位=空格子，不能把后面的票往前挤")
        self.assertEqual(len(d["codes"]), 9, "最多记 9 个位置")

    def test_非法布局忽略(self):
        self.api.save_multi_watch(["600000"], "3x3")
        self.api.save_multi_watch(None, "9x9")
        self.assertEqual(self.api.load_multi_watch()["layout"], "3x3")

    def test_文件坏掉也不炸(self):
        self.api._MW_STATE_FP.write_text("{bad json", encoding="utf-8")
        d = self.api.load_multi_watch()
        self.assertEqual((d["codes"], d["layout"]), ([], ""))


class TestFrontendWiring(unittest.TestCase):
    def test_后端优先于seed与localStorage(self):
        assert "async function _mwSyncFromServer()" in JS
        i = JS.index("function _mwLoadCodes()")
        seg = JS[i:i + 900]
        self.assertIn("_mwRemoteCodes && _mwRemoteCodes.some(Boolean)", seg)
        self.assertLess(seg.index("_mwRemoteCodes"), seg.index("window._mwSeedCodes"),
                        "后端记忆要排在 seed 之前（seed 是主窗口的旧内存）")

    def test_改票与改布局都回写(self):
        self.assertIn('apiCall("save_multi_watch", mwCodes, null)', JS)
        self.assertIn('apiCall("save_multi_watch", null, v)', JS)
        self.assertIn("_mwRemoteLayout = v", JS)

    def test_开窗先取后端记忆(self):
        i = JS.index("async function openMultiWatch()")
        seg = JS[i:i + 400]
        self.assertIn("await _mwSyncFromServer()", seg)
        self.assertLess(seg.index("_mwSyncFromServer"), seg.index("_mwLoadCodes"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
