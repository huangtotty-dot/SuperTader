# -*- coding: utf-8 -*-
"""超跌跳空确认 · 账本对账（`executor.reconcile_book`）—— **上线首日实测缺陷的回归守卫**（2026-10-14）。

## 现场（不是假想，是当天真实发生）

    候选 4 只 → gap 降序下 3 笔：
      600097  gap +10.03%  ← **涨停** ⇒ 委托一直挂着、一股没成交
      000823  gap  +9.99%  ← **涨停** ⇒ 同上
      301373  gap  +7.95%  ← 未涨停 ⇒ 成交 ✓
    603186          skipped（现金不足）

结果：**账本 5 只 / 账户 3 只**。

**根因**：`register_buys_to_book` 在买单 `status="submitted"`（挂单未成交）时就入账本，
而数量取的是 `filled_qty or qty_placed` ⇒ **一股没成交也按计划数量全额入账**。

**后果（若不修）**：14:50 卖侧读账本得到 5 只 ⇒ 对那两只下**无券可卖**的委托 ⇒ 刷拒单 +
`days_held` 被推进 + 5 日到期反复触发；账本与账户不一致还会连带污染体检/复盘。

## 为什么不能简单改成"只认 filled_qty"

宿主 `on_order_status`（成交回调）**只写成交流水、不写账本** ⇒ 账本登记**只在 10:00 那条路**里。
若改成只认成交，**后来成交的挂单就永远进不了账本** ⇒ 变成无人认领持仓 ⇒ 被归属锁锁死。
⇒ 正解是「**挂单即入账 + 每次卖前按账户夹紧**」：没成交就掉、成交了就留。

运行：python -m unittest tests.phase3.test_osc_book_reconcile -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "execution" / "auto") not in sys.path:
    sys.path.insert(0, str(_ROOT / "execution" / "auto"))

from oversold_gap_confirm import executor                    # noqa: E402


class TestReconcileBook(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="osc_book_")
        executor.save_book({"open": {
            # 账户真有的三只
            "605198": {"qty": 1800, "entry_price": 62.043, "entry_date": "2026-10-12", "days_held": 0},
            "688003": {"qty": 1700, "entry_price": 65.662, "entry_date": "2026-10-12", "days_held": 0},
            "301373": {"qty": 300, "entry_price": 108.81, "entry_date": "2026-10-14", "days_held": 0},
            # 账户**没有**的两只（涨停买不到、委托还挂着）
            "600097": {"qty": 9800, "entry_price": 10.2, "entry_date": "2026-10-14", "days_held": 0},
            "000823": {"qty": 4000, "entry_price": 24.55, "entry_date": "2026-10-14", "days_held": 0},
        }}, self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _open(self):
        return executor.load_book(self.d).get("open") or {}

    def test_drops_phantom_entries(self):
        """**上线首日那个现场**：账户只有 3 只 ⇒ 账本那两只挂单未成交的必须掉。"""
        n = executor.reconcile_book(self.d, {"605198": 1800, "688003": 1700, "301373": 300})
        self.assertEqual(n, 2, "应剔除 2 笔（600097 / 000823）")
        self.assertEqual(set(self._open()), {"605198", "688003", "301373"})

    def test_clamps_partial_fill(self):
        """部分成交：账本数量按账户夹紧（不能高于账户，否则卖侧会超卖）。"""
        executor.reconcile_book(self.d, {"605198": 1800, "688003": 1700,
                                         "301373": 300, "600097": 3000, "000823": 4000})
        self.assertEqual(self._open()["600097"]["qty"], 3000, "计划 9800、实际 3000 ⇒ 夹到 3000")

    def test_keeps_late_fill(self):
        """**关键**：挂单后来成交了 ⇒ 账本必须留着（不能因为"没成交"就删）。

        这正是「不能改成只认 filled_qty」的理由 —— 成交回调不写账本。
        """
        n = executor.reconcile_book(self.d, {"605198": 1800, "688003": 1700, "301373": 300,
                                             "600097": 9800, "000823": 4000})
        self.assertEqual(n, 0, "全部对得上 ⇒ 不该改任何东西")
        self.assertEqual(set(self._open()), {"605198", "688003", "301373", "600097", "000823"})

    def test_noop_when_consistent(self):
        before = json.dumps(self._open(), sort_keys=True, ensure_ascii=False)
        executor.reconcile_book(self.d, {"605198": 1800, "688003": 1700, "301373": 300,
                                         "600097": 9800, "000823": 4000})
        self.assertEqual(before, json.dumps(self._open(), sort_keys=True, ensure_ascii=False),
                         "一致时不得改动账本（避免无谓写盘）")

    def test_account_extra_position_untouched(self):
        """账户多出**非本策略**的票：对账只管账本里的条目，不许往账本里加东西。"""
        executor.reconcile_book(self.d, {"605198": 1800, "688003": 1700, "301373": 300,
                                         "999999": 500})
        self.assertNotIn("999999", self._open(), "不许把账户里别的票写进本策略账本")

    def test_empty_account_clears_all(self):
        n = executor.reconcile_book(self.d, {})
        self.assertEqual(n, 5)
        self.assertEqual(self._open(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
