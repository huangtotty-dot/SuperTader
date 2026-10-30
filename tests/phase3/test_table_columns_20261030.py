# -*- coding: utf-8 -*-
"""2026-10-30 owner 的三条列改动（三张截图圈红框）+ 新增一列。

删：
  · 持仓 30min 体检：RSI / KDJ-J / CCI / BOLL（这四列口径已写明「不驱动判定」）
  · 建筑信号扫描：**判定** 与 **扫描**（两个红框）
  · 市场风向标：**依据**（build_reason 那一长条文字）
增：
  · 「近30日上涨次数占比」→ 建仓信号扫描 / 异常监控池 / 避风港湾 / 选股猎手 / 持仓30min体检

守四条：
  ① 被删的列真的没了（表头与单元格都不再引用，避免"删了表头还留着 td"这种错位）；
  ② 五张表都有新列，且**表头列数 == colspan**（错一列整行就串位）；
  ③ 后端 `up30` 口径：最近 30 个日收益里 >0 的天数 / 30 × 100，不足 31 根给 None；
  ④ 是"描述列"，不许写成买卖动作。

运行：python -m pytest -q tests/phase3/test_table_columns_20261030.py
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
_TH = re.compile(r"<th(?=[\s>])")


def _hdr_span(anchor: str):
    i = JS.index(anchor)
    j = JS.rindex("<thead><tr>", 0, i)
    k = JS.index("</tr></thead>", j)
    return JS[j:k]


def _cols(anchor: str) -> int:
    seg = _hdr_span(anchor)
    return len(_TH.findall(seg)) + seg.count("${UP30_TH}")


class TestRemovedColumns(unittest.TestCase):
    def test_建仓表删判定与扫描(self):
        seg = _hdr_span('colspan="19" class="empty">无扫描结果')
        self.assertNotIn("<th>判定</th>", seg, "判定列已按 owner 要求删除")
        self.assertNotIn(">扫描</th>", seg, "扫描列已按 owner 要求删除")
        # 单元格也不能留（否则整行错位）
        self.assertNotIn('esc(r.scan_type', JS, "扫描单元格必须一起删")

    def test_体检表删四个超买指标(self):
        seg = _hdr_span("<th>趋势(30m)</th>")
        for k in ("RSI", "KDJ-J", "CCI", "BOLL"):
            self.assertNotIn(f">{k}</th>", seg, f"{k} 列已删")
        self.assertNotIn("ob_.kdj", JS, "KDJ 单元格必须一起删")
        self.assertNotIn("ob_.cci", JS, "CCI 单元格必须一起删")
        # 脚注不能再声称 RSI/KDJ/CCI/BOLL 是日线指标（列都没了）
        self.assertNotIn("RSI/KDJ/CCI/BOLL 为日线超买指标", JS)

    def test_风向标删依据列(self):
        self.assertNotIn("<th>依据</th>", JS, "「依据」表头必须删掉")
        self.assertNotIn('esc(s.build_reason || "—")', JS, "「依据」单元格必须一起删")


class TestUp30Column(unittest.TestCase):
    def test_五张表都有(self):
        n = JS.count("up30InnerHtml(") + JS.count("up30CellHtml(")
        self.assertGreaterEqual(n, 6, f"up30 渲染点太少（{n}），应有 5 张表 + 定义")
        self.assertEqual(JS.count("${UP30_TH}"), 5, "五张表都要这个表头")

    def test_每张表列数与colspan一致(self):
        """表头列数必须等于 colspan（差一列整行就串位，肉眼很难发现）。"""
        seg = _hdr_span('colspan="19" class="empty">无扫描结果')
        self.assertEqual(_cols('colspan="19" class="empty">无扫描结果'), 19)
        m = re.search(r'colspan="(\d+)" class="empty">无扫描结果', seg)
        self.assertIsNone(m, "colspan 在 tbody 里、不在 thead；这里只做占位断言")
        # 体检
        self.assertEqual(_cols("<th>趋势(30m)</th>"), 14)
        self.assertIn('colspan="14" class="cell-dim">${esc(s.code)}', JS, "体检错误行 colspan 要跟着 14")
        # 猎手
        self.assertEqual(_cols('<tbody class="h-sector-group"'), 18)
        self.assertIn('class="h-subgroup-head"><td colspan="18"', JS, "猎手子分组行 colspan 要跟着 18")

    def test_是描述列不是买卖信号(self):
        i = JS.index("function up30Tip")
        seg = JS[i:i + 500]
        self.assertIn("描述列", seg)
        self.assertIn("不是涨幅", seg)
        self.assertNotIn("买入", seg)

    def test_升降箭头走前一窗口(self):
        """owner 2026-10-30：「要根据前几天的比例显示出正在上升还是下降」。
        箭头必须由 (up30 − up30_prev) 决定，且五张表**每一处渲染**都把 prev 传进去
        （只传 v 的渲染点会让箭头永远不出现，而表看着"正常"）。"""
        self.assertIn("function up30ArrowHtml", JS)
        self.assertIn("up30ArrowHtml(v, prev", JS, "up30InnerHtml 要调用箭头渲染")
        # 所有调用点（定义行除外）都必须把 prev / up30_prev 传进去
        lines = JS.splitlines()
        sites = 0
        for ln in lines:
            for fn in ("up30InnerHtml(", "up30CellHtml("):
                if fn not in ln:
                    continue
                if ln.strip().startswith("function "):      # 定义行
                    continue
                if "up30InnerHtml(v, prev)" in ln:          # up30CellHtml 定义体内部调用
                    continue
                sites += 1
                self.assertTrue("up30_prev" in ln or "info.up30_prev" in ln,
                                f"渲染点漏了前一窗口占比：{ln.strip()[:120]}")
        self.assertGreaterEqual(sites, 6, f"up30 渲染点太少（{sites}）")


class TestUp30Color(unittest.TestCase):
    """红/绿（owner 2026-10-30：满足=红、不满足=绿）必须与研究结论一致。"""

    def test_红档是45到55且上升(self):
        self.assertIn("const UP30_RED_LO = 45, UP30_RED_HI = 55;", JS)
        i = JS.index("function up30State")
        seg = JS[i:i + 500]
        self.assertIn("n >= UP30_RED_LO", seg)
        self.assertIn("n < UP30_RED_HI", seg)
        self.assertIn("n > p", seg, "上升 = 当前占比 > 5 个交易日前")

    def test_判不了方向不给绿(self):
        """数据不足 ≠ 不满足：缺 up30_prev 时必须灰，不能涂绿（会把空缺读成结论）。"""
        i = JS.index("function up30InnerHtml")
        seg = JS[i:i + 500]
        self.assertIn('st === "on" ? "up"', seg)
        self.assertIn('st === "off" ? "down"', seg)
        self.assertIn('"cell-dim"', seg)

    def test_tooltip带研究判词(self):
        i = JS.index("function up30Tip")
        seg = JS[i:i + 1500]
        self.assertIn("IC = −0.026", seg)
        self.assertIn("不构成交易信号", seg)


class TestUp30Backend(unittest.TestCase):
    def _df(self, closes):
        n = len(closes)
        return pd.DataFrame({
            "date": [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)],
            "open": closes, "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes], "close": closes,
            "volume": [1000.0] * n,
        })

    def test_口径与手算一致(self):
        import t_gui
        # 30 个收益里 18 个上涨 ⇒ 60.0%
        closes = [10.0]
        for i in range(30):
            closes.append(closes[-1] * (1.01 if i % 5 < 3 else 0.99))
        r = t_gui.Api()._stock_tags_from_df(self._df(closes), "000001", t30=False)
        manual = sum(1 for i in range(1, 31) if closes[i] > closes[i - 1]) / 30 * 100
        self.assertAlmostEqual(r["up30"], round(manual, 1), places=1)

    def test_不足31根给None(self):
        import t_gui
        r = t_gui.Api()._stock_tags_from_df(self._df([10.0] * 30), "000001", t30=False)
        self.assertIsNone(r.get("up30"), "只有 30 根算不出 30 个收益，不能编")

    def test_前一窗口是同口径整体前移5根(self):
        """升降比较必须有基准：`up30_prev` = 窗口整体前移 5 根的同一口径。"""
        import t_gui
        closes = [10.0]
        for i in range(45):
            closes.append(closes[-1] * (1.01 if (i // 7) % 2 == 0 else 0.99))
        r = t_gui.Api()._stock_tags_from_df(self._df(closes), "000001", t30=False)
        n = len(closes)
        manual = sum(1 for j in range(n - 36, n - 5) if closes[j + 1] > closes[j]) / 30 * 100
        self.assertAlmostEqual(r["up30_prev"], round(manual, 1), places=1)
        self.assertNotAlmostEqual(r["up30"], r["up30_prev"], places=1,
                                  msg="构造的两段方向应不同，否则测不到箭头")

    def test_不足36根时prev给None(self):
        import t_gui
        r = t_gui.Api()._stock_tags_from_df(self._df([10.0 + i * 0.01 for i in range(35)]),
                                            "000001", t30=False)
        self.assertIsNone(r.get("up30_prev"), "35 根挪不出 5 天前的 30 日窗口，不能猜方向")

    def test_后端三条下发路径都带up30(self):
        src = (Path(_ROOT) / "t_gui.py").read_text(encoding="utf-8")
        self.assertIn('"up30": _tagres.get("up30")', src, "持仓体检")
        self.assertIn('"up30": (_tags_map.get(c) or {}).get("up30")', src, "异常监控池/避风港湾")
        self.assertEqual(src.count('r["up30"] = info.get("up30")'), 2, "建仓表两条路径")


if __name__ == "__main__":
    unittest.main(verbosity=2)
