# -*- coding: utf-8 -*-
"""「均线粘合」标签（2026-10-23 owner：选股猎手/建仓扫描/异常监控 + 技术标签里加这个概念）。

**先做了实验**（`scripts/experiment/ma_convergence_forward_20261023.py`）：
666,205 股票·日 / 1932 只 / 700 天 ⇒ 粘合(spread≤3%) 的 T+5 超额 **−0.07%**(t=−2.75)，
越紧越差（≤1.5% 时 −0.22%）⇒ 它描述的是"低波动蓄势"、**没有正向 alpha**。
所以本仓只把它做成**描述性标签**（`色=neutral`、不参与任何打分/排序）。

本文件钉住两件事：
  1. `core.ma_reclaim.convergence_state` 的口径（离散度 / 排列 / 数据不足）；
  2. `_stock_tags_from_df` 真的把它挂上，且**排在趋势标签之后**（共享给四张表的技术标签列）。
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.ma_reclaim import CONV_THR, convergence_state  # noqa: E402


def _df(closes):
    """构造日线帧（要有 volume/high/low：`_stock_tags_from_df` 会剔"无成交且无振幅"的尾根）。"""
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes,
        "volume": [1000.0] * n,
    })


class TestConvergenceState(unittest.TestCase):
    def test_横盘_五线几乎重合(self):
        st = convergence_state([10.0] * 80)
        self.assertAlmostEqual(st["spread"], 0.0, places=6)
        self.assertTrue(st["conv"])
        self.assertEqual(st["arr"], "mix")

    def test_多头排列(self):
        st = convergence_state([10.0 + i * 0.02 for i in range(80)])
        self.assertEqual(st["arr"], "bull")
        self.assertFalse(st["conv5"], "单边上涨时**五线**是发散的（>3%）")
        # ⚠️ 但慢速上涨时 5/10/20 常常只差 1~2% ⇒ 短口径会判粘合（2026-10-23 owner 的口径）

    def test_空头排列(self):
        st = convergence_state([20.0 - i * 0.02 for i in range(80)])
        self.assertEqual(st["arr"], "bear")

    def test_短口径也能判粘合(self):
        """有研新材 2026-04-08 实测：五线 7.6%（MA30/60 还远）但短线只差 1.9%
        ⇒ 「五线≤3% **或** 短线(5/10/20)≤2%」判为粘合（owner 认为它就是粘合的）。"""
        # 40 天平台 → 10 天拉升 0.05/天 → 10 天在高位横住：MA5/10/20 几乎重合，MA30/60 还留在下面
        closes = [10.0] * 40 + [10.0 + i * 0.05 for i in range(1, 11)] + [10.5] * 10
        st = convergence_state(closes)
        self.assertIsNotNone(st)
        self.assertGreater(st["spread"], 3.0, "五线离散应当 >3%")
        self.assertLessEqual(st["spread_short"], 2.0, "但短线(5/10/20)应 ≤2%")
        self.assertTrue(st["conv"])
        self.assertEqual(st["why"], "短线")

    def test_数据不足(self):
        self.assertIsNone(convergence_state([10.0] * 59))
        self.assertIsNone(convergence_state(None))

    def test_阈值常量(self):
        self.assertEqual(CONV_THR, 3.0)


class TestDev365(unittest.TestCase):
    """2026-10-23 owner：四张表把「理由/依据」列换成**偏离 365 日线**。"""

    @classmethod
    def setUpClass(cls):
        import t_gui
        cls.api = t_gui.Api()

    def test_有满365根时给出百分比(self):
        closes = [10.0] * 400
        closes[-1] = 12.0
        r = self.api._stock_tags_from_df(_df(closes), "000001", t30=False)
        self.assertIn("dev365", r)
        ma = sum(closes[-365:]) / 365
        self.assertAlmostEqual(r["dev365"], (12.0 - ma) / ma * 100, places=1)

    def test_不足365根给None(self):
        r = self.api._stock_tags_from_df(_df([10.0] * 300), "000001", t30=False)
        self.assertIsNone(r["dev365"], "不足 365 根不能编一个数出来")

    def test_四张表都用它渲染(self):
        from pathlib import Path as _P
        js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        for fn in ("dev365CellHtml(", "dev365InnerHtml"):
            self.assertIn(fn, js)
        # 猎手 / 30min体检 / 异常监控 三处都是 dev365CellHtml 或 h-dev365；建仓表是 dev365CellHtml
        self.assertGreaterEqual(js.count("dev365CellHtml("), 3)
        self.assertIn("h-dev365", js, "猎手行按 class 就地回填")
        self.assertIn("偏离365日线", js)


class TestDev365Markers(unittest.TestCase):
    """2026-10-24 owner：「不是超买超卖，是偏离太大了直接提醒」⇒ 中性告警，不含方向判断。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path as _P
        cls.js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")

    def test_阈值常量(self):
        self.assertIn("const DEV365_WARN_UP = 60;", self.js)
        self.assertIn("const DEV365_WARN_DOWN = -20;", self.js)

    def test_是中性的偏离过大_不是超买超卖(self):
        # 只看 dev365 这一段：别处（RSI 标签）本来就有"超买/超卖"字样
        i = self.js.index("function dev365Warn")
        seg = self.js[i:i + 1400]
        self.assertIn("偏离过大", seg)
        self.assertNotIn("超买", seg)
        self.assertNotIn("超卖", seg)

    def test_提示写明只提示偏离不含方向(self):
        i = self.js.index("function dev365Warn")
        seg = self.js[i:i + 1400]
        self.assertIn("不含涨跌方向判断", seg)


class TestTagWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import t_gui
        cls.api = t_gui.Api()

    def _labels(self, closes):
        r = self.api._stock_tags_from_df(_df(closes), "000001", t30=False)
        return [t["label"] for t in r["tags"]], r["tags"]

    def test_粘合时挂标签且排在趋势之后(self):
        labels, tags = self._labels([10.0] * 80)
        self.assertIn("均线粘合", labels)
        self.assertEqual(labels[0], "震荡", "趋势标签仍放最前")
        self.assertEqual(labels[1], "均线粘合", "粘合紧随趋势（一眼能对上）")
        t = next(x for x in tags if x["label"] == "均线粘合")
        self.assertEqual(t["color"], "neutral", "描述性标签，不染成涨跌色")
        self.assertIn("无正向 alpha", t.get("title", ""), "tooltip 要写明实验结论，别让人当信号用")

    def test_发散时不挂(self):
        labels, _ = self._labels([10.0 + i * 0.08 for i in range(80)])
        self.assertNotIn("均线粘合", labels)

    def test_标签上限留了位置(self):
        """加了新标签后上限从 6 提到 7，否则会把「破10日线」挤掉。"""
        import inspect
        src = inspect.getsource(self.api._stock_tags_from_df)
        self.assertIn("tags[:7]", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
