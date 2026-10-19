# -*- coding: utf-8 -*-
"""K 线打开提速的源码契约（2026-10-20）。

根因：① 背离冷导入 1.5s（已移到启动预热 + schema 短路）；② 三个分钟档串行取
3×~1.6s=4.8s（已并行）；③ 需要能区分后端慢与前端渲染慢（新增 chart_render /
chart_open_total 到慢日志）。
只读源码断言，不启动 GUI。
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_PY = (_ROOT / "t_gui.py").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


class TestChartOpenPerf(unittest.TestCase):
    def test_minute_fetches_are_parallel(self):
        self.assertIn("thread_name_prefix=\"minfetch\"", _PY)
        self.assertIn("_ex.submit(_fetch_min_bars", _PY)
        self.assertIn("max_workers=3", _PY)

    def test_divergence_schema_short_circuit(self):
        self.assertIn("divergence_schema", _PY)
        self.assertIn("_DIV_SCHEMA", _PY)

    def test_frontend_reports_render_and_total(self):
        self.assertIn('report_slow_call("chart_render"', _JS)
        self.assertIn('report_slow_call("chart_open_total"', _JS)


if __name__ == "__main__":
    unittest.main()
