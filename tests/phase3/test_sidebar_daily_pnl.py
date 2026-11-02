# -*- coding: utf-8 -*-
"""侧栏「当日盈亏」+ 总资金改按本金（2026-11-02 owner）。

owner 三条：
  ① GUI 没有显示当日盈亏；
  ② 手动盘总资金不对 —— 应该是「历史输入资金的总和」（本金，不随盈亏漂移）；
  ③ 按交割单更新账户信息（数据侧，不在本测试范围）。

守四条：
  ① 手动盘当日盈亏 = Σ 持仓数量 ×（现价 − 昨收），缺昨收的不计入（不把"没数据"算成 0）；
  ② 自动盘当日盈亏取每日权益序列最后一天的 day_pnl，并在 summary 里下发；
  ③ 总资金优先 principal / deposits 之和，**都没有才**退回旧的 total_capital；
  ④ 两个 id 都在 index.html 里存在（否则 JS 静默更新不到）。
"""
import os
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
HTML = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")
PY = (Path(_ROOT) / "t_gui.py").read_text(encoding="utf-8")


class TestSidebar(unittest.TestCase):
    def test_两个当日盈亏行都在(self):
        self.assertIn('id="sumDayPnl"', HTML)
        self.assertIn('id="sumAutoDayPnl"', HTML)

    def test_手动盘当日盈亏口径(self):
        self.assertIn("x.pre_close", JS)
        i = JS.index("function updateSidebarSummary")
        seg = JS[i:i + 2200]
        self.assertIn("dayPnl += qty * (Number(x.price) - Number(x.pre_close))", seg)
        self.assertIn("dayN++", seg, "分母要记：没有昨收的条目不参与")

    def test_总资金优先本金(self):
        i = JS.index("function updateSidebarSummary")
        seg = JS[i:i + 2600]
        self.assertIn("a.principal != null", seg)
        self.assertIn("a.deposits", seg)
        self.assertIn("total_capital", seg, "没有本金字段时才回退旧口径")

    def test_已实现用有符号台账(self):
        """2026-11-02：`realized_pnl`（有符号、含已清仓）优先；旧 realized_loss 只覆盖当前持仓，
        这正是「总实亏 0」的根因。"""
        self.assertIn("portfolioCfg.realized_pnl", JS)
        self.assertIn("realizedSigned", JS)
        self.assertIn("累计已实现盈亏", JS)
        self.assertIn('<span class="sum-label">已实现</span>', HTML)

    def test_自动盘summary带day_pnl(self):
        self.assertIn('"day_pnl": (daily[-1].get("day_pnl") if daily else None)', PY)
        self.assertIn('set("sumAutoDayPnl"', JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
