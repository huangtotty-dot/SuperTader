# -*- coding: utf-8 -*-
"""超跌跳空确认 · 卖单迟确认（`_execute_sell_decisions` 尾部兜底）——
**2026-10-16 实测缺陷的回归守卫**。

## 现场

    14:50:01 卖出三笔（301373 sl5_deep / 605198 horizon_expired / 688003 horizon_expired）
    14:50:01.5 ~ 14:50:06.9  三笔全部成交（events pos_after=0）
    但 positions.json 仍挂 6 只 —— 成交了账本却不剔除。

## 根因

GM 仿真终端同步下单回报里 `order_id` 为空 ⇒ `_place_and_poll` 的轮询前置条件
`rep.get("order_id")` 为假 ⇒ **一轮都不轮询**；成交回报是异步的（0.5~6 秒后）⇒
status 停在 submitted、filled_qty=0 ⇒ 账本不剔除。

## 修复

`_execute_sell_decisions` 尾部：对仍停在 submitted 的卖单按「账户持仓变化」迟确认
——复用轮询参数等待 + 重取持仓快照，数量下降即视为成交并回填账本。
订单真没成交（挂在限价上）⇒ 账户持仓不变 ⇒ 保留账本条目。

运行：python -m unittest tests.phase3.test_osc_sell_late_fill -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "execution" / "auto") not in sys.path:
    sys.path.insert(0, str(_ROOT / "execution" / "auto"))

from oversold_gap_confirm import executor                    # noqa: E402


class _GwStub:
    """GM 仿真网关桩：下单返回 order_id=None（复刻当日现场），
    get_positions 按脚本返回持仓（模拟异步成交后持仓归零/减少）。"""

    def __init__(self, qty_script):
        """qty_script: list，第 i 次 get_positions 调用返回的该票持仓数量。"""
        self.qty_script = list(qty_script)
        self.calls = 0

    def place_order(self, code, side, qty, price):
        return {"order_id": None, "status": "submitted",
                "filled_qty": 0, "filled_price": None, "message": ""}

    def get_positions(self):
        idx = min(self.calls, len(self.qty_script) - 1)
        self.calls += 1
        q = self.qty_script[idx]
        return {"SZSE.301373": {"qty": q, "available": q, "cost": 108.81}} if q > 0 else {}


def _run(qty_script):
    open_pos = {"301373": {"qty": 300, "entry_price": 108.81,
                           "entry_date": "2026-10-14", "days_held": 2,
                           "dev20": -0.1142}}
    positions = {"301373": {"qty": 300, "available": 300, "cost": 108.81}}
    decisions = [{"code": "301373", "reason": "sl5_deep", "price": 99.68,
                  "entry_date": "2026-10-14", "entry_price": 108.81,
                  "dev20": -0.1142, "days_held": 2}]
    cfg = {"order_poll_rounds": 3, "order_poll_sleep_sec": 0}   # sleep 置 0，测试不等
    gw = _GwStub(qty_script)
    out = executor._execute_sell_decisions(gw, open_pos, positions, decisions,
                                           cfg, lambda s: None, lambda *a: None)
    return out, open_pos


class TestSellLateFillConfirm(unittest.TestCase):

    def test_fill_confirmed_by_position_drop(self):
        """当日现场复刻：下单回报 order_id 空、状态停 submitted，
        但下一轮快照持仓归零 ⇒ 迟确认成交，账本剔除。"""
        out, open_pos = _run([300, 300, 0])     # 前两次查询还有，第三次归零
        self.assertNotIn("301373", open_pos, "成交后账本必须剔除")
        self.assertEqual(out[0]["status"], "filled_by_position")
        self.assertEqual(out[0]["filled_qty"], 300)
        self.assertIn("迟确认", out[0]["message"])

    def test_unfilled_limit_order_kept(self):
        """订单真没成交（挂在限价上）：持仓一直不变 ⇒ 账本保留、状态保持 submitted。"""
        out, open_pos = _run([300, 300, 300, 300])
        self.assertIn("301373", open_pos, "未成交不得误删账本")
        self.assertEqual(open_pos["301373"]["qty"], 300)
        self.assertEqual(out[0]["status"], "submitted")
        self.assertEqual(out[0]["filled_qty"], 0)

    def test_partial_fill_reduces_book(self):
        """部分成交：持仓 300→100 ⇒ 迟确认 200 股，账本削到 100。"""
        out, open_pos = _run([300, 100])
        self.assertIn("301373", open_pos)
        self.assertEqual(open_pos["301373"]["qty"], 100)
        self.assertEqual(out[0]["status"], "filled_by_position")
        self.assertEqual(out[0]["filled_qty"], 200)

    def test_no_double_count_across_rounds(self):
        """跨轮滚动基准：第一轮部分成交、第二轮剩余成交 ⇒ 数量不累加重复。"""
        # 持仓 300 → 100（成交200）→ 0（再成交100）
        out, open_pos = _run([300, 100, 0])
        self.assertNotIn("301373", open_pos)
        self.assertEqual(out[0]["filled_qty"], 300, "两轮合计 300，不得重复计数")


if __name__ == "__main__":
    unittest.main()
