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
    spec = importlib.util.spec_from_file_location("_tg_pb_test", _ROOT / "t_gui.py")
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
        import src.holdings_repo as hr
        self._orig_auto = hr.load_auto

        fake_manual = {"600001": {"name": "甲", "qty": 1000, "pool": "manual"}}
        fake_wl = {"stocks": {
            "600001": {"name": "甲", "status": "monitoring", "pool": "manual"},
            "600002": {"name": "乙", "status": "monitoring", "pool": "manual"},
            "600003": {"name": "丙", "status": "monitoring", "pool": "auto"},
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
        import src.holdings_repo as hr
        hr.load_auto = self._orig_auto

    def test_held_codes_are_gone(self):
        rows = self.api._agg_position_builder("2026-01-01").get("rows") or []
        codes = {str(r.get("code", "")).split("_")[0] for r in rows}
        self.assertNotIn("600001", codes, "手动持仓股不该出现在建仓扫描里")
        self.assertNotIn("600003", codes, "自动盘持仓股也不该出现")
        self.assertIn("600002", codes, "无持仓的股池票应照常出现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
