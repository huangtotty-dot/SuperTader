# -*- coding: utf-8 -*-
"""站上20线公司占比面板（2026-10-20 owner：「在年限宽度下方加上 站上20线公司占比，
周期和年限宽度保持一致」）。

刻意**不做**第二次全池扫描：MA20 宽度与年限宽度在同一次解析里顺手算，所以契约上必须满足
  · `_ma365_payload` 同时给出 `series20` / `last20`；旧缓存（无 series20）要能被识别
    （`ma20_warming=True`），让前端显示"计算中"而不是空白；
  · 历史档（自2024-09-01）的 series20 从**本地**计算结果按日期收边（MA20 只需 20 根回看，
    不用像 MA365 那样 GM 补算），右端按 365 档的 latest_date 截齐 ⇒ 两张图横轴一致；
  · 前端两张图共用一次取数与同一套骨架 `_breadthChartOption`。
本测试只做源码/纯函数契约，不启动 GUI、不扫缓存。

运行：python -m unittest tests.phase3.test_ma20_breadth_panel -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import t_gui  # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


class TestPayloadContract(unittest.TestCase):
    def test_payload_carries_series20(self):
        rec = {"latest_date": "2026-10-20", "computed_at": "x",
               "series": [{"date": "2026-10-18", "above": 1, "total": 2, "pct": 50.0},
                          {"date": "2026-10-20", "above": 1, "total": 2, "pct": 50.0}],
               "series20": [{"date": "2026-10-18", "above": 3, "total": 4, "pct": 75.0},
                            {"date": "2026-10-20", "above": 1, "total": 4, "pct": 25.0}],
               "coverage": {"files": 10, "cohort": 2}}
        p = t_gui.Api._ma365_payload(rec, 120)
        self.assertEqual([x["pct"] for x in p["series20"]], [75.0, 25.0])
        self.assertEqual(p["last20"]["pct"], 25.0)
        self.assertFalse(p["ma20_warming"])

    def test_legacy_cache_flags_ma20_warming(self):
        """2026-10-20 之前的磁盘缓存没有 series20 ⇒ 不能静默空白，必须给 warming 标记。"""
        rec = {"latest_date": "2026-10-20", "series":
               [{"date": "2026-10-20", "above": 1, "total": 2, "pct": 50.0}],
               "coverage": {"files": 10, "cohort": 2}}
        p = t_gui.Api._ma365_payload(rec, 120, stale=True)
        self.assertEqual(p["series20"], [])
        self.assertTrue(p["ma20_warming"])

    def test_history_slice_is_bounded_on_both_ends(self):
        rows = [{"date": d, "above": 1, "total": 2, "pct": 50.0}
                for d in ("2024-08-30", "2024-09-02", "2026-10-16", "2026-10-20")]
        old = t_gui._MA365_MEM.get("val")
        t_gui._MA365_MEM["val"] = {"series20": rows}
        try:
            got = t_gui.Api._ma20_hist_series("2024-09-01", "2026-10-16")
        finally:
            t_gui._MA365_MEM["val"] = old
        self.assertEqual([x["date"] for x in got], ["2024-09-02", "2026-10-16"],
                         "历史档要按 start 起、并按 365 档 latest_date 截尾（两图横轴对齐）")

    def test_ma20_window_covers_hist_range(self):
        # 「自2024-09-01」约 510 个交易日 ⇒ 窗口必须比它长，否则历史档会缺前半段
        self.assertGreaterEqual(t_gui._MA20_MAX_DAYS, 520)


class TestFrontendWiring(unittest.TestCase):
    def test_section_sits_right_below_ma365(self):
        i365 = _HTML.index('id="ma365-breadth"')
        i20 = _HTML.index('id="ma20-breadth"')
        self.assertLess(i365, i20, "站上20线占比必须在年限宽度**下方**")
        self.assertIn('id="ma20BreadthChart"', _HTML)
        self.assertIn('id="ma20BreadthMeta"', _HTML)

    def test_one_fetch_renders_both(self):
        # 禁止为第二张图再拉一次（那会把 5500 个日线缓存的解析做两遍）
        self.assertEqual(_JS.count('apiCall("load_ma365_breadth"'), 2,
                         "只允许 recent / hist 两个分支各一次取数")
        self.assertIn("renderMa20Breadth(d);", _JS)

    def test_shares_chart_skeleton(self):
        self.assertIn("function _breadthChartOption(", _JS)
        self.assertIn('ma365Inst.setOption(_breadthChartOption(series, "站上年限"', _JS)
        self.assertIn('ma20Inst.setOption(_breadthChartOption(series, "站上20日线"', _JS)

    def test_range_toggle_drives_both(self):
        # 区间只有一处按钮（年限宽度那里），第二张图跟随它 —— 不许另起一套 range 状态
        self.assertEqual(_JS.count('id="ma365Range'), 0)   # 按钮在 index.html，不在 JS
        self.assertIn('const meta20 = document.getElementById("ma20BreadthMeta");', _JS)


if __name__ == "__main__":
    unittest.main()
