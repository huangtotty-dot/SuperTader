# -*- coding: utf-8 -*-
"""飞书「突破预警」注明韭研板块（2026-11-02 owner）。

owner 截图：「飞书推送的这个信息要注明来自韭研概念的哪个板块」。

守三条：
  ① `core.jiuyan_tags` 的主板块必须与 `t_gui._jiuyan_board()` **逐码一致** ——
     它是同一口径的**第三份实现**（t_gui 不能 import 策略包、main.py 不能 import t_gui），
     副本可以存在，但必须有机械闸（同 `test_market_beacon.TestBoardMapSingleSource`）；
  ② 概念合并口径与 `t_gui._jiuyan_concepts` 一致（编号字段优先、`|` 拆分、去重保序）；
  ③ `main._push_price_alert` 真的把标签拼进飞书文案（读不到也照发，只少这一截）。

运行：python -m unittest tests.phase3.test_price_alert_jiuyan_label -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import t_gui                                              # noqa: E402
from core import jiuyan_tags as jt                       # noqa: E402

PY_MAIN = (_ROOT / "main.py").read_text(encoding="utf-8")


class TestBoardSingleSource(unittest.TestCase):
    """第三份实现必须与 t_gui 的主板块逐码一致（漂移即红）。"""

    def test_逐码一致(self):
        ref = t_gui._jiuyan_board()
        mine = jt._build()
        self.assertGreater(len(ref), 1000, "韭研表没读到 —— 测试数据缺失")
        self.assertEqual(set(ref), set(mine), "代码集合不一致")
        diff = {c: (ref.get(c), (mine.get(c) or {}).get("board"))
                for c in ref if ref.get(c) != (mine.get(c) or {}).get("board")}
        self.assertFalse(diff, f"主板块口径漂移（前 5 个）：{list(diff.items())[:5]}")


class TestConceptMerge(unittest.TestCase):
    """概念合并口径：编号字段优先、list/`|` 都展开、去重保序。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_fp, self._old_cache = jt._WL_FP, dict(jt._CACHE)
        jt._WL_FP = Path(self._tmp.name) / "watchlist_jiuyan.json"
        jt._WL_FP.write_text(json.dumps({
            "603179": {"jiuyan_category": "汽车", "jiuyan_category1": "机器人",
                       "jiuyan_concept": "零部件", "jiuyan_concept1": "结构件"},
            "000538": {"jiuyan_category": "中药", "jiuyan_concept": ["种植", "民族药|种植"]},
            "830001": {"jiuyan_category": "北交所"},
            "600000": {"jiuyan_category": "创新药", "jiuyan_concept": "临床CRO"},
        }, ensure_ascii=False), encoding="utf-8")
        jt._CACHE.update({"key": None, "map": {}})

    def tearDown(self):
        jt._WL_FP = self._old_fp
        jt._CACHE.clear()
        jt._CACHE.update(self._old_cache)
        self._tmp.cleanup()

    def test_主板块取第一个有值的编号字段(self):
        self.assertEqual(jt.board_of("603179"), "机器人", "category1 要盖过 category")
        self.assertEqual(jt.board_of("000538"), "医药", "MERGES：中药 → 医药")
        self.assertEqual(jt.board_of("830001"), "", "北交所是机构标签，不算题材板块")
        self.assertEqual(jt.board_of("600000"), "医药", "MERGES：创新药 → 医药")

    def test_概念去重保序且编号优先(self):
        # 有编号字段就不再读旧 `jiuyan_concept`（与 t_gui._jiuyan_concepts 同口径）
        self.assertEqual(jt.concepts_of("603179"), ["结构件"])
        self.assertEqual(jt.concepts_of("000538"), ["种植", "民族药"])
        self.assertEqual(jt.concepts_of("600000"), ["临床CRO"], "没有编号字段才回退旧字段")
        self.assertEqual(jt.concepts_of("830001"), [])
        self.assertEqual(jt.label_of("603179", max_concepts=1)["concepts"], ["结构件"])

    def test_读不到表就返回空标签(self):
        jt._WL_FP = Path(self._tmp.name) / "missing.json"
        jt._CACHE.update({"key": None, "map": {}})
        self.assertEqual(jt.label_of("603179"), {"board": "", "concepts": []})


class TestPushIncludesLabel(unittest.TestCase):
    def test_推飞书文案带板块标签(self):
        i = PY_MAIN.index("def _push_price_alert")
        seg = PY_MAIN[i:PY_MAIN.index("\n\n\n", i)]
        self.assertIn("from core.jiuyan_tags import label_of", seg)
        self.assertIn("🏷", seg)
        self.assertIn("_lab['board']", seg)

    def test_标签失败不影响推送(self):
        """读不到 watchlist ⇒ 只少标签，消息照发（label_of 内部 fail-open）。"""
        i = PY_MAIN.index("def _push_price_alert")
        seg = PY_MAIN[i:PY_MAIN.index("\n\n\n", i)]
        self.assertIn("if _lab.get(\"board\")", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
