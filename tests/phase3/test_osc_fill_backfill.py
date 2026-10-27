# -*- coding: utf-8 -*-
"""超跌跳空确认 · fill 事件驱动回填（`fillbackfill.apply_fill`）——
**2026-10-19 复盘缺陷的回归守卫**（任务B）。

## 现场

  · 卖侧：executor 迟确认兜底窗口（≈6s）外成交（10-19 601118 成交于 10:00:07.386，
    窗口 10:00:06 已关）⇒ positions.json 不剔除，连续两天手工 RECONCILE。
  · 买侧：fills_2026-10-16/10-19 所有买单 filled_qty=0、order_id=""（GM 仿真同步
    回报恒空/恒 0，成交是异步事件）⇒ 601118 账本 entry_price=6.53（委托价）
    vs 真实成交 6.59。

## 覆盖五场景

    卖全平 / 卖部分 / 买新建（register 已入账→原地修正价量）/ 买加仓合并 / 重复事件幂等
    （外加：孤儿卖跳过、买侧已确认留痕的语义守卫）

运行：python -m unittest tests.phase3.test_osc_fill_backfill -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "execution" / "auto") not in sys.path:
    sys.path.insert(0, str(_ROOT / "execution" / "auto"))

from oversold_gap_confirm import executor, fillbackfill        # noqa: E402

_DATE = "2026-10-19"


def _write_json(fp, payload):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _buy_rec(code, qty, price, status="submitted", filled_qty=0, filled_price=None):
    return {"code": code, "symbol": executor.gm_symbol_of(code), "side": "BUY",
            "gap": 0.05, "price": price, "qty_target": qty, "qty_placed": qty,
            "truncated": False, "defer_reason": None, "status": status,
            "filled_qty": filled_qty, "filled_price": filled_price,
            "fee_est": 5.0, "message": "", "order_id": "",
            "qty_remaining": qty - filled_qty}


class _CaseBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self, *a):
        self.logs.append(" ".join(str(x) for x in a))

    def _book(self):
        return executor.load_book(self.dir)

    def _set_book(self, book):
        _write_json(os.path.join(self.dir, "positions.json"), book)

    def _set_fills(self, buys, date=_DATE):
        _write_json(os.path.join(self.dir, "fills_%s.json" % date),
                    {"date": date, "mode": "live", "picks": [], "buys": buys,
                     "summary": {}})

    def _set_sells(self, tag, sells, date=_DATE):
        _write_json(os.path.join(self.dir, "sells_%s_%s.json" % (tag, date)),
                    {"date": date, "mode": "live", "sells": sells, "summary": {}})

    def _read_json(self, fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)

    def _ev(self, code, side, qty, price, ts, fee=None):
        return {"time": ts, "code": code, "side": side, "qty": qty, "price": price,
                "order_id": "", "fee": fee, "fill_vwap": price}


class TestSellBackfill(_CaseBase):

    def test_sell_full_close(self):
        """卖全平：601118 现场复刻——账本剔除入 closed（exit_date/exit_price/
        exit_reason），sells_morning 留痕修正为 filled_by_event。"""
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": "2026-10-16", "entry_price": 6.59, "qty": 100,
                       "dev20": -0.0499, "gap": 0.053, "board": "大农业",
                       "days_held": 0, "last_check_date": None}}})
        self._set_sells("morning", [
            {"code": "601118", "side": "SELL", "reason": "pop_sell", "qty_placed": 100,
             "status": "submitted", "filled_qty": 0, "filled_price": None}])
        res = fillbackfill.apply_fill(
            self.dir, self._ev("601118", "SELL", 100, 6.50,
                               "2026-10-19 10:00:07.386568", fee=5.0), log=self._log)
        self.assertEqual(res["action"], "sell_closed")
        book = self._book()
        self.assertNotIn("601118", book["open"], "全平后账本必须剔除")
        closed = book["closed"]["601118"]
        self.assertEqual(closed["exit_date"], "2026-10-19")
        self.assertEqual(closed["exit_price"], 6.50)
        self.assertEqual(closed["exit_qty"], 100)
        self.assertEqual(closed["exit_reason"], "pop_sell")
        self.assertEqual(closed["exit_fee"], 5.0)
        sells = self._read_json(os.path.join(
            self.dir, "sells_morning_%s.json" % _DATE))["sells"]
        self.assertEqual(sells[0]["status"], "filled_by_event")
        self.assertEqual(sells[0]["filled_qty"], 100)
        self.assertEqual(sells[0]["filled_price"], 6.50)

    def test_sell_partial_then_rest(self):
        """生产真实形态的部分回填：executor 迟确认先按账户变化扣了 200 股
        （留痕 filled_qty=200、账本 300→100），随后**成交总量 300** 的 fill 事件到达
        ⇒ 只补差额 100，账本 100→0 剔除入 closed（不二次减量）。"""
        self._set_book({"updated_at": "", "open": {
            "301373": {"entry_date": "2026-10-14", "entry_price": 108.81, "qty": 100,
                       "dev20": -0.1142, "days_held": 2, "last_check_date": "2026-10-16"}}})
        self._set_sells("close", [
            {"code": "301373", "side": "SELL", "reason": "sl5_deep", "qty_placed": 300,
             "status": "filled_by_position", "filled_qty": 200, "filled_price": 99.68}])
        r2 = fillbackfill.apply_fill(
            self.dir, self._ev("301373", "SELL", 300, 99.85,
                               "2026-10-19 14:50:06.900", fee=36.2), log=self._log)
        self.assertEqual(r2["action"], "sell_closed")
        book = self._book()
        self.assertNotIn("301373", book["open"])
        self.assertEqual(book["closed"]["301373"]["exit_qty"], 100,
                         "剔除的是账本剩余的 100 股（迟确认已扣 200）")
        self.assertEqual(book["closed"]["301373"]["exit_reason"], "sl5_deep")
        sells = self._read_json(os.path.join(
            self.dir, "sells_close_%s.json" % _DATE))["sells"]
        self.assertEqual(sells[0]["filled_qty"], 300, "留痕补到成交总量")
        self.assertEqual(sells[0]["filled_price"], 99.85)

    def test_sell_partial_reduces(self):
        """卖部分：委托 300 只成交 200（事件量=200）⇒ 账本 300→100，记 partial_exits。"""
        self._set_book({"updated_at": "", "open": {
            "301373": {"entry_date": "2026-10-14", "entry_price": 108.81, "qty": 300,
                       "dev20": -0.1142, "days_held": 2, "last_check_date": "2026-10-16"}}})
        self._set_sells("close", [
            {"code": "301373", "side": "SELL", "reason": "sl5_deep", "qty_placed": 300,
             "status": "submitted", "filled_qty": 0, "filled_price": None}])
        r1 = fillbackfill.apply_fill(
            self.dir, self._ev("301373", "SELL", 200, 99.85,
                               "2026-10-19 14:50:01.500", fee=24.0), log=self._log)
        self.assertEqual(r1["action"], "sell_reduced")
        book = self._book()
        self.assertEqual(book["open"]["301373"]["qty"], 100)
        self.assertEqual(book["open"]["301373"]["partial_exits"][0]["qty"], 200)

    def test_sell_no_entry_skip(self):
        """孤儿/已剔除：账本没有该票 ⇒ no-op（迟确认已回填的场景也不会二次减量）。"""
        self._set_book({"updated_at": "", "open": {}})
        res = fillbackfill.apply_fill(
            self.dir, self._ev("688003", "SELL", 1700, 59.99,
                               "2026-10-19 14:50:06.900"), log=self._log)
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(self._book()["open"], {})

    def test_sell_already_deducted_by_executor_skip(self):
        """双保险（cycle driver 实证场景）：executor 同步成交已扣账
        （留痕 filled_qty=13900=委托量；账本剩 T+1 锁定的加仓 2700 股），
        fill 事件再到 ⇒ 差额=0 ⇒ 账本**不动**（防二次减量误删加仓残留）。"""
        self._set_book({"updated_at": "", "open": {
            "600002": {"entry_date": "2026-10-18", "entry_price": 7.17, "qty": 2700,
                       "dev20": -0.08, "days_held": 1, "last_check_date": "2026-10-19"}}})
        self._set_sells("close", [
            {"code": "600002", "side": "SELL", "reason": "sl5_deep", "qty_placed": 13900,
             "status": "filled", "filled_qty": 13900, "filled_price": 6.59}])
        res = fillbackfill.apply_fill(
            self.dir, self._ev("600002", "SELL", 13900, 6.60,
                               "2026-10-19 14:50:02.000", fee=90.0), log=self._log)
        self.assertEqual(res["action"], "skipped")
        book = self._book()
        self.assertIn("600002", book["open"], "加仓残留的 2700 股不得被误删")
        self.assertEqual(book["open"]["600002"]["qty"], 2700)


class TestBuyBackfill(_CaseBase):

    def test_buy_new_position_corrected(self):
        """买新建：register 已按委托价入账（6.53），fill@6.59 ⇒ entry_price 修正为
        真实成交价、数量不变；fills 留痕 status→filled_by_event。"""
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": _DATE, "entry_price": 6.53, "qty": 100,
                       "dev20": -0.0499, "gap": 0.053, "board": "大农业",
                       "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("601118", 100, 6.53)])
        res = fillbackfill.apply_fill(
            self.dir, self._ev("601118", "BUY", 100, 6.59,
                               "2026-10-19 10:00:01.498", fee=5.0), log=self._log)
        self.assertEqual(res["action"], "buy_corrected")
        entry = self._book()["open"]["601118"]
        self.assertEqual(entry["qty"], 100, "全成 ⇒ 数量不变")
        self.assertAlmostEqual(entry["entry_price"], 6.59, places=4,
                               msg="entry_price 必须换成真实成交价（6.53→6.59 失真根治）")
        buys = self._read_json(os.path.join(self.dir, "fills_%s.json" % _DATE))["buys"]
        self.assertEqual(buys[0]["status"], "filled_by_event")
        self.assertEqual(buys[0]["filled_qty"], 100)
        self.assertEqual(buys[0]["filled_price"], 6.59)

    def test_buy_add_merge(self):
        """买加仓合并：已有 100@6.59（前日），今日再买 100（委托价 6.95，register
        合并成 200@6.77），真实成交@7.00 ⇒ 加权修正为 200@6.795，entry_date 保持首次。"""
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": "2026-10-16", "entry_price": 6.77, "qty": 200,
                       "dev20": -0.0499, "days_held": 1, "last_check_date": _DATE}}})
        self._set_fills([_buy_rec("601118", 100, 6.95)])
        res = fillbackfill.apply_fill(
            self.dir, self._ev("601118", "BUY", 100, 7.00,
                               "2026-10-19 10:00:02.000", fee=5.0), log=self._log)
        self.assertEqual(res["action"], "buy_corrected")
        entry = self._book()["open"]["601118"]
        self.assertEqual(entry["qty"], 200)
        # (6.77*200 − 6.95*100 + 7.00*100) / 200 = 6.795
        self.assertAlmostEqual(entry["entry_price"], 6.795, places=4)
        self.assertEqual(entry["entry_date"], "2026-10-16", "加仓不改首次入场日")

    def test_buy_partial_fill_reduces(self):
        """买部分成交：委托 7800 只成 5000 ⇒ 账本数量削到真实成交、价换真实价。"""
        self._set_book({"updated_at": "", "open": {
            "002368": {"entry_date": _DATE, "entry_price": 12.75, "qty": 7800,
                       "dev20": None, "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("002368", 7800, 12.75)])
        res = fillbackfill.apply_fill(
            self.dir, self._ev("002368", "BUY", 5000, 12.70,
                               "2026-10-19 10:00:08.000", fee=32.0), log=self._log)
        self.assertEqual(res["action"], "buy_corrected")
        entry = self._book()["open"]["002368"]
        self.assertEqual(entry["qty"], 5000)
        self.assertAlmostEqual(entry["entry_price"], 12.70, places=4)
    def test_buy_sync_confirmed_rec_skip(self):
        """双保险（cycle driver 实证场景）：executor 同步路径已确认
        （轮询 filled，filled_qty=委托量但 filled_price=None——register 已按
        委托价正确入账）⇒ fill 事件再到 ⇒ 跳过，**不得兜底重复加仓**。"""
        self._set_book({"updated_at": "", "open": {
            "600001": {"entry_date": _DATE, "entry_price": 9.324, "qty": 10700,
                       "dev20": -0.05, "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("600001", 10700, 9.324, status="filled",
                                  filled_qty=10700, filled_price=None)])
        res = fillbackfill.apply_fill(
            self.dir, self._ev("600001", "BUY", 10700, 9.324,
                               "2026-10-19 10:00:02.000", fee=5.0), log=self._log)
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(self._book()["open"]["600001"]["qty"], 10700, "不得重复加仓")


class TestIdempotency(_CaseBase):

    def test_duplicate_event_noop(self):
        """重复事件幂等：同一 fill 应用两次 ⇒ 第二次 skipped，账本/留痕零变化。"""
        ev = self._ev("601118", "SELL", 100, 6.50, "2026-10-19 10:00:07.386568", fee=5.0)
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": "2026-10-16", "entry_price": 6.59, "qty": 100,
                       "dev20": -0.0499, "days_held": 0, "last_check_date": None}}})
        r1 = fillbackfill.apply_fill(self.dir, dict(ev), log=self._log)
        self.assertEqual(r1["action"], "sell_closed")
        after1 = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        r2 = fillbackfill.apply_fill(self.dir, dict(ev), log=self._log)
        self.assertEqual(r2["action"], "skipped")
        self.assertEqual(r2["reason"], "duplicate_key")
        after2 = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(after1, after2, "重复事件不得改动账本")

    def test_buy_confirmed_rec_semantic_guard(self):
        """语义守卫：日志缺失（如被清理）但留痕已 confirmed 且参数一致 ⇒ 跳过，
        不得兜底重复入账。"""
        ev = self._ev("601118", "BUY", 100, 6.59, "2026-10-19 10:00:01.498", fee=5.0)
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": _DATE, "entry_price": 6.59, "qty": 100,
                       "dev20": None, "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("601118", 100, 6.53, status="filled_by_event",
                                  filled_qty=100, filled_price=6.59)])
        res = fillbackfill.apply_fill(self.dir, ev, log=self._log)
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(self._book()["open"]["601118"]["qty"], 100, "不得重复加仓")

    def test_journal_persists(self):
        """应用日志落盘且含事件键（跨进程幂等的锚）。"""
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": "2026-10-16", "entry_price": 6.59, "qty": 100,
                       "dev20": None, "days_held": 0, "last_check_date": None}}})
        fillbackfill.apply_fill(
            self.dir, self._ev("601118", "SELL", 100, 6.50,
                               "2026-10-19 10:00:07.386568"), log=self._log)
        fp = os.path.join(self.dir, fillbackfill.JOURNAL_FILE)
        self.assertTrue(os.path.exists(fp))
        with open(fp, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["action"], "sell_closed")
        self.assertIn("601118|SELL|100", lines[0]["key"])

    def test_invalid_event_returns_none(self):
        """非法事件（qty=0 / 价=0 / side 未知）⇒ None，不动账本。"""
        self._set_book({"updated_at": "", "open": {}})
        self.assertIsNone(fillbackfill.apply_fill(
            self.dir, {"time": "2026-10-19 10:00:00", "code": "601118",
                       "side": "SELL", "qty": 0, "price": 6.5}, log=self._log))
        self.assertIsNone(fillbackfill.apply_fill(
            self.dir, {"time": "2026-10-19 10:00:00", "code": "601118",
                       "side": "HOLD", "qty": 100, "price": 6.5}, log=self._log))


class TestAutoConfigShadow(_CaseBase):
    """auto 宿主的 config 不导出 log 时，三类回填仍只能使用临时账本。"""

    def setUp(self):
        super().setUp()
        missing = object()
        self._missing = missing
        self._old_config = sys.modules.get("config", missing)
        self._old_core_utils = sys.modules.get("core.utils", missing)
        sys.modules.pop("core.utils", None)
        # 复刻 auto 的 config 阴影：模块存在，但没有 core.utils 所需的 log。
        sys.modules["config"] = type(sys)("config")

    def tearDown(self):
        for name, old in (("config", self._old_config),
                          ("core.utils", self._old_core_utils)):
            if old is self._missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        super().tearDown()

    def test_fill_buy_sell_and_reject_without_config_log(self):
        self._set_book({"updated_at": "", "open": {
            "600001": {"entry_date": _DATE, "entry_price": 10.0, "qty": 100},
            "600002": {"entry_date": _DATE, "entry_price": 20.0, "qty": 200},
            "600003": {"entry_date": _DATE, "entry_price": 30.0, "qty": 300}}})
        self._set_fills([_buy_rec("600001", 100, 10.0),
                         _buy_rec("600003", 300, 30.0)])
        self._set_sells("close", [
            {"code": "600002", "side": "SELL", "reason": "sl5_deep",
             "qty_placed": 200, "status": "submitted", "filled_qty": 0,
             "filled_price": None}])
        sells_fp = os.path.join(self.dir, "sells_close_%s.json" % _DATE)
        sells_payload = self._read_json(sells_fp)
        sells_payload["summary"]["open_after"] = 3
        _write_json(sells_fp, sells_payload)

        buy = fillbackfill.apply_fill(
            self.dir, self._ev("600001", "BUY", 100, 10.1,
                               "2026-10-19 10:00:01.000"), log=self._log)
        sell = fillbackfill.apply_fill(
            self.dir, self._ev("600002", "SELL", 200, 19.9,
                               "2026-10-19 14:50:01.000"), log=self._log)
        sells_after = self._read_json(sells_fp)
        self.assertEqual(sells_after["summary"]["open_after"], 2)
        self.assertEqual(sells_after["summary"]["sells_filled"], 1)
        self.assertEqual(sells_after["sells"][0]["filled_qty"], 200)

        reject = fillbackfill.apply_reject(
            self.dir, {"time": "2026-10-19 10:00:02.000", "code": "600003",
                       "side": "BUY", "qty": 300, "status": 8,
                       "rej_detail": "柜台拒绝"}, log=self._log)

        self.assertEqual(buy["action"], "buy_corrected")
        self.assertEqual(sell["action"], "sell_closed")
        self.assertEqual(reject["action"], "buy_rejected_removed")
        book = self._book()
        self.assertAlmostEqual(book["open"]["600001"]["entry_price"], 10.1, places=4)
        self.assertNotIn("600002", book["open"])
        self.assertNotIn("600003", book["open"])


if __name__ == "__main__":
    unittest.main()
