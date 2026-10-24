# -*- coding: utf-8 -*-
"""自动盘「逐股盈亏状态」（2026-10-24 owner：
「自动盘根据逐步流水模仿手动盘给出每支股票的盈亏状态」）。

数据 = fill 流（`t_io/bridge/events_*.jsonl`）+ 日线缓存收盘价，**不动 heartbeat**
（数量以 heartbeat 为准的纪律见 `_auto_fill_costs`）。
列与「手动盘 · 逐股明细」对齐：已实现 / 浮动 / 合计 / 收益率 / 买入额 / 卖出额 / 费用 /
笔数 / 仍持有 / 区间，末尾保留自动盘特有的「周期明细」。
"""
import os
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402

_JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")


class TestAutoStockPnl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = t_gui.Api().load_auto_pnl()
        cls.stocks = cls.d.get("stocks") or []

    def test_逐股字段齐全(self):
        if not self.stocks:
            self.skipTest("本机无自动盘成交记录")
        x = self.stocks[0]
        for k in ("realized_pnl", "unreal_pnl", "total_pnl", "pnl_pct", "open_qty",
                  "buy_amt", "sell_amt", "fees", "first", "last", "trades_n", "cycles"):
            self.assertIn(k, x, f"缺字段 {k}（要与手动盘逐股表对齐）")
        self.assertAlmostEqual(x["total_pnl"], x["realized_pnl"] + x["unreal_pnl"], places=2)
        if x["buy_amt"]:
            self.assertAlmostEqual(x["pnl_pct"], x["total_pnl"] / x["buy_amt"] * 100, places=1)

    def test_合计等于各周期之和(self):
        if not self.stocks:
            self.skipTest("本机无自动盘成交记录")
        for x in self.stocks[:10]:
            cyc = x.get("cycles") or []
            if not cyc:
                continue
            r = sum(c.get("realized_pnl") or 0 for c in cyc)
            u = sum(c.get("unreal_pnl") or 0 for c in cyc)
            self.assertAlmostEqual(x["realized_pnl"], r, places=1)
            self.assertAlmostEqual(x["unreal_pnl"], u, places=1)

    def test_账单面板挂在自动盘页(self):
        """2026-10-24 owner：「我还是没有看到自动盘的每股盈亏」——**面板根本不在自动盘页**：
        `#auto-pnl` 原来挂在「概览仪表盘」，自动盘 tab 里只有状态/持仓/流水/选股记录。"""
        html = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="autoPnlBody"'), 1, "只该有一份")
        i_auto = html.index('id="tab-auto"')
        i_pnl = html.index('id="auto-pnl"')
        i_main = html.index("</main>")
        self.assertTrue(i_auto < i_pnl < i_main, "逐股盈亏账单必须放在「自动盘」tab 里")
        self.assertIn("loadAutoPnl();", _JS.split('if (tab === "auto")')[1][:400],
                      "切到自动盘页要立刻拉一次账单")

    def test_前端列与手动盘对齐(self):
        i = _JS.index("function renderAutoPnl(d)")
        seg = _JS[i:_JS.index("\nfunction ", i + 10)]
        for c in ("已实现", "浮动", "合计", "收益率", "买入额", "卖出额", "费用",
                  "笔数", "仍持有", "区间"):
            self.assertIn(c, seg, f"自动盘逐股表缺列「{c}」")

    def test_逐股历史可展开且不被裁掉(self):
        """2026-10-24 owner：「自动盘我没有看到每支股票的历史盈利状态」——
        13 列把最右的周期明细裁出屏外了。改成**点行展开** + 外层 overflow-x 兜底。"""
        i = _JS.index("function renderAutoPnl(d)")
        seg = _JS[i:_JS.index("\nfunction ", i + 10)]
        self.assertIn("auto-cyc-row", seg, "历史周期要放在可展开的副行里")
        self.assertIn('style="display:none"', seg, "默认收起")
        self.assertIn("toggleAutoCycles(this)", seg)
        self.assertIn("overflow-x:auto", seg, "窄窗口下要能横向滚动，别再裁列")
        self.assertIn("function toggleAutoCycles(tr)", _JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
