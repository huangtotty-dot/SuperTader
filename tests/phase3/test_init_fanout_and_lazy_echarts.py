# -*- coding: utf-8 -*-
"""P2-5（2026-11-22）：首屏按需加载 echarts + init 扇出收敛 的契约与行为验证。

① echarts.min.js 不再同步阻塞首屏：index.html 无同步 <script>，app.js 有
   `_ensureEcharts()`（动态注入/单飞/排队补画），且**所有**裸 `echarts.init` /
   `echarts.graphic` 使用点都已接入（所在函数体内必出现 `_ensureEcharts`）。
② loadFocusCodes / loadRedMarks / loadWeeklyMacdLastStatus 三个启动调用并入
   `load_day` 响应：gui.py 两条返回路径都带三键（行为验证走缺 daily_review 的
   部分载荷路径），前端在 loadAndRender 统一应用、键缺失回退原端点。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_PY = (_ROOT / "gui.py").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")

sys.path.insert(0, str(_ROOT))
import gui  # noqa: E402


def _fn_bodies(src, marker):
    """返回 `marker`（如 `echarts.init(`）每处出现所在的函数体文本列表。

    以「列 0 的 function/async function 声明」为函数边界（本文件渲染函数均为顶层声明）。
    """
    lines = src.split("\n")
    starts = [i for i, l in enumerate(lines)
              if l.startswith("function ") or l.startswith("async function ")]
    starts.append(len(lines))
    bodies = []
    for i, l in enumerate(lines):
        if marker not in l:
            continue
        for j in range(len(starts) - 1):
            if starts[j] <= i < starts[j + 1]:
                bodies.append("\n".join(lines[starts[j]:starts[j + 1]]))
                break
    return bodies


class TestLazyEcharts(unittest.TestCase):
    def test_index_html_no_sync_echarts(self):
        self.assertNotIn('<script src="vendor/echarts.min.js">', _HTML,
                         "echarts 不得再同步阻塞首屏（应由 _ensureEcharts 动态注入）")

    def test_ensure_echarts_single_flight(self):
        self.assertIn("function _ensureEcharts()", _JS)
        self.assertIn("_echartsLoading", _JS)                     # 单飞句柄
        self.assertIn('s.src = "vendor/echarts.min.js";', _JS)    # 动态注入
        self.assertIn("let echarts = window.echarts || null;", _JS)

    def test_no_bare_echarts_without_ensure(self):
        """每个含 `echarts.init(` / `echarts.graphic.` 的函数体都必须接 `_ensureEcharts`。"""
        for marker in ("echarts.init(", "echarts.graphic."):
            bodies = _fn_bodies(_JS, marker)
            self.assertTrue(bodies, f"{marker} 使用点凭空消失？（应至少还有 K线/宽度图）")
            for b in bodies:
                head = b.split("\n", 1)[0]
                self.assertIn("_ensureEcharts", b,
                              f"{marker} 所在函数未接 _ensureEcharts：{head[:80]}")

    def test_renderers_queue_instead_of_drop(self):
        """未就绪排队补画的关键钉：echRender 自调度重入。"""
        self.assertIn("_ensureEcharts().then(() => echRender(id, opt))", _JS)
        self.assertIn("_ensureEcharts().then(() => renderStockChart(applyZoom))", _JS)
        self.assertIn("await _ensureEcharts()", _JS)              # async 渲染点直接 await


class TestInitFanout(unittest.TestCase):
    def test_load_day_carries_three_keys_both_paths(self):
        # 两条返回路径（部分载荷/全量载荷）都必须并入
        self.assertEqual(_PY.count("out.update(self._init_marks_payload())"), 2)
        self.assertIn('"focus_codes"', _PY)
        self.assertIn('"red_marks"', _PY)
        self.assertIn('"weekly_macd_status"', _PY)

    def test_frontend_reads_from_load_day(self):
        self.assertIn("loadFocusCodes(payload.focus_codes);", _JS)
        self.assertIn("loadRedMarks(payload.red_marks);", _JS)
        self.assertIn("loadWeeklyMacdLastStatus(payload.weekly_macd_status);", _JS)

    def test_fanout_calls_collapsed(self):
        """三个独立启动调用各只剩 1 处（函数内回退分支）；weekly_macd_status 另有轮巡轮询 1 处。"""
        self.assertEqual(_JS.count('apiCall("load_focus_codes")'), 1)
        self.assertEqual(_JS.count('apiCall("load_red_marks")'), 1)
        self.assertEqual(_JS.count('apiCall("weekly_macd_status")'), 2)

    def test_init_no_longer_calls_trio(self):
        i = _JS.index("async function init() {")
        seg = _JS[i:i + 3000]
        self.assertNotIn("loadFocusCodes();", seg)
        self.assertNotIn("loadRedMarks();", seg)
        self.assertNotIn("loadWeeklyMacdLastStatus();", seg)

    def test_load_day_partial_path_behavior(self):
        """行为验证（缺 daily_review 的部分载荷路径）：三键真实出现且形状与端点一致。"""
        api = gui.Api()
        with tempfile.TemporaryDirectory() as td:
            old_out = gui.OUT
            gui.OUT = Path(td)                     # 空目录 ⇒ 走部分载荷分支
            try:
                with mock.patch.object(api, "_load_positions", return_value={"current": {}}), \
                     mock.patch.object(api, "_load_stage_board", return_value={}), \
                     mock.patch.object(api, "load_portfolio_config", return_value={}), \
                     mock.patch.object(api, "state_revision", return_value={}), \
                     mock.patch.object(api, "load_focus_codes",
                                       return_value={"codes": ["600519"], "items": {"600519": {"src": "manual"}}}), \
                     mock.patch.object(api, "load_red_marks",
                                       return_value={"codes": ["000001"], "updated_at": "x"}), \
                     mock.patch.object(api, "weekly_macd_status",
                                       return_value={"running": False, "last": {"finished_at": "t"}}):
                    payload = api.load_day("2026-11-20")      # 周五 ⇒ _wd<5 部分路径
            finally:
                gui.OUT = old_out
        self.assertEqual(payload["focus_codes"]["codes"], ["600519"])
        self.assertEqual(payload["red_marks"]["codes"], ["000001"])
        self.assertEqual(payload["weekly_macd_status"]["last"]["finished_at"], "t")

    def test_marks_payload_failure_isolated(self):
        """单键异常不炸 load_day：坏键静默缺席，其余照常。"""
        api = gui.Api()
        with mock.patch.object(api, "load_focus_codes", side_effect=RuntimeError("x")), \
             mock.patch.object(api, "load_red_marks", return_value={"codes": []}), \
             mock.patch.object(api, "weekly_macd_status", return_value={"last": {}}):
            d = api._init_marks_payload()
        self.assertNotIn("focus_codes", d)
        self.assertIn("red_marks", d) and self.assertIn("weekly_macd_status", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
