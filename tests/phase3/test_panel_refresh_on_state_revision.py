# -*- coding: utf-8 -*-
"""仓位管理器「本金/现有总资金」+ 文件指纹驱动的面板刷新（2026-11-02 owner）。

owner 两条：
  ① 「总资金是我初始时的资金，现在的资金已经亏损，还要显示现有总资金」；
  ② 「每次更新的时候要把仓位管理器，以及图表分析中的信息都更新掉」。

守四条：
  ① 总资金 = 历史投入本金（principal / deposits，缺了才退回 total_capital）；
  ② 现有总资金 = 持仓市值 + 各账户可用；**目标配比的分母用它**（拿本金当分母会把
     已亏损当成"还欠配"）；
  ③ 后端 `state_revision` 指纹搭在 `load_day` 与 `poll_live_bundle` 里回来（不额外加桥往返）；
  ④ 前端 `_applyStateRev` 只在指纹变化时重拉仓位管理器 / 图表分析。
"""
import os
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
PY = (Path(_ROOT) / "gui.py").read_text(encoding="utf-8")


class TestPositionManagerCapital(unittest.TestCase):
    def test_后端总资金按本金_现有按市值加可用(self):
        i = PY.index("def load_position_manager")
        seg = PY[i:PY.index("def _parse_log_line", i)]     # 整个方法体
        self.assertIn('a.get("principal") is not None', seg)
        self.assertIn('a.get("deposits")', seg, "没有 principal 才退回 deposits")
        self.assertIn('"current_assets"', seg)
        self.assertIn("mkt_now + _avail", seg, "现有总资金 = 持仓市值 + 可用")

    def test_目标配比分母用现有总资金(self):
        self.assertIn("build_position_gap(target_capital, raw, default_pct)", PY)
        self.assertIn('"target_capital"', PY)

    def test_前端仓位管理器同时显示两行(self):
        i = JS.index("function renderPositionManager")
        seg = JS[i:i + 3200]
        self.assertIn("pm.current_assets", seg)
        self.assertIn("现有总资金", seg)
        self.assertIn("累计盈亏", seg)


class TestStateRevision(unittest.TestCase):
    def test_后端把指纹搭进两个载荷(self):
        self.assertIn("def state_revision", PY)
        self.assertIn('"state_revision": self.state_revision(),', PY, "load_day 部分载荷路径")
        self.assertIn('out["state_revision"] = self.state_revision()', PY, "load_day 完整路径")
        self.assertIn('"state_rev": lambda: self.state_revision()', PY,
                      "10s poll_live_bundle 顺带带回，零额外桥往返")

    def test_前端按指纹重拉面板(self):
        self.assertIn("function _applyStateRev", JS)
        i = JS.index("function _applyStateRev")
        seg = JS[i:i + 1800]
        self.assertIn("load_position_manager", seg)
        self.assertIn("loadTradeHistory(true)", seg)
        self.assertIn('ch("ledger")', seg)
        # 两个入口都要喂指纹：load_day 路径只记基线（它自己会重拉），实时 bundle 路径按变化触发
        self.assertIn("_applyStateRev(payload.state_revision, true)", JS)
        self.assertIn('_applyStateRev(_ok("state_rev"))', JS)

    def test_刷新按钮也重拉图表分析(self):
        i = JS.index("async function loadAndRender")
        seg = JS[i:i + 2600]
        self.assertIn("loadTradeHistory(!silent)", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
