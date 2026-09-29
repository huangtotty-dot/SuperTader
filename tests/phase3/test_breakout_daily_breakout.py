# -*- coding: utf-8 -*-
"""「当日有效突破」+ 北交所代码映射 + 全池扫描 离线单测（2026-09-29）。

覆盖三件事，对应本次三项需求：

  T1 **codec 北交所市场**：`430047/873169/920819 → BJ`；且 `900xxx`（沪B）**仍归 SH**——
     这是本次最危险的回归点：`920` 与 `900` 都以 `9` 开头，一刀切会把沪B判成北交所。
  T2 **当日有效突破口径**（owner 2026-09-29）：昨收 ≤ 上沿 < 今价，幅度 0.3~8%。
     核心回归闸是**「陈旧突破不得再报」**：旧标签 `向上突破` 只看"当前价在箱体上沿之上"
     （无任何当日成分）⇒ 三周前突破的票每天照旧被扫出来，这正是需求 5 要改掉的。
  T3 **池子 = watchlist 全量**：静态断言 `_jiuyan_concepts` 过滤已从 `_breakout_pool_codes`
     移除（原过滤把 5041 只砍到 1258 只）。

铁律：**全离线**。全部用合成帧，不读 `t_io/`、不打网络、不读 `watchlist_jiuyan.json`
（对池子用**静态源码断言**，避免依赖会变动的本地数据文件）。

运行：python tests/phase3/test_breakout_daily_breakout.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from core.market_data import codec  # noqa: E402
from core.market_data import bj_daily  # noqa: E402


class TestCodecBeijing(unittest.TestCase):
    """T1：北交所市场判定。"""

    def test_北交所各段归BJ(self):
        for c in ("430047", "873169", "920819", "830799", "400001"):
            self.assertEqual(codec.market_of(c), "BJ", f"{c} 应判北交所")

    def test_沪B不得被误判为北交所(self):
        """最危险的回归点：900xxx 与 920xxx 同以 9 开头。"""
        self.assertEqual(codec.market_of("900901"), "SH", "900xxx 是沪B，必须仍归 SH")
        self.assertEqual(codec.to_gm("900901"), "SHSE.900901")

    def test_既有市场不回归(self):
        for c, m in (("600481", "SH"), ("688981", "SH"), ("000001", "SZ"),
                     ("002639", "SZ"), ("300750", "SZ")):
            self.assertEqual(codec.market_of(c), m, f"{c} 市场判定回归了")

    def test_to_gm前缀(self):
        self.assertEqual(codec.to_gm("430047"), "BJSE.430047")
        self.assertEqual(codec.to_gm("600481"), "SHSE.600481")
        self.assertEqual(codec.to_gm("000001"), "SZSE.000001")

    def test_to_internal可剥北交所前缀(self):
        for g in ("BJSE.430047", "bj430047"):
            self.assertEqual(codec.to_internal(g), "430047", f"{g} 未正确剥离")

    def test_多账户后缀仍剥离(self):
        self.assertEqual(codec.to_gm("430047_A"), "BJSE.430047")


def _band_frame(n=200):
    """n 天在 9.8~10.2 窄幅震荡（构成箱体），末日日期=今日。

    箱体上沿由 `_detect_boxes` 自己算（分位数法），测试不写死数值——
    写死会随检测器调参而脆断。
    """
    rows = []
    t0 = datetime.now() - timedelta(days=int(n * 1.5))
    for i in range(n):
        px = 10.0 + (0.2 if i % 2 else -0.2)
        rows.append({"date": (t0 + timedelta(days=int(i * 1.5))).strftime("%Y-%m-%d"),
                     "open": px, "high": px + 0.03, "low": px - 0.03,
                     "close": px, "volume": 1000.0})
    rows[-1]["date"] = datetime.now().strftime("%Y-%m-%d")
    return pd.DataFrame(rows)


class TestDailyBreakoutRule(unittest.TestCase):
    """T2：当日有效突破。"""

    @classmethod
    def setUpClass(cls):
        import t_gui
        cls.api = t_gui.Api()
        cls.df = _band_frame()
        cls.box_top = cls.api._box_top_for(cls.df.iloc[:-1])

    def test_合成帧确实构成箱体(self):
        """前置自检：若检测器变了导致这里没箱子，下面的断言会变成空断言。"""
        self.assertIsNotNone(self.box_top, "合成窄幅震荡帧未构成箱体，本类断言将失效")

    def _with_today(self, px):
        d = self.df.copy()
        d.loc[d.index[-1], "close"] = round(px, 3)
        return d

    def test_今价首穿上沿命中(self):
        r = self.api._breakout_probe_one("T", self._with_today(self.box_top * 1.02))
        self.assertIsNotNone(r, "今日首次站上箱体上沿 2% 应当命中")
        self.assertEqual(r["box_top"], round(self.box_top, 3))
        self.assertAlmostEqual(r["pct_above"], 2.0, places=1)

    def test_陈旧突破不得再报(self):
        """核心回归闸（需求 5）：近 25 天就已在箱体上沿之上 ⇒ 今天不是"当天突破"。"""
        d = self.df.copy()
        px = round(self.box_top * 1.02, 3)
        for i in range(len(d) - 25, len(d)):
            d.loc[d.index[i], ["close", "high", "low"]] = [px, px, px]
        self.assertIsNone(self.api._breakout_probe_one("T", d),
                          "陈旧突破被当成当日突破报出（旧口径的毛病）")

    def test_幅度过小不命中(self):
        self.assertIsNone(self.api._breakout_probe_one("T", self._with_today(self.box_top * 1.001)),
                          f"幅度 < {self.api._BK_MIN_PCT}% 不应命中")

    def test_幅度过大不命中(self):
        self.assertIsNone(self.api._breakout_probe_one("T", self._with_today(self.box_top * 1.09)),
                          f"幅度 > {self.api._BK_MAX_PCT}% 不应命中（已完全脱离箱体）")

    def test_未穿上沿不命中(self):
        self.assertIsNone(self.api._breakout_probe_one("T", self._with_today(self.box_top * 0.98)))

    def test_非交易日不得报今日(self):
        """末日不是今日 ⇒ 说明今日 bar 不存在（周末/盘前），此时报出的会是**上一交易日**的突破。"""
        d = self.df.copy()
        d.loc[d.index[-1], "close"] = round(self.box_top * 1.02, 3)
        d.loc[d.index[-1], "date"] = "2026-09-26"
        self.assertIsNone(self.api._breakout_probe_one("T", d),
                          "非今日 bar 仍被当成「今天」（周末会把周五的突破报出去）")

    def test_切片后不足30根不命中(self):
        """恰好 30 根全量 ⇒ 切掉当日后剩 29 ⇒ `_detect_boxes` 静默返回 [] ⇒ 必须显式挡。"""
        d = self._with_today(self.box_top * 1.02).tail(30).reset_index(drop=True)
        self.assertIsNone(self.api._breakout_probe_one("T", d),
                          "切片后 29 根仍进入检测，会静默漏检")

    def test_空帧与None不崩(self):
        for bad in (None, pd.DataFrame()):
            self.assertIsNone(self.api._breakout_probe_one("T", bad))

    def test_口径常量写死(self):
        """owner 2026-09-29 拍板：0.3~8%。常量被人改动必须显式改这里。"""
        self.assertEqual(self.api._BK_MIN_PCT, 0.3)
        self.assertEqual(self.api._BK_MAX_PCT, 8.0)


class TestPoolAndBatching(unittest.TestCase):
    """T3：池子与批量参数（静态断言，避免依赖可变的本地数据文件）。"""

    @classmethod
    def setUpClass(cls):
        import t_gui
        with open(t_gui.__file__, encoding="utf-8") as fh:
            cls.src = fh.read()

    @staticmethod
    def _body(name):
        """取函数体源码，**剥掉 docstring 与注释**。

        不能直接在原始源码里 `assertNotIn`：本文件第一版就栽在这里——旧标识符
        （`_jiuyan_concepts` / `load_stock_tags_batch`）恰好出现在"说明为何改掉它"的
        docstring 里，于是断言把说明当成了代码。
        """
        import ast
        for node in ast.walk(ast.parse(TestPoolAndBatching.src)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = node.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]
                return "\n".join(ast.unparse(n) for n in body)
        return ""

    def test_池子已去掉概念过滤(self):
        body = self._body("_breakout_pool_codes")
        self.assertTrue(body, "_breakout_pool_codes 缺失")
        self.assertNotIn("_jiuyan_concepts", body,
                         "池子仍在按概念过滤 ⇒ 5041 只会被砍到 1258 只，与需求不符")
        self.assertIn("isdigit", body)

    def test_批量上限不超GM实测行数上限(self):
        """GM `history` 的限制是**总行数约 200k**，不是标的数。

        实测（2026-09-29，300 日历日留窗 ≈200 根/只）：
          1000 只 = 199k 行 ✓ / 1500 只 ✗ GmError 1029「查询结果过大」。
        批次大小 × 每只根数 必须留在该上限内。
        """
        from core.market_data.facade import MarketDataFacade
        import t_gui
        batch = MarketDataFacade._BATCH_MAX
        bars = t_gui.Api._BK_SCAN_BARS
        self.assertGreater(batch, 0)
        self.assertLessEqual(batch * bars, 200_000,
                             f"{batch}×{bars}={batch * bars} 行，顶到 GM 上限会被 1029 拒绝")

    def test_扫描不再用逐只标签路径(self):
        """逐只 `load_stock_tags_batch` 是 GM 单线程串行 + 90s 批超时静默丢票，全池不可行。"""
        body = self._body("_scan_breakout")
        self.assertTrue(body, "_scan_breakout 缺失")
        self.assertIn("daily_many", body, "扫描未走批量取数")
        self.assertNotIn("load_stock_tags_batch", body, "扫描仍在走逐只标签路径")

    def test_扫描状态加锁(self):
        for fn in ("start_breakout_scan", "get_breakout_scan", "_scan_breakout",
                   "_bk_cache_get", "_bk_cache_put"):
            self.assertTrue(self._body(fn), f"{fn} 缺失")
        self.assertIn("_BREAKOUT_LOCK", self.src, "扫描状态未加锁（800ms 读线程可读到中间态）")

    def test_强制重扫先失效缓存(self):
        """`get_breakout_scan` 优先返回缓存 ⇒ force 若不先失效，前端第一次 poll 就拿到
        上一轮结果、判 done 并停止轮询，用户看到旧数据、"重新扫描"形同无效。"""
        body = self._body("start_breakout_scan")
        self.assertIn("force", body)
        self.assertIn("pop(cache_key", body, "force 分支未失效内存缓存")
        self.assertIn("unlink", body, "force 分支未失效磁盘缓存")

    def test_两条返回路径都带no_data(self):
        """北交所取不到日线时靠 no_data 如实汇报；两条路径（live state / cache）都不能丢。"""
        self.assertIn("no_data", self._body("get_breakout_scan"),
                      "get_breakout_scan 未带 no_data（缓存命中路径会丢失）")
        self.assertIn("no_data", self._body("_scan_breakout"))
        self.assertIn('"no_data": hit.get("no_data"', self.src)


class TestBjDegrade(unittest.TestCase):
    """北交所降级：取不到返回空帧、不抛（当前环境四种源全不通，见 bj_daily docstring）。"""

    def test_空帧列齐备(self):
        self.assertEqual(list(bj_daily._empty().columns),
                         ["date", "open", "high", "low", "close", "volume"])

    def test_解析东财klines(self):
        raw = ('{"data":{"name":"诺思兰德","klines":["2026-09-25,10.0,10.5,10.6,9.9,1234",'
               '"2026-09-26,10.5,10.7,10.8,10.4,2345"]}}')
        df = bj_daily._parse(raw)
        self.assertEqual(len(df), 2)
        self.assertEqual(list(df.columns), ["date", "open", "high", "low", "close", "volume"])
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 10.7)

    def test_解析空响应得空帧(self):
        for raw in ('{"data":{"klines":[]}}', '{"data":null}', '{}'):
            self.assertTrue(bj_daily._parse(raw).empty)

    def test_东财lows单位不倒置(self):
        """东财 f53=收、f54=高、f55=低 —— 顺序写错会静默产生 high<low 的帧。"""
        raw = '{"data":{"klines":["2026-09-26,10.5,10.7,10.8,10.4,2345"]}}'
        r = bj_daily._parse(raw).iloc[0]
        self.assertLessEqual(r["low"], r["open"])
        self.assertLessEqual(r["low"], r["close"])
        self.assertGreaterEqual(r["high"], r["open"])
        self.assertGreaterEqual(r["high"], r["close"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
