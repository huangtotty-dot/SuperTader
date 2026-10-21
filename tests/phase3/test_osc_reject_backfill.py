# -*- coding: utf-8 -*-
"""超跌跳空确认 · 拒单事件驱动回填（`fillbackfill.apply_reject`）——
**2026-10-21 复盘缺陷的回归守卫**。

## 现场

002591/688028 两笔买单被柜台拒单，但宿主拒单分支只写 write_reject/write_risk，
从不回填 OSC ⇒ fills_2026-10-21.json 里两笔永远挂 status="submitted"、filled_qty=0，
summary `rejected_count=0` 虚报；OSC 买侧「挂单即入账」⇒ 账本虚挂，要等次日 10:00
卖前 reconcile_book 按账户夹紧才剔除。

## 覆盖五场景

    a. BUY 拒单：submitted 留痕 → rejected，summary rejected_count=1 且 buys_placed 减 1，
       exec_report 同步，账本剔除该笔（新票整条删除）；journal 幂等（重放 no-op）
    b. BUY 拒单加权场景：旧持仓加权合并过 ⇒ 只剔新挂数量，entry_price 反算还原旧成本
    c. SELL 拒单：sells_close_ 留痕 → rejected，账本持仓不动
    d. 无匹配留痕（孤儿/手工单拒单）⇒ no-op，账本/fills 均不变
    e. 留痕已 filled_by_event 再来拒单 ⇒ 语义守卫跳过

运行：python -m unittest tests.phase3.test_osc_reject_backfill -v
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

_DATE = "2026-10-21"


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

    def _set_exec_report(self, date=_DATE):
        _write_json(os.path.join(self.dir, "exec_report_%s.json" % date),
                    {"date": date, "summary": {"rejected_count": 0, "buys_placed": 2}})

    def _set_sells(self, tag, sells, date=_DATE):
        _write_json(os.path.join(self.dir, "sells_%s_%s.json" % (tag, date)),
                    {"date": date, "mode": "live", "sells": sells, "summary": {}})

    def _read_json(self, fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)

    def _ev(self, code, side, qty, ts, status=8, rej_detail="柜台拒绝", order_id=""):
        return {"time": ts, "code": code, "side": side, "qty": qty,
                "order_id": order_id, "status": status, "rej_detail": rej_detail}


class TestBuyRejectBackfill(_CaseBase):

    def test_a_buy_reject_new_position_removed(self):
        """a. BUY 拒单（10-21 现场复刻）：002591 submitted 留痕 → rejected，
        summary rejected_count=1、buys_placed 2→1（688028 仍在途），exec_report 同步；
        新票账本整条剔除；journal 幂等——重放同事件 no-op。"""
        self._set_book({"updated_at": "", "open": {
            "002591": {"entry_date": _DATE, "entry_price": 18.50, "qty": 300,
                       "dev20": -0.06, "gap": 0.052, "board": "半导体",
                       "days_held": 0, "last_check_date": None},
            "688028": {"entry_date": _DATE, "entry_price": 45.20, "qty": 100,
                       "dev20": -0.05, "gap": 0.048, "board": "存储芯片",
                       "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("002591", 300, 18.50),
                         _buy_rec("688028", 100, 45.20)])
        self._set_exec_report()
        ev = self._ev("002591", "BUY", 300, "2026-10-21 09:35:01.000",
                      status=8, rej_detail="可用资金不足")
        res = fillbackfill.apply_reject(self.dir, dict(ev), log=self._log)
        self.assertEqual(res["action"], "buy_rejected_removed")
        # 账本：002591 整条剔除，688028 不动
        book = self._book()
        self.assertNotIn("002591", book["open"], "拒单虚挂必须整条剔除")
        self.assertEqual(book["open"]["688028"]["qty"], 100)
        # fills 留痕
        fills = self._read_json(os.path.join(self.dir, "fills_%s.json" % _DATE))
        rec = fills["buys"][0]
        self.assertEqual(rec["status"], "rejected")
        self.assertIn("柜台拒单 status=8", rec["message"])
        self.assertIn("可用资金不足", rec["message"])
        self.assertTrue(rec.get("reject_confirmed_at"))
        self.assertEqual(rec["qty_remaining"], 0)
        # summary 重算：buys_placed 2→1，rejected_count 0→1
        self.assertEqual(fills["summary"]["rejected_count"], 1)
        self.assertEqual(fills["summary"]["buys_placed"], 1)
        self.assertEqual(fills["summary"]["buys_planned"], 2)
        # exec_report 同步
        rep = self._read_json(os.path.join(self.dir, "exec_report_%s.json" % _DATE))
        self.assertEqual(rep["summary"]["rejected_count"], 1)
        self.assertEqual(rep["summary"]["buys_placed"], 1)
        # journal 幂等：重放同事件 ⇒ skipped，账本/留痕零变化
        snap_book = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        snap_fills = json.dumps(fills, sort_keys=True, ensure_ascii=False)
        r2 = fillbackfill.apply_reject(self.dir, dict(ev), log=self._log)
        self.assertEqual(r2["action"], "skipped")
        self.assertEqual(r2["reason"], "duplicate_key")
        self.assertEqual(json.dumps(self._book(), sort_keys=True, ensure_ascii=False),
                         snap_book, "重放不得改动账本")
        fills2 = self._read_json(os.path.join(self.dir, "fills_%s.json" % _DATE))
        self.assertEqual(json.dumps(fills2, sort_keys=True, ensure_ascii=False),
                         snap_fills, "重放不得改动留痕")
        # 拒单键与 fill 键不撞：同参数 fill 事件键不在日志里
        self.assertNotIn(fillbackfill.fill_key(
            {"time": ev["time"], "code": "002591", "side": "BUY", "qty": 300,
             "price": 18.50}), fillbackfill._journal_keys(self.dir))

    def test_b_buy_reject_weighted_restores_old_cost(self):
        """b. BUY 拒单加权场景：旧持仓 100@6.59，今日再挂 100@6.95（register 加权
        合并成 200@6.77）⇒ 拒单只剔新挂 100 股，entry_price 反算还原 6.59。"""
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": "2026-10-20", "entry_price": 6.77, "qty": 200,
                       "dev20": -0.0499, "days_held": 1, "last_check_date": _DATE}}})
        self._set_fills([_buy_rec("601118", 100, 6.95)])
        res = fillbackfill.apply_reject(
            self.dir, self._ev("601118", "BUY", 100, "2026-10-21 09:35:02.000",
                               status=8, rej_detail="柜台拒绝"), log=self._log)
        self.assertEqual(res["action"], "buy_rejected_unweighted")
        entry = self._book()["open"]["601118"]
        self.assertEqual(entry["qty"], 100, "只剔新挂的 100 股")
        # (6.77*200 − 6.95*100) / 100 = 6.59 ⇒ 还原旧成本
        self.assertAlmostEqual(entry["entry_price"], 6.59, places=4)
        self.assertEqual(entry["entry_date"], "2026-10-20", "拒单回滚不改首次入场日")

    def test_buy_reject_book_missing_noop(self):
        """边界：留痕修正照做，但账本已无该 code（reconcile 已夹紧）⇒ 账本 no-op。"""
        self._set_book({"updated_at": "", "open": {}})
        self._set_fills([_buy_rec("002591", 300, 18.50)])
        res = fillbackfill.apply_reject(
            self.dir, self._ev("002591", "BUY", 300, "2026-10-21 09:35:03.000"),
            log=self._log)
        self.assertEqual(res["action"], "buy_rejected_book_missing")
        self.assertEqual(self._book()["open"], {}, "账本不得凭空新建")
        fills = self._read_json(os.path.join(self.dir, "fills_%s.json" % _DATE))
        self.assertEqual(fills["buys"][0]["status"], "rejected", "留痕仍须修正")

    def test_buy_reject_abnormal_qty_removed_with_warning(self):
        """边界：账本数量 < 委托量（异常态）⇒ 删条目并 log 警示。"""
        self._set_book({"updated_at": "", "open": {
            "002591": {"entry_date": _DATE, "entry_price": 18.50, "qty": 100,
                       "dev20": None, "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("002591", 300, 18.50)])
        res = fillbackfill.apply_reject(
            self.dir, self._ev("002591", "BUY", 300, "2026-10-21 09:35:04.000"),
            log=self._log)
        self.assertEqual(res["action"], "buy_rejected_removed_abnormal")
        self.assertNotIn("002591", self._book()["open"])
        self.assertTrue(any("异常态" in l for l in self.logs), "必须 log 警示")


class TestSellRejectBackfill(_CaseBase):

    def test_c_sell_reject_book_untouched(self):
        """c. SELL 拒单：sells_close_ 留痕 → rejected + summary rejected_count=1；
        账本持仓不动（卖侧下单不扣账，拒单无账可回滚）。"""
        self._set_book({"updated_at": "", "open": {
            "301373": {"entry_date": "2026-10-20", "entry_price": 108.81, "qty": 300,
                       "dev20": -0.1142, "days_held": 1, "last_check_date": _DATE}}})
        self._set_sells("close", [
            {"code": "301373", "side": "SELL", "reason": "sl5_deep", "qty_placed": 300,
             "status": "submitted", "filled_qty": 0, "filled_price": None,
             "message": ""}])
        before = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        res = fillbackfill.apply_reject(
            self.dir, self._ev("301373", "SELL", 300, "2026-10-21 14:50:01.000",
                               status=8, rej_detail="超过涨跌幅限制"), log=self._log)
        self.assertEqual(res["action"], "sell_rejected")
        after = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(before, after, "SELL 拒单账本必须不动")
        sells = self._read_json(os.path.join(
            self.dir, "sells_close_%s.json" % _DATE))
        rec = sells["sells"][0]
        self.assertEqual(rec["status"], "rejected")
        self.assertIn("柜台拒单 status=8", rec["message"])
        self.assertTrue(rec.get("reject_confirmed_at"))
        self.assertEqual(sells["summary"]["rejected_count"], 1)


class TestOrphanAndGuards(_CaseBase):

    def test_d_orphan_reject_noop(self):
        """d. 无匹配留痕（孤儿/仿真终端手工单拒单）⇒ no-op，账本/fills 均不变。"""
        self._set_book({"updated_at": "", "open": {
            "600001": {"entry_date": "2026-10-20", "entry_price": 9.32, "qty": 1000,
                       "dev20": -0.05, "days_held": 1, "last_check_date": _DATE}}})
        # 场景1：fills 文件存在但无该 code 留痕
        self._set_fills([_buy_rec("688028", 100, 45.20)])
        before_book = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        fills_fp = os.path.join(self.dir, "fills_%s.json" % _DATE)
        before_fills = json.dumps(self._read_json(fills_fp),
                                  sort_keys=True, ensure_ascii=False)
        res = fillbackfill.apply_reject(
            self.dir, self._ev("300999", "BUY", 500, "2026-10-21 10:00:00.000",
                               status=8, rej_detail="手工单被拒"), log=self._log)
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(res["reason"], "semantic_guard")
        self.assertEqual(json.dumps(self._book(), sort_keys=True, ensure_ascii=False),
                         before_book, "孤儿拒单不得动账本")
        self.assertEqual(json.dumps(self._read_json(fills_fp),
                                    sort_keys=True, ensure_ascii=False),
                         before_fills, "孤儿拒单不得动 fills")
        # 场景2：fills 文件根本不存在也不崩
        res2 = fillbackfill.apply_reject(
            self.dir, self._ev("300998", "SELL", 200, "2026-10-21 10:01:00.000"),
            log=self._log)
        self.assertEqual(res2["action"], "skipped")

    def test_e_filled_rec_semantic_guard(self):
        """e. 留痕已 filled_by_event 的 code 再来拒单（事件乱序/重复）⇒ 语义守卫
        跳过，账本/留痕均不得改回 rejected。"""
        self._set_book({"updated_at": "", "open": {
            "601118": {"entry_date": _DATE, "entry_price": 6.59, "qty": 100,
                       "dev20": None, "days_held": 0, "last_check_date": None}}})
        self._set_fills([_buy_rec("601118", 100, 6.53, status="filled_by_event",
                                  filled_qty=100, filled_price=6.59)])
        before_book = json.dumps(self._book(), sort_keys=True, ensure_ascii=False)
        res = fillbackfill.apply_reject(
            self.dir, self._ev("601118", "BUY", 100, "2026-10-21 09:35:05.000",
                               status=8, rej_detail="迟到的拒单"), log=self._log)
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(json.dumps(self._book(), sort_keys=True, ensure_ascii=False),
                         before_book)
        fills = self._read_json(os.path.join(self.dir, "fills_%s.json" % _DATE))
        self.assertEqual(fills["buys"][0]["status"], "filled_by_event",
                         "已成交留痕不得被改回 rejected")

    def test_invalid_event_returns_none(self):
        """非法事件（qty=0 / side 未知 / 空 code）⇒ None，不动账本。"""
        self._set_book({"updated_at": "", "open": {}})
        self.assertIsNone(fillbackfill.apply_reject(
            self.dir, {"time": "2026-10-21 10:00:00", "code": "601118",
                       "side": "BUY", "qty": 0, "status": 8}, log=self._log))
        self.assertIsNone(fillbackfill.apply_reject(
            self.dir, {"time": "2026-10-21 10:00:00", "code": "601118",
                       "side": "HOLD", "qty": 100, "status": 8}, log=self._log))
        self.assertIsNone(fillbackfill.apply_reject(
            self.dir, {"time": "2026-10-21 10:00:00", "code": "",
                       "side": "BUY", "qty": 100, "status": 8}, log=self._log))


if __name__ == "__main__":
    unittest.main()
