# -*- coding: utf-8 -*-
"""选股猎手「板块站上5/20日线占比」多坐标轴面板（2026-10-20 owner：
「在选股猎手星空图下方做一个多坐标轴的界面，每个坐标轴显示个板块的站上5日线
和站上20日线的百分比」）。

契约要点：
  · 板块 = 概念主板块（`_concept_board()`），与 daily_summary / 星空图板块名同源；
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

import gui  # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_PY = (_ROOT / "gui.py").read_text(encoding="utf-8")


def _rec():
    pts = [{"date": f"2026-10-{d:02d}", "pct5": 40.0 + d, "pct20": 30.0 + d, "n": 100}
           for d in range(6, 21)]
    return {"computed_at": "2026-10-20 11:00:00", "latest_date": "2026-10-20",
            "sectors": [{"name": "半导体", "points": pts},
                        {"name": "机器人", "points": pts}],
            "coverage": {"codes": 1690, "scanned": 1662}}


class TestPayload(unittest.TestCase):
    def test_window_and_end_date(self):
        p = gui.Api._sector_ma_payload(_rec(), 10, "20261016")
        self.assertTrue(p["available"])
        for s in p["sectors"]:
            self.assertEqual(len(s["points"]), 10)
            self.assertEqual(s["points"][-1]["date"], "2026-10-16", "end_date 必须收边（≤ 该日）")
        self.assertEqual(p["sectors"][0]["last5"], 56.0)     # 40+16

    def test_sorted_by_daily_summary_rank(self):
        p = gui.Api._sector_ma_payload(_rec(), 10, "")
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
        p = gui.Api._sector_ma_payload(_rec(), 10, "")
        for s in p["sectors"]:
            for q in s["points"]:
                self.assertGreaterEqual(q["pct5"], 0)
                self.assertLessEqual(q["pct5"], 100)
                self.assertGreaterEqual(q["pct20"], 0)
                self.assertLessEqual(q["pct20"], 100)


class TestMapping(unittest.TestCase):
    def test_every_summary_sector_has_board_codes(self):
        """星空图里的板块名必须都能在概念主板块映射里找到成分股，否则该格永远是空的。"""
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
        board = gui._concept_board()
        have = {v for v in board.values() if v}
        missing = sorted(s for s in sectors if s and s not in have)
        self.assertFalse(missing, f"这些板块在 _concept_board 里没有成分股：{missing}")


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
        self.assertIn('axisLabel: { show: true, color: "#', _JS)
        self.assertIn("hideOverlap: true", _JS)
        self.assertNotIn("axisLabel: { show: r === rows - 1", _JS)

    def test_hover_crosshair_shows_intersections_and_values(self):
        """竖线 snap 到当天 + 交点有实心圆 + 日期胶囊/百分比数值。"""
        self.assertIn('showSymbol: true, symbol: "circle", symbolSize: 3.4', _JS)
        self.assertIn('axisPointer: { type: "line", snap: true,', _JS)
        self.assertIn('label: { show: true, backgroundColor: "#30363d"', _JS)
        self.assertIn("站上5日线 <b>${fmt(p.pct5, 1)}%</b>", _JS)

    def test_20261104_每行两个与深色坐标系(self):
        """owner：「每行显示 2 个；坐标系用深色背景；名字改成板块指标」。"""
        self.assertIn("const cols = 2;", _JS)
        self.assertIn('backgroundColor: "#0b1117"', _JS, "整张画布必须深色")
        self.assertIn('fill: "#121d29"', _JS, "每个坐标系要垫深色卡片")
        self.assertIn("板块指标", _HTML)
        self.assertNotIn("板块「站上5/20日线」占比", _HTML)

    def test_wired_into_hunter_tab_and_date_change(self):
        self.assertIn("loadSectorStars(); loadSectorMaBreadth();", _JS)
        self.assertIn("if (_act && _act.dataset.tab === \"hunter\") { loadSectorStars(); loadSectorMaBreadth(); }", _JS)

    def test_uses_same_end_date_as_star_map(self):
        # 2026-10-23：两块图都改走 `_panelDate()`（= 猎手「重新生成」那天的覆盖 || 全局日期）
        self.assertIn('apiCall("load_sector_ma_breadth", 10, _panelDate())', _JS)
        self.assertIn('apiCall("load_sector_constellation", 10, _panelDate())', _JS)

    def test_猎手跑历史日时两块图跟到同一天(self):
        """owner：「运行了 4月8日的选股猎手，星轨图 / 占比图还是显示最近 10 日」。"""
        self.assertIn("_setHunterPanelDate(date)", _JS, "猎手跑完要把两块图钉到同一天")
        self.assertIn("function _clearHunterDayCaches()", _JS,
                      "换日期要清掉按板块缓存的标签/结构（否则拿到今天算的）")


class TestConvergenceSeries(unittest.TestCase):
    """2026-10-23 owner：「这个部分再加一个，统计均线粘合在坐标轴中占板块的比例」。"""

    def test_后端给出粘合占比与独立分母(self):
        import inspect
        src = inspect.getsource(gui._compute_sector_ma_breadth)
        self.assertIn("pct_conv", src)
        self.assertIn("n60", src, "粘合需要 ≥60 根历史，分母必须单独算")
        self.assertIn("CONV_THR as _CONV_THR", src)
        self.assertIn("CONV_THR_SHORT as _CONV_THR_S", src,
                      "口径必须与「均线粘合」技术标签同源（五线≤3% 或 短线≤2%）")

    def test_前端第三条序列(self):
        self.assertIn('const SECMA_CONV = "#a371f7"', _JS)
        self.assertIn('series.push(mk(s.points.map(p => p.pct_conv), SECMA_CONV, "均线粘合"', _JS)
        # 改用「五线≤3% 或 短线≤2%」后中位 20.6%/p90 42.9%，与另两条同量级 ⇒ 共用 0~100 尺度
        # 2026-11-01：图例多了「周线MACD红柱」（每板块 4 条系列）
        self.assertIn('legend: { data: ["站上5日线", "站上20日线", "多头占比", "均线粘合", "周线MACD红柱"]', _JS)
        self.assertNotIn('position: "right"', _JS.split("function renderSectorMaBreadth")[1].split("function ")[0],
                         "粘合率已与另两条同量级，不需要单独右轴")

    def test_标题显示粘合具体数值(self):
        """owner：「板块当天的粘合占比显示具体数值」。"""
        self.assertIn("last_conv", _JS)
        self.assertIn("粘合 ${fmt(s.last_conv, 0)}%", _JS)

    def test_提示框按每板块4条系列取索引(self):
        # 2026-11-01：加了「周线MACD红柱」⇒ 每板块 4 条系列（5日/20日/粘合/周红）
        self.assertIn("Math.floor((ps[0].seriesIndex || 0) / 5)", _JS,
                      "每板块现在是 5 条系列，除错会串板块")
        self.assertIn('const SECMA_WM = "#ff7b72"', _JS)


class TestBullShare(unittest.TestCase):
    """2026-11-04 owner：「再增加一个指标，多头占比」。

    口径 = 日线三态的「多头」（MA60 的 20 交易日变化率 > +3%，与
    `core.position_structure.MA60_FLAT_PCT` 同源）；分母只算有 80 根以上历史的票。
    """

    def test_后端输出多头占比且分母独立(self):
        import inspect
        src = inspect.getsource(gui._compute_sector_ma_breadth)
        self.assertIn('"pct_bull"', src)
        self.assertIn('"n_bull"', src)
        self.assertIn("MA60_FLAT_PCT", src, "阈值必须与日线三态同源，不能各写一份")
        self.assertIn("_SECTOR_MA_SCHEMA = 6", _PY,
                      "加字段必须升 schema，否则旧磁盘档会被当新鲜回放")
        self.assertIn('"last_bull"', _PY)

    def test_前端青线胶囊与提示框(self):
        self.assertIn('const SECMA_BULL = "#2dd4bf"', _JS)
        self.assertIn('series.push(mk(s.points.map(p => p.pct_bull), SECMA_BULL, "多头占比")', _JS)
        self.assertIn("多头 ${fmt(s.last_bull, 0)}%", _JS, "标题胶囊要显示具体数值")
        self.assertIn("多头占比 <b>${p.pct_bull == null", _JS, "tooltip 要有一行")
        self.assertIn("青=多头占比", _HTML, "面板说明文字要提到多头")


if __name__ == "__main__":
    unittest.main()
