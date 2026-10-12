# -*- coding: utf-8 -*-
"""策略框架 P0+P1：**一票一策略硬锁** + 通用下单通道（2026-10-12）。**全离线**。

背景（owner 2026-10-12）：「后面有可能会不断拓展策略 ⇒ 策略需要封装增强模块化设计」，
并拍板三条口径：**一票一策略 + 硬锁** / 各自声明额度 + 先到先用 / 先做 P0+P1。
本文件守 P0（归属）与 P1（通用通道）这两块，**不需要 gm 终端**（依赖全注入）。

## 守什么

  A 归属判定：账本持有 ⇒ 归它；账户持有但无账本认领 ⇒ LEGACY 对所有人锁；
    账户无持仓/无账本 ⇒ 可买；override 文件优先（人工豁免通道）。
  B 硬锁行为：被别的策略持有 ⇒ 网关**不下单**（`order_volume` 一次都不被调用）、
    **不落 order 事件**（否则孤儿闸会误以为本策略今天下过单）、返回 `rejected` + 留痕。
  C 定序铁律：`write_order` 必须在 `order_volume` **之前**（孤儿闸登记；6a96829c 前科）。
  D 等价性：可买路径下网关的行为与原 `_OscGmGateway` 一致（市价单 / 参数前缀 /
    同步拒单码元组 / 返回结构）。
  E 归属只来自账本：`owns()` 为真的 code 才认领，**不用**新增状态文件（防漂移）。

运行：python -m unittest tests.phase3.test_framework_ownership_gateway -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "execution", "auto")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from framework.gateway import GmGateway                       # noqa: E402
from framework.ownership import LEGACY, Ownership             # noqa: E402
from framework.services import GmServices                     # noqa: E402


# ── 假件 ────────────────────────────────────────────────────
class FakeStrategy(object):
    def __init__(self, sid, codes=()):
        self.id = sid
        self.codes = set(codes)

    def owns(self, code):
        return str(code) in self.codes

    def owned_codes(self):
        return sorted(self.codes)


class FakeGm(object):
    """记录调用顺序的假 gm.api 命名空间。"""
    OrderSide_Buy, OrderSide_Sell = 1, 2
    OrderType_Market, OrderType_Limit = 1, 2
    PositionEffect_Open, PositionEffect_Close = 1, 2
    ADJUST_PREV = 1

    def __init__(self, orders=None, positions=None, cash=1000000.0):
        self.calls = []
        self._orders = orders if orders is not None else [{"id": "o1", "status": 0}]
        self._positions = positions or {}
        self._cash = cash

    def order_volume(self, **kw):
        self.calls.append(("order_volume", kw))
        return self._orders

    def get_orders(self, **kw):
        self.calls.append(("get_orders", kw))
        return []

    def current(self, **kw):
        return [{"open": 10.0}]

    def history_n(self, **kw):
        return []


class FakeAccount(object):
    def __init__(self, positions=None, cash=1000000.0):
        self._p = positions or {}
        self._cash = cash

    def cash(self):
        return {"available": self._cash}

    def positions(self, symbol=None, side=None):
        if symbol:
            sym = symbol
            for s, v in self._p.items():
                if s == sym:
                    return [{"symbol": s, "volume": v["qty"], "available": v.get("available", 0),
                             "vwap": v.get("cost", 0)}]
            return []
        return [{"symbol": s, "volume": v["qty"], "available": v.get("available", 0),
                 "vwap": v.get("cost", 0)} for s, v in self._p.items()]


class FakeCtx(object):
    def __init__(self, account):
        # ⚠️ gm SDK 里 `context.account` 是**可调用访问器**（不是对象）⇒ 这里保持一致，
        # 否则网关的 get_positions 会静默走异常分支返空（本测试就会假绿）。
        self.account = (lambda: account)


def _services(gm, ownership, params=None, events=None, risks=None, audits=None):
    params = params or {}
    order = []

    def _write_order(t, code, side, qty, px, order_id="", order_type="MKT"):
        order.append((code, side, qty, px, order_type))
        if events is not None:
            events.append({"event": "order", "code": code, "side": side, "qty": qty,
                           "price": px, "order_type": order_type})

    def _mk(ctx, code, sym, side, qty, px, orders_):
        order.append(("recon", code, side, qty))

    return GmServices(
        gm=gm, sdk_call=lambda desc, fn, *a, **k: fn(*a, **k),
        write_order=_write_order, mark_pending_recon=_mk,
        code_to_gm=lambda c: ("SHSE." if c[:1] in "569" else "SZSE.") + c,
        raw_code=lambda s: s.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", ""),
        param=lambda k, d=None: params.get(k, d),
        clock=lambda ctx: getattr(ctx, "now", None),
        reject_statuses=(4, 5, 6, 8, 12), ownership=ownership,
        risk=(lambda *a, **k: risks.append((a, k))) if risks is not None else None,
        audit=(lambda r: audits.append(r)) if audits is not None else None,
    ), order


class TestOwnershipJudgement(unittest.TestCase):
    """A/E：归属判据。"""

    def setUp(self):
        self.osc = FakeStrategy("osc", ["600000"])
        self.other = FakeStrategy("beta", ["600001"])
        self.own = Ownership(strategies=[self.osc, self.other])

    def test_book_claim_wins(self):
        self.assertEqual(self.own.owner_of("600000"), "osc")
        self.assertEqual(self.own.owner_of("600001"), "beta")
        self.assertIsNone(self.own.owner_of("600002"))

    def test_owner_may_open_own_code(self):
        self.assertEqual(self.own.can_open("600000", "osc", held_qty=500), (True, ""))

    def test_other_owner_is_hard_locked(self):
        ok, why = self.own.can_open("600001", "osc", held_qty=500)
        self.assertFalse(ok, "被别的策略持有必须拒（一票一策略）")
        self.assertIn("held_by:beta", why)

    def test_unclaimed_account_position_is_locked(self):
        """账户持有但无账本认领（旧镜像遗留/终端手工单）⇒ 对所有人锁。"""
        ok, why = self.own.can_open("600002", "osc", held_qty=300)
        self.assertFalse(ok)
        self.assertIn("unclaimed_account_position", why)

    def test_free_code_may_open(self):
        self.assertEqual(self.own.can_open("600003", "osc", held_qty=0), (True, ""))

    def test_owned_but_sold_out_account_is_still_owned(self):
        """账本持有但账户显示 0（刚卖、回调未到）⇒ 仍归账本，别人照样买不进。"""
        ok, why = self.own.can_open("600000", "beta", held_qty=0)
        self.assertFalse(ok)
        self.assertIn("held_by:osc", why)

    def test_override_beats_book(self):
        """override 是人工豁免通道：文件优先于账本（认领历史持仓 / 强制锁定）。"""
        d = tempfile.mkdtemp(prefix="own_")
        fp = os.path.join(d, "auto_ownership.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"overrides": {"600002": "osc"}}, f)
        own = Ownership(strategies=[self.osc], override_path=fp)
        self.assertEqual(own.owner_of("600002"), "osc")
        self.assertEqual(own.can_open("600002", "osc", held_qty=300), (True, ""))
        # 反向：把 osc 自己的票强制锁成 legacy
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"overrides": {"600000": LEGACY}}, f)
        own.reload_overrides()
        self.assertFalse(own.can_open("600000", "osc", held_qty=100)[0])

    def test_broken_override_file_never_raises(self):
        d = tempfile.mkdtemp(prefix="own_")
        fp = os.path.join(d, "auto_ownership.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("{not json")
        own = Ownership(strategies=[self.osc], override_path=fp)
        self.assertEqual(own.overrides, {})
        self.assertTrue(own.load_error)
        self.assertEqual(own.can_open("600000", "osc", held_qty=0), (True, ""))

    def test_no_state_file_is_written(self):
        """归属是**算出来的**：判据只读账本/override，不新增需要维护的状态文件。"""
        with open(os.path.join(_ROOT, "execution", "auto", "framework", "ownership.py"),
                  encoding="utf-8") as f:
            src = f.read()
        for bad in ("def claim(", "def release_if_flat(", "def seed("):
            self.assertNotIn(bad, src, f"不该有需要成交侧维护的写入面：{bad}")


class TestGatewayHardLock(unittest.TestCase):
    """B/C/D：网关硬锁、定序、可买路径的等价性。"""

    def _gw(self, ownership, **kw):
        acct_pos = kw.pop("acct_positions", None) or {}
        gm = FakeGm(**kw)
        acc = FakeAccount(positions=acct_pos, cash=1000000.0)
        ev, rk, au = [], [], []
        sv, order = _services(gm, ownership, events=ev, risks=rk, audits=au,
                              params={"osc_limit_slippage_bp": 0})
        gw = GmGateway(FakeCtx(acc), "osc", sv, log=lambda *a: None)
        return gw, gm, ev, rk, au, order

    def test_locked_buy_never_reaches_broker(self):
        own = Ownership(strategies=[FakeStrategy("beta", ["600001"])])
        gw, gm, ev, rk, au, order = self._gw(own)
        rep = gw.place_order("600001", "BUY", 1000, 10.0)
        self.assertEqual(rep["status"], "rejected")
        self.assertIn("ownership_lock", rep["message"])
        self.assertEqual([c for c in gm.calls if c[0] == "order_volume"], [],
                         "被锁的单绝不能送到柜台")
        self.assertEqual([o for o in order if o[0] != "recon"], [],
                         "被锁的单绝不能落 order 事件（否则孤儿闸误判）")
        self.assertTrue(any("strategy_lock_conflict" in str(x) for x in rk), "必须落 risk 留痕")
        self.assertTrue(any(r.get("event") == "strategy_lock_conflict" for r in au))

    def test_unclaimed_account_position_blocks_buy(self):
        own = Ownership(strategies=[])
        gw, gm, ev, rk, au, order = self._gw(
            own, acct_positions={"SHSE.600002": {"qty": 500, "available": 500, "cost": 9}})
        rep = gw.place_order("600002", "BUY", 1000, 10.0)
        self.assertEqual(rep["status"], "rejected")
        self.assertIn("unclaimed_account_position", rep["message"])
        self.assertEqual([c for c in gm.calls if c[0] == "order_volume"], [])

    def test_free_buy_orders_and_writes_order_first(self):
        own = Ownership(strategies=[FakeStrategy("osc", [])])
        gw, gm, ev, rk, au, order = self._gw(own)
        rep = gw.place_order("600003", "BUY", 1000, 10.0)
        self.assertEqual(rep["status"], "submitted")
        self.assertEqual(rep["order_id"], "o1")
        kinds = [c[0] if isinstance(c, tuple) and c[0] == "order_volume" else c
                 for c in gm.calls]
        self.assertIn("order_volume", kinds, "可买的单必须真的下单")
        # C 定序：order 事件登记在「recon 登记」之前，且 order_volume 之前
        self.assertEqual(order[0][0], "600003")
        self.assertEqual(order[0][4], "osc", "order_type 必须是策略 id（孤儿闸按它判归属）")
        self.assertEqual(order[1][0], "recon")

    def test_owners_own_code_still_buyable(self):
        """自己的票：账本持有 + 账户也持有 ⇒ 不锁（同一策略加仓/复核不受影响）。"""
        own = Ownership(strategies=[FakeStrategy("osc", ["600004"])])
        gw, gm, ev, rk, au, order = self._gw(
            own, acct_positions={"SHSE.600004": {"qty": 500, "available": 0, "cost": 9}})
        rep = gw.place_order("600004", "BUY", 1000, 10.0)
        self.assertEqual(rep["status"], "submitted")

    def test_sell_is_never_locked(self):
        """卖侧不设锁（各策略账本本就是卖侧真源）；锁只拦开仓。"""
        own = Ownership(strategies=[FakeStrategy("beta", ["600005"])])
        gw, gm, ev, rk, au, order = self._gw(own)
        rep = gw.place_order("600005", "SELL", 500, 10.0)
        self.assertEqual(rep["status"], "submitted")

    def test_sync_reject_statuses_unchanged(self):
        """D 等价性：同步回报里的拒单码元组口径不变（4/5/6/8/12）。"""
        own = Ownership(strategies=[])
        gw, gm, ev, rk, au, order = self._gw(own, orders=[{"id": "o9", "status": 8}])
        rep = gw.place_order("600006", "BUY", 100, 10.0)
        self.assertEqual(rep["status"], "rejected")
        self.assertEqual(rep["order_id"], "o9")
        self.assertIn("status=8", rep["message"])

    def test_param_prefix_is_strategy_id(self):
        """参数前缀 = 策略 id（多策略各自一套，不再硬编码 osc_*）。"""
        own = Ownership(strategies=[])
        gm = FakeGm()
        sv, order = _services(gm, own, params={"beta_limit_slippage_bp": 20})
        gw = GmGateway(FakeCtx(FakeAccount()), "beta", sv, log=lambda *a: None)
        gw.place_order("600007", "BUY", 100, 10.0)
        kw = [c[1] for c in gm.calls if c[0] == "order_volume"][0]
        self.assertEqual(kw["order_type"], FakeGm.OrderType_Limit)
        self.assertAlmostEqual(kw["price"], round(10.0 * (1 + 20 / 1e4), 2))

    def test_market_order_when_no_slippage_param(self):
        own = Ownership(strategies=[])
        gw, gm, ev, rk, au, order = self._gw(own)      # osc_limit_slippage_bp=0
        gw.place_order("600008", "BUY", 100, 10.0)
        kw = [c[1] for c in gm.calls if c[0] == "order_volume"][0]
        self.assertEqual(kw["order_type"], FakeGm.OrderType_Market)
        self.assertNotIn("price", kw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
