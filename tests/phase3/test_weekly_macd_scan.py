# -*- coding: utf-8 -*-
"""周线 MACD 柱子翻红 → 自动标紫（全池轮巡，2026-10-31 owner）。

守四条：
  ① 判据：**上周柱子 ≤0 < 本周**（翻红）才算；一直绿 / 早已红都不算；不足 35 周 → None；
  ② 周线取数：日线 → 按周（W-FRI）取**当周最后一个收盘**；
  ③ 轮巡接口：后台线程跑、只加不删、已在关注里的不重复计数、状态可轮询；
  ④ 前端：顶栏有总按钮，轮询完成后自动重拉关注表（星标变紫）。

运行：python -m pytest -q tests/phase3/test_weekly_macd_scan.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
HTML = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")

_DECLINE = [10 - 0.05 * i for i in range(60)]          # 60 周缓慢下跌
_BOUNCE = _DECLINE + [round(_DECLINE[-1] * 1.001, 4)]  # 最后一周微反弹 ⇒ 柱子翻红


class TestMacdCriterion(unittest.TestCase):
    def setUp(self):
        import t_gui
        self.g = t_gui

    def test_一直绿不算(self):
        self.assertFalse(self.g.weekly_macd_turned_red(_DECLINE))

    def test_最后一周翻红才算(self):
        self.assertTrue(self.g.weekly_macd_turned_red(_BOUNCE))

    def test_早已红不算(self):
        self.assertFalse(self.g.weekly_macd_turned_red(_DECLINE + [11, 12.5, 14.5]))

    def test_数据不足返回None(self):
        self.assertIsNone(self.g.weekly_macd_turned_red([1, 2, 3]))

    def test_周线取当周最后收盘(self):
        rows = []
        closes = [10, 10.1, 10.2, 10.3, 10.4,      # 第 1 周（周一~周五）
                  10.5, 10.6, 10.7, 10.8, 10.9]     # 第 2 周
        dates = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
                 "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
        for d, c in zip(dates, closes):
            rows.append({"date": d, "close": c})
        w = self.g._weekly_closes_from_rows(rows)
        self.assertEqual(w, [10.4, 10.9], "每周应取周五那根收盘")


class TestScanApi(unittest.TestCase):
    def setUp(self):
        import t_gui
        self.tg = t_gui
        self.td = tempfile.TemporaryDirectory()
        self._orig_fp = t_gui._FOCUS_FP
        self._orig_wm = t_gui._WM_STATE_FP
        t_gui._FOCUS_FP = Path(self.td.name) / "focus_stocks.json"
        t_gui._WM_STATE_FP = Path(self.td.name) / "weekly_macd_scan.json"
        t_gui._WM_LAST = {}
        with t_gui._WM_LOCK:
            t_gui._WM_SCAN.update({"running": False, "done": 0, "total": 0,
                                   "hits": [], "matched": [], "error": "",
                                   "started_at": None, "finished_at": None})
        self.api = t_gui.Api()

    def tearDown(self):
        self.tg._FOCUS_FP = self._orig_fp
        self.tg._WM_STATE_FP = self._orig_wm
        with self.tg._WM_LOCK:
            self.tg._WM_SCAN.update({"running": False})

    def _wait(self, timeout=15.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.api.weekly_macd_status()
            if not st.get("running"):
                return st
            time.sleep(0.05)
        raise AssertionError("轮巡超时未结束")

    def test_命中写紫且只加不删(self):
        # 先把 600362 手工标记，验证轮巡不覆盖、不取消
        self.api.toggle_focus("600362")
        # ⚠️ 轮询必须在 patch 上下文**之内**：worker 在后台线程里查全局名，
        # with 一退出 mock 就还原，线程会用真函数算（踩过一次）。
        with mock.patch.object(self.tg, "weekly_macd_turned_red",
                               side_effect=lambda closes: True):
            r = self.api.start_weekly_macd_scan(codes=["301079", "600362"])
            self.assertTrue(r.get("started"))
            st = self._wait()
        self.assertFalse(st.get("error"))
        self.assertEqual(st.get("hits"), ["301079"], "已在关注里的票不重复计数")
        codes = self.api.load_focus_codes()["codes"]
        self.assertIn("301079", codes)
        self.assertIn("600362", codes, "人工标记必须保留")
        saved = json.loads(self.tg._FOCUS_FP.read_text(encoding="utf-8"))
        self.assertEqual(saved["codes"]["301079"].get("src"), "weekly_macd")

    def test_轮巡结果落盘_重启后可回读(self):
        with mock.patch.object(self.tg, "weekly_macd_turned_red",
                               side_effect=lambda closes: True):
            self.api.start_weekly_macd_scan(codes=["301079"])
            st = self._wait()
        fp = self.tg._WM_STATE_FP
        self.assertTrue(fp.exists(), "上次轮巡摘要必须落盘（GUI 重启后还能看到）")
        saved = json.loads(fp.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("hits"), ["301079"])
        self.assertTrue(saved.get("finished_at"))
        # 模拟重启：清空内存后从磁盘回读
        self.tg._WM_SCAN.update({"hits": [], "matched": [], "finished_at": None})
        self.assertEqual(self.tg._wm_load_state().get("hits"), ["301079"])
        self.assertEqual(st.get("last", {}).get("hits"), ["301079"], "status 应带上次摘要")

    def test_无命中不改文件(self):
        with mock.patch.object(self.tg, "weekly_macd_turned_red",
                               side_effect=lambda closes: False):
            self.api.start_weekly_macd_scan(codes=["301079"])
            st = self._wait()
        self.assertEqual(st.get("hits"), [])
        self.assertEqual(self.api.load_focus_codes()["codes"], [])


class TestFrontend(unittest.TestCase):
    def test_顶栏总按钮(self):
        self.assertIn('id="wmScanBtn"', HTML)
        self.assertIn("runWeeklyMacdScan()", HTML)

    def test_轮询与完成刷新(self):
        self.assertIn("async function runWeeklyMacdScan()", JS)
        self.assertIn('apiCall("start_weekly_macd_scan")', JS)
        self.assertIn('apiCall("weekly_macd_status")', JS)
        i = JS.index("async function runWeeklyMacdScan()")
        seg = JS[i:i + 2600]
        self.assertIn("await loadFocusCodes()", seg, "完成后必须重拉关注表（星标变紫）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
