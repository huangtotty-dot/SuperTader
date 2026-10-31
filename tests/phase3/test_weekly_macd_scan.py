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


class TestMacdStateModes(unittest.TestCase):
    """2026-10-31 owner 报「赛福天周线 MACD 已是红柱却没被扫出来」：
    原判据只认"本周刚翻红"，而它 8 周前就翻红了 ⇒ 增加**状态口径**。"""

    def setUp(self):
        import t_gui
        self.g = t_gui
        self.flip_now = _BOUNCE                                   # 最后一根刚翻红
        self.flip_3w = _BOUNCE + [_BOUNCE[-1]] * 3                 # 3 周前翻红、之后一路红柱
        self.always_red = _DECLINE[:40] + [8.0 + 0.6 * i for i in range(1, 12)]  # 早已红、近期无翻转

    def test_状态与跃迁分开(self):
        st = self.g.weekly_macd_state(self.flip_now)
        self.assertTrue(st["red"] and st["flip"])
        self.assertEqual(st["flip_bars_ago"], 0)
        st3 = self.g.weekly_macd_state(self.flip_3w)
        self.assertTrue(st3["red"])
        self.assertFalse(st3["flip"])
        self.assertEqual(st3["flip_bars_ago"], 3, "三周前翻红 ⇒ 距今 3 根")

    def test_红柱口径能扫到早已翻红的票(self):
        self.assertTrue(self.g.weekly_macd_match(self.flip_3w, "red"))
        self.assertFalse(self.g.weekly_macd_match(self.flip_3w, "flip"),
                         "flip 口径只认本周刚翻红")

    def test_近N周口径(self):
        self.assertTrue(self.g.weekly_macd_match(self.flip_3w, "recent", recent_weeks=4))
        late = _BOUNCE + [_BOUNCE[-1]] * 5
        self.assertFalse(self.g.weekly_macd_match(late, "recent", recent_weeks=4),
                         "5 周前翻红 ⇒ 超出近4周")
        self.assertTrue(self.g.weekly_macd_match(late, "red"), "但仍算当前红柱")

    def test_近4周与近8周口径(self):
        """owner 2026-10-31：本周 / 4 周内 / 8 周内都要显示 —— 用 recent8 一次覆盖，
        深浅再按周数区分。"""
        w3 = _BOUNCE + [_BOUNCE[-1]] * 3          # 3 周前翻红
        w5 = _BOUNCE + [_BOUNCE[-1]] * 5          # 5 周前翻红
        w9 = _BOUNCE + [_BOUNCE[-1]] * 9          # 9 周前翻红
        for w, tag in ((w3, "3周"), (w5, "5周"), (w9, "9周")):
            st = self.g.weekly_macd_state(w)
            self.assertTrue(st["red"], tag)
            self.assertFalse(st["flip"], tag)
        self.assertTrue(self.g.weekly_macd_match_state(self.g.weekly_macd_state(w3), "recent"))
        self.assertFalse(self.g.weekly_macd_match_state(self.g.weekly_macd_state(w5), "recent"))
        self.assertTrue(self.g.weekly_macd_match_state(self.g.weekly_macd_state(w5), "recent8"))
        self.assertFalse(self.g.weekly_macd_match_state(self.g.weekly_macd_state(w9), "recent8"))
        self.assertTrue(self.g.weekly_macd_match_state(self.g.weekly_macd_state(w9), "red"),
                        "9 周前翻红：不算近8周，但仍是红柱")

    def test_恒红不算本周翻红(self):
        st = self.g.weekly_macd_state(self.always_red)
        self.assertTrue(st["red"])
        self.assertFalse(st["flip"])


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
        with mock.patch.object(self.tg, "weekly_macd_match_state",
                               side_effect=lambda st, mode="red", recent_weeks=4: True):
            r = self.api.start_weekly_macd_scan(codes=["301079", "600362"])
            self.assertTrue(r.get("started"))
            st = self._wait()
        self.assertFalse(st.get("error"))
        self.assertEqual(st.get("hits"), ["301079"], "已在关注里的票不重复计数")
        codes = self.api.load_focus_codes()["codes"]
        self.assertIn("301079", codes)
        self.assertIn("600362", codes, "人工标记必须保留")
        saved = json.loads(self.tg._FOCUS_FP.read_text(encoding="utf-8"))
        self.assertTrue(str(saved["codes"]["301079"].get("src", "")).startswith("weekly_macd"),
                        "自动标记要带来源（weekly_macd:red/flip/recent 便于区分/清理）")

    def test_轮巡结果落盘_重启后可回读(self):
        with mock.patch.object(self.tg, "weekly_macd_match_state",
                               side_effect=lambda st, mode="red", recent_weeks=4: True):
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

    def test_记录翻红周数并刷新旧标记(self):
        """2026-10-31：颜色深浅要按"翻红多久"，所以命中时必须把 ago 写进关注表；
        已在自动标记里的票**只刷新 ago**（不算新增、不动手工标记）。"""
        # 603028：真实数据里是 8 周前翻红
        with mock.patch.object(self.tg, "weekly_macd_match_state",
                               side_effect=lambda st, mode="red", recent_weeks=4: True):
            self.api.start_weekly_macd_scan(codes=["603028"])
            st = self._wait()
        self.assertEqual(st.get("hits"), ["603028"])
        saved = json.loads(self.tg._FOCUS_FP.read_text(encoding="utf-8"))
        ent = saved["codes"]["603028"]
        self.assertEqual(ent.get("ago"), 8, "翻红 8 周 ⇒ ago=8（前端据此加深紫色）")
        self.assertTrue(str(ent.get("src")).startswith("weekly_macd:"))

        # 再跑一次：不算新增，但 ago 仍然刷新
        with mock.patch.object(self.tg, "weekly_macd_match_state",
                               side_effect=lambda st, mode="red", recent_weeks=4: True):
            self.api.start_weekly_macd_scan(codes=["603028"])
            st2 = self._wait()
        self.assertEqual(st2.get("hits"), [], "已标记的不重复计入新增")
        saved = json.loads(self.tg._FOCUS_FP.read_text(encoding="utf-8"))
        self.assertEqual(saved["codes"]["603028"].get("ago"), 8)

    def test_不动手工标记(self):
        self.api.toggle_focus("600362")               # 手工
        with mock.patch.object(self.tg, "weekly_macd_match_state",
                               side_effect=lambda st, mode="red", recent_weeks=4: True):
            self.api.start_weekly_macd_scan(codes=["600362", "603028"])
            self._wait()
        saved = json.loads(self.tg._FOCUS_FP.read_text(encoding="utf-8"))
        self.assertNotIn("ago", saved["codes"]["600362"], "手工标记不带 ago（恒为基准紫）")
        self.assertEqual(saved["codes"]["600362"].get("src", "manual"), "manual")

    def test_无命中不改文件(self):
        with mock.patch.object(self.tg, "weekly_macd_match_state",
                               side_effect=lambda st, mode="red", recent_weeks=4: False):
            self.api.start_weekly_macd_scan(codes=["301079"])
            st = self._wait()
        self.assertEqual(st.get("hits"), [])
        self.assertEqual(self.api.load_focus_codes()["codes"], [])


class TestBeaconWm(unittest.TestCase):
    """2026-10-31 owner：「市场风向也要显示周线 MACD 是否翻红」。"""

    def test_按代码取状态且带记忆(self):
        import t_gui
        st = t_gui._wm_state_of_code("603028")
        self.assertIsInstance(st, dict)
        self.assertTrue(st["red"])
        self.assertFalse(st["flip"])
        self.assertEqual(st["flip_bars_ago"], 8)
        st2 = t_gui._wm_state_of_code("603028")
        self.assertEqual(st, st2)
        self.assertIsNone(t_gui._wm_state_of_code("999999"))

    def test_enrich里带wm(self):
        src = (Path(_ROOT) / "t_gui.py").read_text(encoding="utf-8")
        i = src.index("def _beacon_enrich(")
        seg = src[i:i + 2500]
        self.assertIn('"wm": _wm_state_of_code(c)', seg)


class TestFrontend(unittest.TestCase):
    def test_顶栏总按钮(self):
        self.assertIn('id="wmScanBtn"', HTML)
        self.assertIn("runWeeklyMacdScan()", HTML)

    def test_四档色带强对比且带周数(self):
        self.assertIn("function focusBand(", JS)
        i = JS.index("function focusBand(")
        seg = JS[i:i + 600]
        for c in ("#a78bfa", "#8b5cf6", "#6d28d9", "#3b0f78"):
            self.assertIn(c, seg, f"四档颜色缺 {c}")
        self.assertIn("focus-ago", JS, "色块上要显示翻红周数（数字比色差更可靠）")
        self.assertIn('class="focus-ago"', JS)
        # 默认口径 = 近8周（覆盖 本周/4周内/8周内）
        self.assertIn('<option value="recent8" selected>', HTML)
        self.assertIn("近8周翻红", HTML)

    def test_颜色按翻红周数分档(self):
        """连续渐变区分度不够（owner 2026-10-31）⇒ 改四档强对比，仍按周数取色。"""
        self.assertIn("function focusColor(", JS)
        i = JS.index("function focusColor(")
        seg = JS[i:i + 500]
        self.assertIn("m.ago", seg)
        self.assertIn("focusBand(m.ago)", seg, "focusColor 必须走四档色带")
        self.assertIn('return "#8250df"', seg, "手动标记（无 ago）用基准紫")
        self.assertIn("--focus-c", JS, "行首紫条也要跟着档位走")

    def test_风向标显示周线MACD标记(self):
        self.assertIn("const _wm = s.wm || null;", JS)
        self.assertIn("周红", JS)
        self.assertIn("周绿", JS)
        self.assertIn("focusColorByAgo(a)", JS, "红柱颜色沿用关注的四档色带")
        css = (Path(_ROOT) / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".wm-chip", css)

    def test_轮询与完成刷新(self):
        self.assertIn("async function runWeeklyMacdScan()", JS)
        self.assertIn('apiCall("start_weekly_macd_scan", null, mode)', JS)
        self.assertIn('apiCall("weekly_macd_status")', JS)
        i = JS.index("async function runWeeklyMacdScan()")
        seg = JS[i:i + 2600]
        self.assertIn("await loadFocusCodes()", seg, "完成后必须重拉关注表（星标变紫）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
