# -*- coding: utf-8 -*-
"""板块「站上5/20日线」面板加「周线 MACD 红柱占比」（2026-11-01 owner）。

守四条：
  ① `_weekly_macd_flags_by_date` 的**最后一天**必须与 `weekly_macd_state().red` 同口径；
  ② 预热不足（<35 周）的那些日子返回 None —— 不把"没数据"算成"不是红柱"；
  ③ 面板输出带 `pct_wm / n_wm`，且缓存 schema 升到 5（否则会复用没有该字段的旧缓存）；
  ④ 前端：新增红线系列 + 「周红」胶囊 + tooltip 第 4 行（每板块 4 条系列）。

运行：python -m pytest -q tests/phase3/test_sector_ma_weekly_macd.py
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PY = (Path(_ROOT) / "t_gui.py").read_text(encoding="utf-8")
JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
HTML = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")


def _series(code):
    fp = Path(_ROOT) / "t_io" / "cache" / "daily_kline" / f"{code}.json"
    rows = (json.loads(fp.read_text(encoding="utf-8")) or {}).get("rows") or []
    return [str(r["date"])[:10] for r in rows], [r["close"] for r in rows], rows


class TestFlags(unittest.TestCase):
    def setUp(self):
        import t_gui
        self.g = t_gui

    def test_最后一天与状态同口径(self):
        for code, want in (("603028", True), ("600362", False)):
            dates, closes, rows = _series(code)
            flags = self.g._weekly_macd_flags_by_date(dates, closes)
            self.assertIsNotNone(flags)
            self.assertEqual(flags[-1], want, f"{code} 最后一天红柱判定与 weekly_macd_state 不一致")
            st = self.g.weekly_macd_state(self.g._weekly_closes_from_rows(rows))
            self.assertEqual(st["red"], want)

    def test_预热不足返回None(self):
        dates, closes, _ = _series("603028")
        flags = self.g._weekly_macd_flags_by_date(dates, closes)
        self.assertIsNone(flags[0], "第一根不可能已有 35 周历史")
        self.assertGreater(sum(1 for f in flags if f is None), 100,
                           "预热期应有上百根 None（约 34 周）")
        self.assertEqual(len(flags), len(dates))


class TestPanelWiring(unittest.TestCase):
    def test_后端输出pct_wm且schema升版(self):
        self.assertIn('"pct_wm"', PY)
        self.assertIn('"n_wm"', PY)
        self.assertIn("_SECTOR_MA_SCHEMA = 6", PY)
        self.assertIn('"last_wm"', PY)

    def test_前端新系列与胶囊(self):
        self.assertIn("SECMA_WM", JS)
        self.assertIn('"周线MACD红柱"', JS, "图例/系列名")
        self.assertIn("周红", JS, "标题里的红胶囊")
        self.assertIn("/ 5", JS, "tooltip 按每板块 5 条系列切分（2026-11-04 加多头）")
        self.assertIn("pct_wm", JS)
        self.assertIn("周线MACD红柱", HTML, "面板说明文字要提到周线 MACD")


if __name__ == "__main__":
    unittest.main(verbosity=2)
