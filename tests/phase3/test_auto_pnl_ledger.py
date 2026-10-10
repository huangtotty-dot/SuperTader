# -*- coding: utf-8 -*-
"""自动盘盈亏账单 `load_auto_pnl` 的离线单测（2026-10-10，owner 需求5）。

口径：按 code 用 fill.pos_after 归零切分「建仓→清仓」周期；已实现盈亏按加权平均成本法，
未平仓周期用日线缓存最新收盘算浮盈；pos_after 与逐笔不连续 → irregular 标记。

铁律：全离线。把 BRIDGE_DIR / STATE_DIR / daily_kline 目录指到临时目录，
不读真实 t_io/bridge、不打网络。

运行：python tests/phase3/test_auto_pnl_ledger.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402
import core.position_builder as _pb  # noqa: E402


def _fill(time, code, side, qty, price, pos_after, fee=0.0):
    return {"event": "fill", "time": time, "code": code, "side": side,
            "qty": qty, "price": price, "pos_after": pos_after, "fee": fee}


class TestAutoPnlLedger(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.bridge = root / "bridge"
        self.state = root / "state"
        self.daily = root / "daily"
        for d in (self.bridge, self.state, self.daily):
            d.mkdir(parents=True, exist_ok=True)
        # 模块级路径改指向临时目录
        self._orig = (t_gui.BRIDGE_DIR, t_gui.STATE_DIR, _pb._DAILY_CACHE_DIR)
        t_gui.BRIDGE_DIR = self.bridge
        t_gui.STATE_DIR = self.state
        _pb._DAILY_CACHE_DIR = self.daily
        # 名称源
        (self.state / "holdings_auto.json").write_text(
            json.dumps({"600000": {"name": "测试甲"}, "000001": {"name": "测试乙"}}),
            encoding="utf-8")
        # 事件流：600000 一周期(建→加→清) + 再次建仓(在场)；000001 孤儿SELL
        evs = [
            _fill("2026-01-02 09:31:00", "600000", "BUY", 100, 10.0, 100, 5.0),
            _fill("2026-01-03 10:00:00", "600000", "BUY", 200, 11.0, 300, 6.0),
            _fill("2026-01-10 14:00:00", "600000", "SELL", 300, 12.0, 0, 8.0),
            _fill("2026-02-01 09:31:00", "600000", "BUY", 100, 20.0, 100, 3.0),
            _fill("2026-02-05 10:00:00", "000001", "SELL", 100, 5.0, 0, 1.0),  # 孤儿
        ]
        with open(self.bridge / "events_20260102.jsonl", "w", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        (self.bridge / "heartbeat.json").write_text(
            json.dumps({"cash": 12345.0, "positions": {}}), encoding="utf-8")
        # 逐日心跳：01-02 收盘持 600000×300@10.5 / 现金 5000；01-03 空仓 / 现金 12345。
        # 01-02 的**末行故意写成半截 JSON**（引擎逐分钟追加，读尾块必撞）⇒ 应跳到上一条完整行。
        hb1 = [
            {"event": "heartbeat", "time": "2026-01-02 09:31:00", "bar": "09:31",
             "positions": {"SHSE.600000": {"qty": 100, "cost": 10.0}}, "cash": 9000.0},
            {"event": "heartbeat", "time": "2026-01-02 14:58:00", "bar": "14:58",
             "positions": {"SHSE.600000": {"qty": 300, "cost": 10.5}}, "cash": 5000.0},
        ]
        with open(self.bridge / "heartbeat_2026-01-02.jsonl", "w", encoding="utf-8") as f:
            for e in hb1:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.write('{"event": "heartbeat", "time": "2026-01-02 14:59:00", "bar": "14:59"')
        with open(self.bridge / "heartbeat_2026-01-03.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "heartbeat", "time": "2026-01-03 14:58:00",
                                "bar": "14:58", "positions": {}, "cash": 12345.0},
                               ensure_ascii=False) + "\n")
        with open(self.bridge / "heartbeat_2026-01-10.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "heartbeat", "time": "2026-01-10 14:58:00",
                                "bar": "14:58", "positions": {}, "cash": 12345.0},
                               ensure_ascii=False) + "\n")
        # 日线缓存（最新收盘 = 22.0 给 600000，供既有未平仓浮盈用例）
        (self.daily / "600000.json").write_text(
            json.dumps({"rows": [{"date": "2026-01-02", "close": 12.0},
                                 {"date": "2026-01-03", "close": 13.0},
                                 {"date": "2026-02-01", "close": 22.0}]}), encoding="utf-8")
        self.api = t_gui.Api()
        t_gui._AUTO_PNL_CACHE.update({"ts": 0.0, "date": None, "data": None})

    def tearDown(self):
        t_gui.BRIDGE_DIR, t_gui.STATE_DIR, _pb._DAILY_CACHE_DIR = self._orig
        t_gui._AUTO_PNL_CACHE.update({"ts": 0.0, "date": None, "data": None})
        self._tmp.cleanup()

    def _stock(self, d, code):
        return next(s for s in d["stocks"] if s["code"] == code)

    def test_01_周期切分与已实现盈亏(self):
        d = self.api.load_auto_pnl()
        s = self._stock(d, "600000")
        closed = [c for c in s["cycles"] if c["status"] == "closed"]
        self.assertEqual(len(closed), 1, "应恰有一个已平仓周期")
        c = closed[0]
        self.assertEqual(c["start"], "2026-01-02")
        self.assertEqual(c["end"], "2026-01-10")
        self.assertEqual(c["buy_qty"], 300)
        self.assertEqual(c["sell_qty"], 300)
        self.assertEqual(c["fees"], 19.0)
        # realized = (3600-8) - (3200+11) = 381
        self.assertAlmostEqual(c["realized_pnl"], 381.0, places=2)
        self.assertFalse(c["irregular"])
        self.assertEqual(s["name"], "测试甲")

    def test_02_未平仓浮盈(self):
        d = self.api.load_auto_pnl()
        s = self._stock(d, "600000")
        opn = [c for c in s["cycles"] if c["status"] == "open"]
        self.assertEqual(len(opn), 1, "再次建仓应生成新周期（未平仓）")
        c = opn[0]
        self.assertEqual(c["start"], "2026-02-01")
        self.assertEqual(c["open_qty"], 100)
        self.assertEqual(c["last_price"], 22.0)
        # 在场浮盈走**券商口径成本**（fill 派生、不含手续费）：22*100 - 20*100 = 200。
        # 2026-10-10 改：此前用含费均价（2003 → 197），与券商终端「浮动盈亏」对不上
        # （引擎 heartbeat 的 cost 在加仓后不重新加权，故在场成本一律以成交流派生为准）。
        self.assertAlmostEqual(c["unreal_pnl"], 200.0, places=2)
        self.assertAlmostEqual(c["cost_price"], 20.0, places=3)
        self.assertAlmostEqual(s["realized_pnl"], 381.0, places=2, msg="已实现仍按含费口径")
        self.assertAlmostEqual(s["unreal_pnl"], 200.0, places=2)
        self.assertEqual(s["open_qty"], 100)

    def test_03_孤儿成交标记irregular(self):
        d = self.api.load_auto_pnl()
        s = self._stock(d, "000001")
        # 首笔即 SELL 且 pos_after=0 → 无买腿：建仓起点缺失，标记 irregular、已实现为 0
        c = s["cycles"][0]
        self.assertTrue(c["irregular"], "孤儿成交应标记 irregular")
        self.assertEqual(c["realized_pnl"], 0.0)
        self.assertEqual(c["buy_qty"], 0)

    def test_04_汇总(self):
        d = self.api.load_auto_pnl()
        summ = d["summary"]
        self.assertAlmostEqual(summ["realized_pnl"], 381.0, places=2)
        self.assertAlmostEqual(summ["unreal_pnl"], 200.0, places=2)
        self.assertAlmostEqual(summ["total_assets"], 12345.0 + 2200.0, places=2)
        self.assertAlmostEqual(summ["market_value"], 2200.0, places=2)
        self.assertEqual(summ["cash"], 12345.0)
        self.assertEqual(summ["positions_count"], 1)

    # ---- 2026-10-10 需求2：逐笔流水 + 每日盈亏（自动盘无交割单，靠事件总线反推） ----

    def test_05_逐笔流水补齐名称与净额(self):
        d = self.api.load_auto_pnl()
        fills = d["fills"]
        self.assertEqual(len(fills), 5)
        self.assertEqual(fills[0]["date"], "2026-02-05", "应按时间倒序")
        # 2026-01-02 买入 100@10 费5 → 成交额 1000 / 净 -1005
        buy = next(f for f in fills if f["date"] == "2026-01-02")
        self.assertEqual(buy["side"], "BUY")
        self.assertEqual(buy["name"], "测试甲", "名称应从 holdings_auto.json 反查补齐")
        self.assertAlmostEqual(buy["amount"], 1000.0, places=2)
        self.assertAlmostEqual(buy["net"], -1005.0, places=2)
        self.assertEqual(buy["pos_after"], 100)
        # 2026-01-10 卖出 300@12 费8 → 成交额 3600 / 净 +3592
        sell = next(f for f in fills if f["date"] == "2026-01-10")
        self.assertEqual(sell["side"], "SELL")
        self.assertAlmostEqual(sell["amount"], 3600.0, places=2)
        self.assertAlmostEqual(sell["net"], 3592.0, places=2)
        self.assertEqual(sell["pos_after"], 0)

    def test_06_每日盈亏取末条心跳并跳过半截行(self):
        d = self.api.load_auto_pnl()
        daily = d["daily"]
        self.assertEqual([r["date"] for r in daily],
                         ["2026-01-02", "2026-01-03", "2026-01-10"])
        d0 = daily[0]
        self.assertEqual(d0["cash"], 5000.0, "应取当日**末条完整**心跳(现金5000)，不是半截行")
        self.assertAlmostEqual(d0["market_value"], 3600.0, places=2)   # 300×12
        self.assertAlmostEqual(d0["cost"], 3150.0, places=2)           # 300×10.5
        self.assertAlmostEqual(d0["equity"], 8600.0, places=2)
        self.assertAlmostEqual(d0["unreal_pnl"], 450.0, places=2)
        self.assertIsNone(d0["day_pnl"], "首日无前一日可比")
        self.assertAlmostEqual(d0["realized_cum"], 0.0, places=2)
        d1 = daily[1]                                   # 01-03 空仓
        self.assertEqual(d1["cash"], 12345.0)
        self.assertAlmostEqual(d1["equity"], 12345.0, places=2)
        self.assertAlmostEqual(d1["day_pnl"], 3745.0, places=2)
        self.assertAlmostEqual(d1["realized_cum"], 0.0, places=2, msg="当日无卖出腿")
        d2 = daily[2]                                   # 01-10 卖出清仓
        self.assertAlmostEqual(d2["realized_cum"], 381.0, places=2)
        self.assertAlmostEqual(d2["day_pnl"], 0.0, places=2)

    def test_07_逐股成交笔数与轮次(self):
        d = self.api.load_auto_pnl()
        s = self._stock(d, "600000")
        self.assertEqual(s["trades_n"], 4)
        self.assertEqual(s["round_trips"], 1)
        self.assertEqual(self._stock(d, "000001")["trades_n"], 1)

    def test_08_尾块读取只解析末条(self):
        # 只在尾部出现的一条才能被取到；同时验证半截行被跳过
        got = t_gui._tail_last_json_line(self.bridge / "heartbeat_2026-01-02.jsonl")
        self.assertEqual(got["bar"], "14:58")
        # 文件不存在 / 不可解析 → None，不抛
        self.assertIsNone(t_gui._tail_last_json_line(self.bridge / "nope.jsonl"))
        p = self.bridge / "junk.jsonl"
        p.write_text("not json\n", encoding="utf-8")
        self.assertIsNone(t_gui._tail_last_json_line(p))
        # 尾块窗口内没有任何完整行 → None（而不是读到文件头）
        big = self.bridge / "big_one_line.jsonl"
        big.write_text(json.dumps({"bar": "x", "pad": "y" * 200}) + "\n", encoding="utf-8")
        self.assertIsNone(t_gui._tail_last_json_line(big, max_bytes=32))
        self.assertEqual(t_gui._tail_last_json_line(big)["bar"], "x")

    def test_09_孤儿卖出不能当纯利(self):
        """事件流起点之前建的仓 ⇒ 首笔就是卖出、没有买腿。

        逐笔流水的 realized 必须只对**配对到的那部分**计提价差；直接写
        `(amt-fee) - matched*avg` 会在 matched=0 时把整笔卖出款当利润，
        污染 daily 的 realized_cum（实测自动盘曾虚高）。
        """
        evs = [_fill("2026-03-02 09:31:00", "000002", "SELL", 200, 48.5, 0, 5.0)]
        with open(self.bridge / "events_20260302.jsonl", "w", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with open(self.bridge / "heartbeat_2026-03-02.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "heartbeat", "time": "2026-03-02 14:58:00",
                                "bar": "14:58", "positions": {}, "cash": 10000.0},
                               ensure_ascii=False) + "\n")
        t_gui._AUTO_PNL_CACHE.update({"ts": 0.0, "date": None, "data": None})
        t_gui._AUTO_COST_CACHE.update({"ts": 0.0, "data": None})
        d = self.api.load_auto_pnl()
        # 000002 全程无买入腿 ⇒ 这笔卖出不得产生任何已实现，realized_cum 应与前一日持平
        prev = [r for r in d["daily"] if r["date"] < "2026-03-02"]
        row = next(r for r in d["daily"] if r["date"] == "2026-03-02")
        self.assertAlmostEqual(row["realized_cum"], prev[-1]["realized_cum"] if prev else 0.0,
                               places=2, msg="无买腿的卖出不应产生任何已实现")
        self.assertNotIn("000002", [s["code"] for s in d["stocks"] if s["realized_pnl"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
