# -*- coding: utf-8 -*-
"""选股猎手「板块站上5/20日线占比」多坐标轴面板（2026-10-20 owner：
「在选股猎手星空图下方做一个多坐标轴的界面，每个坐标轴显示个板块的站上5日线
和站上20日线的百分比」）。

契约要点：
  · 板块 = 韭研主板块（`_jiuyan_board()`），与 daily_summary / 星空图板块名同源；
  · 网格顺序 = 该日板块排名（与星空图右端直标一致，不另外维护一套排序）；
  · `end_date` 与星空图一样"只取 ≤ 该日"，窗口默认 10 天；
  · 前端一张 ECharts 里多 grid，**共享 0~100 的 y 尺度**（否则小图各自缩放不可对读）。
只做纯函数/源码契约，不跑全池扫描。

运行：python -m unittest tests.phase3.test_sector_ma_breadth_panel -v
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


def _rec():
    pts = [{"date": f"2026-10-{d:02d}", "pct5": 40.0 + d, "pct20": 30.0 + d, "n": 100}
           for d in range(6, 21)]
    return {"computed_at": "2026-10-20 11:00:00", "latest_date": "2026-10-20",
            "sectors": [{"name": "半导体", "points": pts},
                        {"name": "机器人", "points": pts}],
            "coverage": {"codes": 1690, "scanned": 1662}}


class TestPayload(unittest.TestCase):
    def test_window_and_end_date(self):
        p = t_gui.Api._sector_ma_payload(_rec(), 10, "20261016")
        self.assertTrue(p["available"])
        for s in p["sectors"]:
            self.assertEqual(len(s["points"]), 10)
            self.assertEqual(s["points"][-1]["date"], "2026-10-16", "end_date 必须收边（≤ 该日）")
        self.assertEqual(p["sectors"][0]["last5"], 56.0)     # 40+16

    def test_sorted_by_daily_summary_rank(self):
        p = t_gui.Api._sector_ma_payload(_rec(), 10, "")
        names = [s["name"] for s in p["sectors"]]
        self.assertEqual(len(names), 2)
        # 排序真源是 daily_summary 当日排名；读不到时按名字兜底（稳定顺序）
        ranks = {}
        try:
            import json
            d = json.loads((_ROOT / "stock_hunter" / "history" / "daily_summary.json")
                           .read_text(encoding="utf-8"))
            k = sorted(d)[-1]
            ranks = {str(r.get("板块") or "").strip(): int(r.get("排名") or 9999) for r in d[k]}
        except Exception:
            pass
        if ranks:
            self.assertEqual(names, sorted(names, key=lambda n: (ranks.get(n, 9999), n)))
        else:
            self.assertEqual(names, sorted(names))

    def test_payload_percentages_in_range(self):
        p = t_gui.Api._sector_ma_payload(_rec(), 10, "")
        for s in p["sectors"]:
            for q in s["points"]:
                self.assertGreaterEqual(q["pct5"], 0)
                self.assertLessEqual(q["pct5"], 100)
                self.assertGreaterEqual(q["pct20"], 0)
                self.assertLessEqual(q["pct20"], 100)


class TestMapping(unittest.TestCase):
    def test_every_summary_sector_has_board_codes(self):
        """星空图里的板块名必须都能在韭研主板块映射里找到成分股，否则该格永远是空的。"""
        import json
        try:
            d = json.loads((_ROOT / "stock_hunter" / "history" / "daily_summary.json")
                           .read_text(encoding="utf-8"))
        except Exception:
            self.skipTest("无 daily_summary.json")
        # 只看**最新一日**：历史档里的旧板块（MLCC/先进封装/食品饮料…）可能已并表或改名，
        # 当前主板块映射里没有它们属正常；但当日板块必须有成分股，否则那一格恒空。
        k = sorted(d)[-1]
        sectors = {str(r.get("板块") or "").strip() for r in (d[k] or []) if isinstance(r, dict)}
        board = t_gui._jiuyan_board()
        have = {v for v in board.values() if v}
        missing = sorted(s for s in sectors if s and s not in have)
        self.assertFalse(missing, f"这些板块在 _jiuyan_board 里没有成分股：{missing}")


class TestFrontend(unittest.TestCase):
    def test_section_is_below_star_map(self):
        self.assertLess(_HTML.index('id="echSectorStars"'),
                        _HTML.index('id="echSectorMa"'), "面板必须在星空图下方")
        self.assertIn('id="sectorMaMeta"', _HTML)

    def test_multi_grid_shared_scale(self):
        self.assertIn("function renderSectorMaBreadth(", _JS)
        self.assertIn("grids.push({ left:", _JS)
        self.assertIn("type: \"value\", gridIndex: i, min: 0, max: 100", _JS,
                      "所有小图必须共享 0~100，否则不可对读")

    def test_x_axis_shows_dates_on_every_cell(self):
        """2026-10-20 owner：「坐标轴横轴需要有日期」——不能只在最后一行标。"""
        self.assertIn('axisLabel: { show: true, color: "#6e7681", fontSize: 8, hideOverlap: true,', _JS)
        self.assertNotIn("axisLabel: { show: r === rows - 1", _JS)

    def test_hover_crosshair_shows_intersections_and_values(self):
        """竖线 snap 到当天 + 交点有实心圆 + 日期胶囊/百分比数值。"""
        self.assertIn('showSymbol: true, symbol: "circle", symbolSize: 3.2', _JS)
        self.assertIn('axisPointer: { type: "line", snap: true,', _JS)
        self.assertIn('label: { show: true, backgroundColor: "#30363d"', _JS)
        self.assertIn("站上5日线 <b>${fmt(p.pct5, 1)}%</b>", _JS)

    def test_wired_into_hunter_tab_and_date_change(self):
        self.assertIn("loadSectorStars(); loadSectorMaBreadth();", _JS)
        self.assertIn("if (_act && _act.dataset.tab === \"hunter\") { loadSectorStars(); loadSectorMaBreadth(); }", _JS)

    def test_uses_same_end_date_as_star_map(self):
        self.assertIn('apiCall("load_sector_ma_breadth", 10, state.date || null)', _JS)


if __name__ == "__main__":
    unittest.main()
