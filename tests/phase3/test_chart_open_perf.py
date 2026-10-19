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

    def test_minute_fetch_is_per_period(self):
        """分钟 Tab 只抓当前周期，不再为一个 Tab 抓三档。"""
        self.assertIn("want_min_period", _PY)
        self.assertIn("_need = period if period in", _PY)
        self.assertIn('apiCall("load_stock_chart", stockChartCode, stockChartVersion,', _JS)
        self.assertIn("_wantMinutes(), stockChartPeriod);", _JS)

    def test_daily_view_strips_minute_series(self):
        """日/周/月视图不把三档分钟序列传给前端（payload 体积 ≈ -2/3）。"""
        self.assertIn("strip_minutes", _PY)
        self.assertIn("strip_minutes=not want_minutes", _PY)

    def test_minute_view_keeps_only_current_period(self):
        """分钟视图只带当前 Tab 的分时序列（payload ≈ -46%）。"""
        self.assertIn("keep_minute", _PY)
        self.assertIn("keep_minute=(period if want_minutes else None)", _PY)

    def test_chart_modal_pauses_background_pollers(self):
        """K 线弹窗打开时暂停 auto/ob/beacon/live 重面板，避免 3~5s 响应堵住 UI 线程。"""
        self.assertIn("let chartModalOpen = false;", _JS)
        self.assertIn("if (chartModalOpen) return;        // K 线弹窗打开", _JS)
        self.assertIn("if (chartModalOpen) {                    // 弹窗打开", _JS)

    def test_auto_pnl_ttl_relaxed(self):
        """auto_pnl/auto_cost 全量重算 3~5s ⇒ TTL 从 30s 提到 120s。"""
        self.assertIn("_AUTO_PNL_TTL = 120.0", _PY)
        self.assertIn("_AUTO_COST_TTL = 120.0", _PY)

    def test_short_daily_cache_cannot_shrink_payload(self):
        """短日线缓存（扫描写 30~40 根）不得让长 payload 被判过期/被覆盖。"""
        self.assertIn("_cache_short", _PY)
        self.assertIn("and not _cache_short", _PY)
        self.assertIn("len(rows) * 2 < _old_n", _PY)
        self.assertIn("禁止用短图覆盖长 payload", _PY)

    def test_frontend_cache_busted_and_boot_marked(self):
        """WebView2 可能缓存旧 app.js（改了跟没改一样）⇒ 带版本号 + 启动标记落日志。"""
        _html = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("app.js?v=20261020c", _html)
        self.assertIn("app_boot_20261020c", _JS)
        self.assertIn("K线加载失败", _JS)


if __name__ == "__main__":
    unittest.main()
