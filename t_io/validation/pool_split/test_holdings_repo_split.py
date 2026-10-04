# -*- coding: utf-8 -*-
"""src/holdings_repo.py 双文件拆分单元测试（2026-10-04）。

验证手动侧/自动侧两份真源的读写隔离、共享字段传播、pool 迁移等不变量。
全部 tempdir + 常量 monkeypatch，不碰真实 t_io/state。

运行：python t_io/validation/pool_split/test_holdings_repo_split.py
"""
import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import src.holdings_repo as repo  # noqa: E402


def _entry(pool, qty=0, base=0, cost=0.0, name="X", gm="", pre=0.0, type="stock"):
    return {"name": name, "gm_symbol": gm, "type": type, "pool": pool,
            "qty": qty, "cost": cost, "base": base, "pre_close": pre}


class TestHoldingsRepoSplit(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = self._td.name
        self._o = (repo.MANUAL_FILE, repo.AUTO_FILE, repo._LEGACY_FILE)
        repo.MANUAL_FILE = os.path.join(td, "holdings_manual.json")
        repo.AUTO_FILE = os.path.join(td, "holdings_auto.json")
        repo._LEGACY_FILE = os.path.join(td, "holdings.json")

    def tearDown(self):
        repo.MANUAL_FILE, repo.AUTO_FILE, repo._LEGACY_FILE = self._o
        self._td.cleanup()

    def _seed(self):
        """both 码 000001 两份各一条；auto-only 600001；manual-only 300001。"""
        repo.save_manual({"000001": _entry("both", qty=100, base=200, name="both票", gm="SZSE.000001"),
                          "300001": _entry("manual", qty=50, name="手动票", gm="SZSE.300001")},
                         actor="t", reason="seed")
        repo.save_auto({"000001": _entry("both", qty=100, base=200, name="both票", gm="SZSE.000001"),
                        "600001": _entry("auto", qty=0, base=300, name="自动票", gm="SHSE.600001")},
                       actor="t", reason="seed")

    def test_sides_isolated(self):
        self._seed()
        self.assertIn("300001", repo.load_manual())
        self.assertNotIn("300001", repo.load_auto())
        self.assertIn("600001", repo.load_auto())
        self.assertNotIn("600001", repo.load_manual())

    def test_shared_field_propagation(self):
        self._seed()
        # 手动侧改身份 name → 自动副本同步；qty 不同步
        e = dict(repo.load_manual()["000001"])
        e["name"] = "改名票"
        e["qty"] = 777
        repo.save_manual({"000001": e}, actor="t", reason="rename")
        auto = repo.load_auto()["000001"]
        self.assertEqual(auto["name"], "改名票", "身份字段必须传播到自动副本")
        self.assertNotEqual(auto["qty"], 777, "qty 为分侧所有，不得传播")

    def test_save_pre_close_both_copies(self):
        self._seed()
        n = repo.save_pre_close({"000001": 12.34, "600001": 9.99}, actor="t", reason="eod")
        self.assertEqual(n, 2)
        self.assertEqual(repo.load_manual()["000001"]["pre_close"], 12.34)
        self.assertEqual(repo.load_auto()["000001"]["pre_close"], 12.34)
        self.assertEqual(repo.load_auto()["600001"]["pre_close"], 9.99)

    def test_load_union_merge_rule(self):
        self._seed()
        # 手动 qty=100，自动 qty 改成 999 → union 取手动 qty，base 取自动
        a = dict(repo.load_auto()["000001"]); a["qty"] = 999
        repo.save_auto({"000001": a}, actor="t", reason="diverge")
        u = repo.load_union()["000001"]
        self.assertEqual(u["qty"], 100, "union 的 qty 取手动侧（实盘口径）")
        self.assertEqual(u["base"], 200)

    def test_set_pool_transitions(self):
        self._seed()
        repo.set_pool("300001", "both", actor="t", reason="to both")
        self.assertIn("300001", repo.load_auto())
        self.assertEqual(repo.load_auto()["300001"]["pool"], "both")
        repo.set_pool("300001", "auto", actor="t", reason="to auto")
        self.assertNotIn("300001", repo.load_manual(), "改为 auto 后应移出手动文件")
        self.assertEqual(repo.load_auto()["300001"]["pool"], "auto")

    def test_delete_side_auto_keeps_manual(self):
        self._seed()
        repo.delete_entry("000001", side="auto", actor="t", reason="rm auto")
        self.assertNotIn("000001", repo.load_auto())
        self.assertIn("000001", repo.load_manual(), "both 码删自动侧应保留手动副本")
        self.assertEqual(repo.load_manual()["000001"]["pool"], "manual", "降为单侧归属")

    def test_upsert_auto_entry_preserves_and_moves(self):
        self._seed()
        # 300001 原为 manual：upsert_auto 应移出手动、进自动，preserve 既有 qty/cost
        repo.upsert_auto_entry("300001", name="手动票", gm_symbol="SZSE.300001",
                               type="stock", base=100, actor="t", reason="u")
        self.assertNotIn("300001", repo.load_manual())
        e = repo.load_auto()["300001"]
        self.assertEqual(e["pool"], "auto")
        self.assertEqual(e["qty"], 50, "preserve 既有 qty")
        self.assertEqual(e["base"], 100)

    def test_legacy_fallback(self):
        # 旧 holdings.json 在、新文件缺 → 读侧按 pool 派生两侧
        legacy = {"000001": _entry("both", qty=1), "600001": _entry("auto"),
                  "300001": _entry("manual")}
        with open(repo._LEGACY_FILE, "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        self.assertEqual(set(repo.load_manual()), {"000001", "300001"})
        self.assertEqual(set(repo.load_auto()), {"000001", "600001"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
