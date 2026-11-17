# -*- coding: utf-8 -*-
"""建仓信号扫描**不得出现持仓股**（owner 2026-10-17）。

背景：这张表的可见性规则改过三次 ——
  2026-08-30 `auto_pool.is_manual` 隐藏 auto 标的 ⇒ 「添加成功却看不到」（600584）；
  2026-09-20 改成「持仓股 + 股池里的都显示」；
  **2026-10-17 改为「持仓股一律不出现」** —— 已有底仓的票属于「持仓体检」，
  列在"建仓候选"里会让人误以为还要再建一次。

本测试钉住：手动盘 qty>0 的票不出现；自动盘 qty>0 的票也不出现；无持仓的正常票照常出现。

运行：python -m unittest tests.phase3.test_pb_excludes_holdings -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _api():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tg_pb_test", _ROOT / "gui.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod, mod.Api()


class TestPbExcludesHoldings(unittest.TestCase):
    def setUp(self):
        self.mod, self.api = _api()
        self._orig_load_json = self.mod._load_json
        import core.holdings_repo as hr
        self._orig_auto = hr.load_auto

        fake_manual = {"600001": {"name": "甲", "qty": 1000, "pool": "manual"},
                       # 2026-11-17：手动「+ 添加」的持仓票（有 manual_added_at）⇒ 放行
                       "600004": {"name": "丁", "qty": 300, "pool": "manual"}}
        fake_wl = {"stocks": {
            "600001": {"name": "甲", "status": "monitoring", "pool": "manual"},
            "600002": {"name": "乙", "status": "monitoring", "pool": "manual"},
            "600003": {"name": "丙", "status": "monitoring", "pool": "auto"},
            "600004": {"name": "丁", "status": "monitoring", "pool": "manual",
                       "manual_added_at": "2026-11-17 10:00:00"},
        }}

        def _lj(p, d=None):
            s = str(p)
            if s == str(self.mod.HOLDINGS_MANUAL):
                return fake_manual
            if s.endswith("watchlist_buy.json"):
                return fake_wl
            return d if d is not None else {}

        self.mod._load_json = _lj
        # 自动盘持仓：丙 qty>0 ⇒ 也不该出现
        hr.load_auto = lambda: {"600003": {"name": "丙", "qty": 500, "pool": "auto"}}

    def tearDown(self):
        self.mod._load_json = self._orig_load_json
        import core.holdings_repo as hr
        hr.load_auto = self._orig_auto

    def test_held_codes_are_gone(self):
        rows = self.api._agg_position_builder("2026-01-01").get("rows") or []
        codes = {str(r.get("code", "")).split("_")[0] for r in rows}
        self.assertNotIn("600001", codes, "手动持仓股不该出现在建仓扫描里")
        self.assertNotIn("600003", codes, "自动盘持仓股也不该出现")
        self.assertIn("600002", codes, "无持仓的股池票应照常出现")

    def test_manual_added_holding_is_visible(self):
        """2026-11-17 owner：「手动加的持仓票也显示」（002690 实报）。

        显式「+ 添加」过的持仓票放行，行上带 held_qty 供前端画「持仓N股」徽章；
        没显式加过的持仓票（600001）仍按 10-17 规则隐藏。"""
        rows = self.api._agg_position_builder("2026-01-01").get("rows") or []
        hit = [r for r in rows if str(r.get("code", "")).split("_")[0] == "600004"]
        self.assertTrue(hit, "手动添加的持仓票应出现在建仓扫描里")
        r = hit[0]
        self.assertTrue(r.get("in_holdings"))
        self.assertEqual(r.get("held_qty"), 300)
        self.assertTrue(r.get("manual_added"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
