# -*- coding: utf-8 -*-
"""多窗口看盘「两市成交额」小卡的后端（2026-11-17，全离线）。

owner：「将该显示框嵌入到多窗口看盘」。小卡口径沿用原桌面浮窗（已删）：
  · 分子 = 腾讯指数实时快照三腿（10 秒级，含正在走的 5min bar）；
  · 分母 = 上一交易日**同一时刻**累计（`prev_cum` 按秒插值）。

这里钉住三条契约：
  A. 分子=三腿合计（不是卡片那条 5min 的 `amount`）；
  B. 分母走插值（不是 5min 边界），且 `src` 如实标注；
  C. 缺曲线时退回 `amount_prev_same_hm`；快照全缺 ⇒ available=False（界面显示原因，不抛）。

运行：python -m unittest tests.phase3.test_turnover_live_card -v
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gui  # noqa: E402

# 11-16 逐 5min 累计（元）：10:00 那根收完 4878.7 亿、10:05 那根收完 5355.9 亿
PREV_CUM = [["09:35", 738.1e8], ["10:00", 4878.7e8], ["10:05", 5355.9e8], ["15:00", 14943.3e8]]
PAYLOAD = {"available": True, "amount": 4776.8e8, "amount_prev": 5355.9e8,
           "amount_prev_same_hm": 5355.9e8, "prev_cum": PREV_CUM,
           "day": "2026-11-17", "prev_trading_day": "2026-11-16",
           "proj_frac": 0.36, "basis": "同期"}
LEGS = {"sh000001": 2213.1e8, "sz399106": 2527.0e8, "bj899050": 36.7e8}


class TestTurnoverLiveCard(unittest.TestCase):
    def _call(self, payload=None, legs=None, now=None):
        api = gui.Api()
        import datetime as _dt
        fixed = now or _dt.datetime(2026, 11, 17, 10, 5, 0)
        with mock.patch.object(gui.Api, "load_market_turnover",
                               return_value=(PAYLOAD if payload is None else payload)), \
                mock.patch.object(gui, "_tx_index_amount", return_value=(LEGS if legs is None else legs)), \
                mock.patch.object(gui, "datetime") as _m:
            _m.now.return_value = fixed
            return api.load_turnover_live()

    def test_molecule_is_three_legs(self):
        r = self._call()
        self.assertTrue(r["available"])
        self.assertAlmostEqual(r["amount"] / 1e8, 4776.8, places=1, msg="三腿合计")
        self.assertTrue(r["bse_included"])

    def test_base_uses_interpolation(self):
        r = self._call()
        self.assertEqual(r["src"], "插值")
        self.assertAlmostEqual(r["base"] / 1e8, 5355.9, places=0)
        self.assertAlmostEqual(r["delta"] / 1e8, -579.1, places=0)
        self.assertAlmostEqual(r["pct"], -10.81, places=1)

    def test_falls_back_to_5min_boundary(self):
        p = dict(PAYLOAD)
        p["prev_cum"] = []
        p["amount_prev_same_hm"] = 4878.7e8
        r = self._call(payload=p)
        self.assertEqual(r["src"], "5min边界")
        self.assertAlmostEqual(r["base"] / 1e8, 4878.7, places=1)

    def test_snapshot_missing_is_reported(self):
        r = self._call(legs={})
        self.assertFalse(r["available"])
        self.assertIn("不可用", r.get("error") or "")

    def test_projected_uses_proj_frac(self):
        r = self._call()
        self.assertAlmostEqual(r["projected"] / 1e8, 4776.8 / 0.36, places=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
