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


class TestBias5(unittest.TestCase):
    """2026-10-28 owner：「各板块加一列 5日乖离度」。

    ⚠️ 这列是**描述列**：实测（`tmp/bias5_study.py`，前复权 235 万股票·日）5 日乖离对
    次日**方向**没有可用 edge —— 正乖离越大「次日跌>1%」概率单调升，但次日收益均值也是正的
    （升的是波动）；「次日下跌概率」峰值只在 +4~+6%（≈53% vs 基线 50%）。
    所以它只进展示，**不许驱动任何判定/排序**（与 dev365 同性质）。
    """

    @classmethod
    def setUpClass(cls):
        import t_gui
        cls.api = t_gui.Api()

    def test_有5根时给出百分比(self):
        closes = [10.0] * 9 + [11.0]
        r = self.api._stock_tags_from_df(_df(closes), "000001", t30=False)
        self.assertIn("bias5", r)
        ma5 = sum(closes[-5:]) / 5          # 10,10,10,10,11 → 10.2
        self.assertAlmostEqual(r["bias5"], (11.0 - ma5) / ma5 * 100, places=2)

    def test_不足5根给None(self):
        r = self.api._stock_tags_from_df(_df([10.0] * 4), "000001", t30=False)
        self.assertIsNone(r.get("bias5"), "不足 5 根不能编一个数出来")

    def test_猎手行有这一列(self):
        from pathlib import Path as _P
        js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("h-bias5", js, "猎手行按 class 就地回填")
        self.assertIn("bias5InnerHtml(", js)
        self.assertIn("5日乖离", js)

    def test_阈值是描述性而非信号(self):
        from pathlib import Path as _P
        js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        i = js.index("const BIAS5_RED")
        blk = js[i:i + 1200]
        self.assertIn("不参与任何判定", js[i:i + 4000])
        self.assertNotIn("买入", blk, "描述列不要写交易动作")

    def test_门限灯是红绿两态(self):
        """owner 2026-10-28：「乖离度小于门限显示绿色 大于门限报红色」。

        与 A 股「涨红跌绿」无关，是门限灯：≥ +8% 红、否则绿（负乖离也是绿）。
        """
        from pathlib import Path as _P
        js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        i = js.index("function bias5Hot")
        self.assertIn("BIAS5_RED", js[i:i + 120], "门限由 BIAS5_RED 单一常量控制")
        j = js.index("function bias5Color")
        self.assertIn('? "up" : "down"', js[j:j + 200], "超门限=红(up)、否则=绿(down)")

    def test_多窗口价格旁有乖离(self):
        from pathlib import Path as _P
        js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function bias5FromDma", js)
        self.assertIn("bias5FromDma(it.dma, q.price)", js, "多窗口价格旁必须用后端 dma 反算")

    def test_四张表都有这一列(self):
        """owner 2026-10-28：「建仓信号扫描 / 持仓30min体检 / 异常监控池 / 选股猎手」都要有。

        猎手行要按 class 就地回填（`h-bias5`），另外三张走共用的 `bias5CellHtml`。
        表头写在同一处 `BIAS5_TH_TITLE` 里，四张表的 tooltip 口径不会漂。
        """
        from pathlib import Path as _P
        js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        # 2026-10-29：新增「⚓ 避风港湾」（与异常监控池共用行构造）⇒ 五张表
        self.assertGreaterEqual(js.count(">5日乖离<br>"), 5,
                                "五张表都要有「5日乖离」表头")
        n_calls = js.count("bias5CellHtml(r.bias5") + js.count("bias5CellHtml(s.bias5")
        self.assertEqual(n_calls, 4,
                         "建仓信号扫描 / 异常监控池 / 避风港湾 (r) + 持仓30min体检 (s)")
        self.assertIn('td class="num h-bias5"', js, "选股猎手行（就地回填）")

    def test_后端三个端点都下发bias5(self):
        from pathlib import Path as _P
        src = (_P(_ROOT) / "t_gui.py").read_text(encoding="utf-8")
        self.assertIn('"bias5": _tagres.get("bias5")', src, "持仓30min体检")
        self.assertIn('"bias5": (_tags_map.get(c) or {}).get("bias5")', src, "异常监控池")
        self.assertEqual(src.count('r["bias5"] = info.get("bias5")'), 2,
                         "建仓信号扫描（load_position_builder + refresh_pb 两条路径）")


class TestDev365Markers(unittest.TestCase):
    """偏离 365 日线 = **风险灯**（2026-10-28 owner：「研究下偏离多少会有风险，有风险红、没风险绿」）。

    阈值来自 `scripts/experiment/dev365_risk_20261028.py`（前复权 766,843 股票·日 / 2,648 只，
    分半窗两段同形）：正偏 ≥+60% 时 5 日内回撤>10% 的概率是基线的 2 倍；负偏只有 ≤−60% 有负收益证据，
    −45~−35 反而是超跌反弹 ⇒ 下方红线从旧的 −20 收窄到 −45。
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path as _P
        cls.js = (_P(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")

    def test_阈值常量(self):
        self.assertIn("const DEV365_WARN_UP = 60;", self.js)
        self.assertIn("const DEV365_WARN_DOWN = -45;", self.js,
                      "−20 那条门限没有数据支持（−25~−15 档与基线持平）")

    def test_着色是风险灯不是涨跌红绿(self):
        i = self.js.index("function dev365InnerHtml")
        seg = self.js[i:i + 700]
        self.assertIn("风险灯", seg)
        self.assertIn('w ? "up" : "down"', seg,
                      "超门限=红(up)、未超=绿(down)；旧实现按正负着色")

    def test_不是超买超卖(self):
        i = self.js.index("function dev365Warn")
        seg = self.js[i:i + 1400]
        self.assertNotIn("超买", seg)
        self.assertNotIn("超卖", seg)

    def test_提示写明绿色不等于看多(self):
        i = self.js.index("function dev365Tip")
        seg = self.js[i:i + 600]
        self.assertIn("不是看多", seg)


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
