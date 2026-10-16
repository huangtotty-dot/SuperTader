# -*- coding: utf-8 -*-
"""自动盘飞书推送的「名称 + 代码 + 行业」标签单测（2026-10-16）。**全离线**。

owner 2026-10-16：「发送的飞书消息，要有股票代码同时要有股票的中文名称以及所属行业」。

## 缺陷是什么

`gm_bridge/watcher.py` 的 `STOCK_NAMES` 是**硬编码 7 只**（旧引擎时代遗留）⇒ 2026-10-16
实际买入的 `603168/600127/601118/601949` **一个都不在表里**，推出去是
`🔵 成交 — 603168`（**没有名称**）；行业则**从来没有过**。

修法：真源改读 `t_io/rotation/industry_map.csv`（代码/名称/行业名称，与
`prescreen.full_market_meta()` 和 GUI 行业列**同一个文件**），stdlib 直读 + TTL 缓存；
取不到就**如实退回纯代码，不编造**。

⚠️ **刻意不 import watcher**：watcher 是长驻进程（`__main__` 里起轮询/发飞书）。
本测试按标记从源码切出标签那一段再 exec —— 测真源码、零副作用。

运行：python -m unittest tests.phase3.test_bridge_push_label -v
"""
import os
import re
import sys
import time
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_WATCHER_FP = _ROOT / "execution" / "auto" / "_gm" / "gm_bridge" / "watcher.py"
_SRC = _WATCHER_FP.read_text(encoding="utf-8")
_A = _SRC.index("# ── 股票「名称 + 代码 + 所属行业」标签")
_B = _SRC.index("def _events_path")


def _load_label_block(env_root=None):
    """切出标签那一段源码并 exec（用真源码，不 import 长驻进程）。"""
    ns = {"os": os, "time": time, "__file__": str(_WATCHER_FP)}
    exec(compile(_SRC[_A:_B], "watcher.py:label", "exec"), ns)
    if env_root is not None:
        ns["_meta_path"] = lambda: os.path.join(env_root, "t_io", "rotation", "industry_map.csv")
    return ns


class TestStockLabel(unittest.TestCase):
    def setUp(self):
        self.ns = _load_label_block()
        self.label = self.ns["_stock_label"]

    def test_known_stock_has_name_and_industry(self):
        """本条的**正题**：名称 + 代码 + 行业三样都要有。"""
        got = self.label("603168")
        self.assertIn("莎普爱思", got, "缺中文名称")
        self.assertIn("603168", got, "缺股票代码")
        self.assertIn("医药生物", got, "缺所属行业")

    def test_today_bought_stocks_all_resolve(self):
        """2026-10-16 实际买入的 4 只（旧硬编码表里一只都没有）必须全解析出名称+行业。"""
        for code, name, ind in (("603168", "莎普爱思", "医药生物"),
                                ("600127", "金健米业", "粮油加工"),
                                ("601118", "海南橡胶", "农林牧渔"),
                                ("601949", "中国出版", "传媒")):
            got = self.label(code)
            self.assertIn(name, got, f"{code} 缺名称")
            self.assertIn(ind, got, f"{code} 缺行业")

    def test_etf_fallback_has_no_invented_industry(self):
        """ETF 不在申万行业表里 ⇒ 只给名称，**不许编造行业**。"""
        got = self.label("588170")
        self.assertIn("科创芯片ETF", got)
        self.assertNotIn("｜", got, "取不到行业时不该出现行业分隔符（那是编造）")

    def test_unknown_code_degrades_to_bare_code(self):
        """表里没有 ⇒ 如实只给 6 位码（本仓忌讳"把无数据画成有数据"）。"""
        self.assertEqual(self.label("999999"), "999999")

    def test_empty_code(self):
        self.assertEqual(self.label(""), "")
        self.assertEqual(self.label(None), "")

    def test_missing_csv_degrades_without_crash(self):
        """表读不到 ⇒ 退回纯代码，**不崩**（且不编造名称）。"""
        import tempfile
        ns = _load_label_block(env_root=tempfile.mkdtemp())
        self.assertEqual(ns["_stock_label"]("603168"), "603168")


class TestMetaPath(unittest.TestCase):
    """⚠️ 2026-10-16 施工时这里**真的错了一层**（落到 `execution/t_io/...`，不存在）⇒
    标签静默全退化裸码，而单看代码完全看不出来。本组钉死层数。"""

    def test_path_points_at_repo_root(self):
        ns = _load_label_block()
        p = Path(ns["_meta_path"]())
        self.assertEqual(p.parts[-3:], ("t_io", "rotation", "industry_map.csv"),
                         f"路径尾部不对: {p}")
        # 仓根必须真的是仓根（有 .git 或 main.py），而不是 execution/
        root = p.parents[2]
        self.assertTrue((root / "main.py").exists(),
                        f"上溯层数错了：算出来的仓根 {root} 里没有 main.py")

    def test_real_csv_exists_and_is_usable(self):
        ns = _load_label_block()
        self.assertTrue(os.path.exists(ns["_meta_path"]()),
                        "行业表不存在 —— 标签会全体退化裸码")


class TestPushSitesUseTheLabel(unittest.TestCase):
    """所有会被推出去的股票标识都必须走 `_stock_label`，不许再插裸码。"""

    def test_no_bare_code_in_push_calls(self):
        # ⚠️ 先剥注释：判据看代码，不看说明文字
        code_only = re.sub(r"(?m)^\s*#.*$", " ", _SRC)
        # 推送调用里出现 f"{code} ..." 这种裸插值就是漏网
        bad = re.findall(r'_push[a-z_]*\([^)]*f"[^"]*\{code\}', code_only)
        self.assertEqual(bad, [], f"这些推送点还在插裸 6 位码: {bad}")

    def test_known_push_sites_use_label(self):
        for title in ("成交 — ", "委托被拒 — ", "现金不足 — ", "风控 — ", "KILL_SWITCH — "):
            self.assertIn(title + "{label}", _SRC, f"{title} 没走 label")

    def test_position_limit_uses_label(self):
        """仓位超限原来塞的是裸码（标题里连股票都没有）——已改。"""
        self.assertIn('_stock_label(code)} 市值占比', _SRC)


class TestCoreStockMeta(unittest.TestCase):
    """仓内（人工侧）共用实现 `core/stock_meta.py` —— 与 watcher 侧**行为契约必须一致**。"""

    def setUp(self):
        from core import stock_meta
        self.m = stock_meta
        stock_meta._META = {}
        stock_meta._META_AT = 0.0

    def test_name_and_industry(self):
        self.assertEqual(self.m.name_of("601949"), "中国出版")
        self.assertEqual(self.m.industry_of("601949"), "传媒")

    def test_label_format_matches_watcher(self):
        """两侧同契约：`名称(代码｜行业)`。"""
        self.assertEqual(self.m.stock_label("603168"), "莎普爱思(603168｜医药生物)")
        ns = _load_label_block()
        self.assertEqual(ns["_stock_label"]("603168"), self.m.stock_label("603168"),
                         "watcher 侧与 core 侧的标签实现**已经不一致**了")

    def test_no_industry_is_not_invented(self):
        self.assertEqual(self.m.industry_of("999999"), "")
        self.assertEqual(self.m.stock_label("999999"), "999999")

    def test_caller_name_wins(self):
        """调用方手里的名字优先（可能比表新）；行业仍从表取。"""
        got = self.m.stock_label("603168", "改名了")
        self.assertIn("改名了", got)
        self.assertIn("医药生物", got)

    def test_missing_table_degrades(self):
        """表路径指向不存在 ⇒ 退回纯代码。

        ⚠️ 必须 patch `_default_path` —— 首版写成 `load_meta(path=<假>)`，
        但 `stock_label` 内部仍走**默认路径**去读真表 ⇒ 测试恒过/恒败都不反映被测行为
        （实测就是这样假失败了一次）。"""
        import tempfile
        _orig = self.m._default_path
        self.addCleanup(setattr, self.m, "_default_path", _orig)
        self.m._default_path = lambda: os.path.join(tempfile.mkdtemp(), "nope.csv")
        self.m._META = {}
        self.m._META_AT = 0.0
        self.assertEqual(self.m.stock_label("603168"), "603168")


class TestManualCardsCarryIndustry(unittest.TestCase):
    """人工侧飞书卡片正文必须带行业（原来只有 `**代码** 名称`）。"""

    def _body(self, card):
        return card["card"]["elements"][0]["content"]

    def test_ma_break_card(self):
        from core.position_builder import build_ma_break_card
        card = build_ma_break_card([{"code": "603168", "name": "莎普爱思", "broke5": True,
                                     "ma5": 6.5, "dev5_pct": -1.2, "price": 6.24}], "2026-10-16")
        body = self._body(card)
        for token in ("莎普爱思", "603168", "医药生物"):
            self.assertIn(token, body, f"破5/10日线卡片缺 {token}")

    def test_ma_reclaim_card(self):
        from core.position_builder import build_ma_reclaim_card
        card = build_ma_reclaim_card([{"code": "600127", "name": "金健米业", "reclaim5": True,
                                       "ma5": 14.2, "dev5_pct": 0.3, "price": 14.23}], "2026-10-16")
        body = self._body(card)
        for token in ("金健米业", "600127", "粮油加工"):
            self.assertIn(token, body, f"站上5/10日线卡片缺 {token}")

    def test_hunter_ma5_card(self):
        from core.hunter_ma5_alert import build_card
        card = build_card([{"code": "601118", "name": "海南橡胶", "kind": "vrev",
                            "price": 6.82, "ma5": 6.7, "dev5_pct": 1.8}], top_n=5)
        self.assertIsNotNone(card, "卡片构建失败（事件字段对不上？）")
        body = self._body(card)
        for token in ("海南橡胶", "601118", "农林牧渔"):
            self.assertIn(token, body, f"猎手MA5卡片缺 {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
