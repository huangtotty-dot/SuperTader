# -*- coding: utf-8 -*-
"""历史成交页**渲染冒烟**单测：在 node 里用 DOM 桩真跑 renderTradeHistory。

为什么要有这一条：2026-10-10 出过一次「三张图空白」——renderTradeHistory 里一个 const
声明晚于使用（TDZ ReferenceError），函数跑到中间就抛了，后面的图**静默留白**，界面上
没有任何提示。`node --check` 查不出来（语法是合法的），python 侧也查不出来，只有真跑
一遍才能发现。这个测试就是那道闸。

全离线：合成一份 xlsx → 走真实解析链 → 生成台账 → 交给 node 跑渲染，断言五张图都画了。
node 不存在时跳过（不把环境问题当失败）。

运行：python tests/phase3/test_trade_history_ui_render.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.trade_history as th  # noqa: E402

_HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "th_ui_harness.js")


class TestTradeHistoryUiRender(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ledger(self):
        """合成两账户（含建仓/清仓/同日做T）→ 跑真实解析链 → 台账。"""
        import openpyxl
        head = ["成交日期", "证券代码", "证券名称", "操作", "成交数量",
                "成交均价", "成交金额", "发生金额", "佣金"]
        rows = [
            ("2026-01-05", "600000", "测试甲", "买入", 100, 10.0, 1000, -1005, 5),
            ("2026-01-06", "600000", "测试甲", "卖出", 100, 11.0, 1100, 1095, 5),
            ("2026-02-10", "600000", "测试甲", "买入", 200, 9.0, 1800, -1805, 5),
            ("2026-03-02", "600000", "测试甲", "买入", 100, 8.0, 800, -805, 5),
            ("2026-03-02", "600000", "测试甲", "卖出", 100, 8.5, 850, 845, 5),
        ]
        paths = []
        for name, acct in (("东方.xlsx", "东方"), ("东莞.xlsx", "东莞")):
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["资金账号"] + head)
            for r in rows:
                ws.append([acct] + list(r))
            p = self.d / name
            wb.save(p)
            paths.append(p)
        led = th.build_from_files(paths)
        led["imported"] = True          # GUI 落盘时会带这个字段（renderTradeHistory 靠它判断）
        out = self.d / "ledger.json"
        out.write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        return out

    def test_五张图全部渲染(self):
        if not shutil.which("node"):
            self.skipTest("环境里没有 node")
        led = self._ledger()
        r = subprocess.run(["node", _HARNESS, str(led)],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        print(out)
        self.assertEqual(r.returncode, 0, f"渲染冒烟失败：\n{out}")
        self.assertIn("ALL PASS", out)
        self.assertIn("echGuard 生效", out, "故障注入应当被 echGuard 兜住")


if __name__ == "__main__":
    unittest.main(verbosity=2)
