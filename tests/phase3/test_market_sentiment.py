# -*- coding: utf-8 -*-
"""「市场情绪」面板后端单测（2026-11-13，全离线）。

覆盖：
  A. 正常档：按日期排序、字段齐、切片按 days、last/stats 正确；
  B. 60 日分位：最高值 → 1.0，最低值 → 1/60 量级；
  C. 缺档 → available=False（不抛）；
  D. 脏记录（zt_count=None / 键不是 8 位）被跳过；
  E. 按 mtime 记忆：同一档第二次读不再解析（改档后自动失效）。

数据源 = `t_io/index_regime/e5_ths_cache.json`（`analysis.index_regime` 每天 14:30 落盘）。

运行：python -m unittest tests.phase3.test_market_sentiment -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gui  # noqa: E402


def _rec(i, zt, dt, zb=0.2):
    return {"date": "", "zt_count": zt, "dt_count": dt, "zb_count": 10,
            "zt_touched": zt + 10, "zb_rate": zb, "seal_rate": 1 - zb}


class TestMarketSentiment(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.fp = os.path.join(self.td, "e5_ths_cache.json")
        self._old = gui._E5_THS_FP
        self._old_loc = gui._SENT_LOCAL_FP
        gui._E5_THS_FP = type(self._old)(self.fp)
        # 本地历史档也要隔离：生产那份有 800 天，会把"最近 N 日"的断言全部打乱
        gui._SENT_LOCAL_FP = type(self._old_loc)(os.path.join(self.td, "sentiment_local.json"))
        gui.Api._SENT_MEM = {"ts": 0.0, "mtime": None, "val": None}
        gui.Api._SENT_LIVE_MEM = {"ts": None, "date": None, "val": None}
        self.api = gui.Api()

    def tearDown(self):
        gui._E5_THS_FP = self._old
        gui._SENT_LOCAL_FP = self._old_loc
        gui.Api._SENT_MEM = {"ts": 0.0, "mtime": None, "val": None}
        gui.Api._SENT_LIVE_MEM = {"ts": None, "date": None, "val": None}

    def _write(self, data):
        with open(self.fp, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_basic(self):
        data = {}
        for i in range(80):
            data[f"202601{i % 28 + 1:02d}"] = _rec(i, 10 + i, i % 5)   # 键有重复也无所谓
        # 用真正的连续日期重写一份，避免重复键
        from datetime import date, timedelta
        data = {}
        d0 = date(2026, 1, 1)
        for i in range(80):
            d = d0 + timedelta(days=i)
            data[d.strftime("%Y%m%d")] = _rec(i, 10 + i, i % 5)
        self._write(data)
        r = self.api.load_market_sentiment(30)
        self.assertTrue(r["available"])
        self.assertEqual(len(r["series"]), 30)
        self.assertEqual(r["series"][-1]["date"], (d0 + timedelta(days=79)).isoformat())
        self.assertEqual(r["last"]["zt"], 89)
        self.assertEqual(r["last"]["net"], 89 - (79 % 5))
        self.assertEqual(r["stats"]["n_days"], 80)
        # 日期必须升序
        ds = [x["date"] for x in r["series"]]
        self.assertEqual(ds, sorted(ds))

    def test_composite_index(self):
        """情绪温度 = 0.5×触板分位 + 0.3×(1−炸板率分位) + 0.2×(1−跌停分位)，0~100；展示用 5 日均值。"""
        from datetime import date, timedelta
        d0 = date(2026, 1, 1)
        data = {}
        for i in range(40):
            # 触板递增 / 炸板率递增 / 跌停递减 ⇒ 最后一天"最热"
            data[(d0 + timedelta(days=i)).strftime("%Y%m%d")] = {
                "zt_count": 10 + i, "dt_count": 40 - i, "zb_count": i,
                "zt_touched": 20 + i, "zb_rate": i / 100.0, "seal_rate": 1 - i / 100.0,
            }
        self._write(data)
        r = self.api.load_market_sentiment(600)
        w = r["weights"]
        self.assertAlmostEqual(w["touch"], 0.5)
        ser = r["series"]
        # 前 19 天滚动窗不足 ⇒ 当日原始值为 None；5 日均值再多等 2 天
        self.assertIsNone(ser[0]["sent"])
        self.assertIsNone(ser[0]["sent_ma"])
        self.assertIsNotNone(ser[-1]["sent"])
        self.assertIsNotNone(ser[-1]["sent_ma"])
        # 5 日均值 = 当日值与前 4 日的均值（口径必须与曲线一致）
        tail = [x["sent"] for x in ser[-5:]]
        self.assertAlmostEqual(r["last"]["sent_ma"], round(sum(tail) / len(tail), 1), places=1)
        # 最后一天：分项（5 日均分位）与展示值自洽
        p = r["stats"]["sent_parts"]
        expect = round(100 * (w["touch"] * p["touch"] + w["zb"] * (1 - p["zb"])
                              + w["dt"] * (1 - p["dt"])), 1)
        self.assertAlmostEqual(r["last"]["sent_ma"], expect, places=1)
        # 递增序列的最后一天应显著偏热
        self.assertGreater(r["last"]["sent_ma"], 60)
        # 单调上升（触板分位与封板质量同时改善）
        vals = [x["sent_ma"] for x in ser if x["sent_ma"] is not None]
        self.assertGreater(vals[-1], vals[0])

    def test_transitions_rule(self):
        """弱势(<40) → 强势(≥60)：从**最后一天弱势**起算交易日数；回落弱势就重新计时。"""
        api = self.api
        vals = [None, None, 30, 35, 45, 50, 55, 60, 70, 30, 20, 65, 70]
        dates = [f"d{i}" for i in range(len(vals))]
        tr = api._sent_transitions(vals, dates)
        self.assertEqual(len(tr), 2)
        # 第一次：最后一天弱势 = d3(35)，首次强势 = d7(60) ⇒ 4 个交易日
        self.assertEqual((tr[0]["from"], tr[0]["to"], tr[0]["days"]), ("d3", "d7", 4))
        # 中途回到弱势(d9=30,d10=20) ⇒ 重新计时：d10 → d11 = 1 日
        self.assertEqual((tr[1]["from"], tr[1]["to"], tr[1]["days"]), ("d10", "d11", 1))

    def test_transitions_payload(self):
        """payload 里要有 transitions（带当日温度）与 stats.trans 分布。"""
        from datetime import date, timedelta
        d0 = date(2026, 1, 1)
        data = {}
        for i in range(40):
            data[(d0 + timedelta(days=i)).strftime("%Y%m%d")] = {
                "zt_count": 10 + i, "dt_count": 40 - i, "zb_count": i,
                "zt_touched": 20 + i, "zb_rate": i / 100.0, "seal_rate": 1 - i / 100.0,
            }
        self._write(data)
        r = self.api.load_market_sentiment(600)
        self.assertIn("transitions", r)
        self.assertIn("trans", r["stats"])
        self.assertIn("zone", r["stats"])
        self.assertIsInstance(r["transitions"], list)
        for t in r["transitions"]:
            self.assertEqual(set(t), {"from", "to", "days", "sent"})
            self.assertGreaterEqual(t["days"], 1)

    def test_percentile_bounds(self):
        from datetime import date, timedelta
        d0 = date(2026, 1, 1)
        data = {}
        for i in range(60):
            data[(d0 + timedelta(days=i)).strftime("%Y%m%d")] = _rec(i, 10 + i, 0, zb=i / 100.0)
        self._write(data)
        r = self.api.load_market_sentiment(600)
        self.assertAlmostEqual(r["stats"]["zt_p60"], 1.0)          # 最后一天最高
        self.assertAlmostEqual(r["stats"]["zb_rate_p60"], 1.0)
        # 把最后一个值改成最小
        data[(d0 + timedelta(days=59)).strftime("%Y%m%d")] = _rec(0, 1, 0, zb=0.0)
        self._write(data)
        gui.Api._SENT_MEM = {"ts": 0.0, "mtime": None, "val": None}
        r2 = self.api.load_market_sentiment(600)
        self.assertAlmostEqual(r2["stats"]["zt_p60"], round(1 / 60, 3))
        self.assertEqual(r2["stats"]["zt_ma5"], round(sum(10 + i for i in range(55, 59)) / 5 + 1 / 5, 1))

    def test_merge_local_history(self):
        """同花顺只有近期；更早的历史由本地自算补，**同日以同花顺为准**。"""
        self._write({
            "20260105": _rec(1, 50, 3),
            "20260106": _rec(2, 60, 2),
        })
        with open(gui._SENT_LOCAL_FP, "w", encoding="utf-8") as f:
            json.dump({"series": {
                "20260102": {"zt": 40, "dt": 5, "touch": 60, "zb": 20, "n": 4800, "zb_rate": 0.33},
                "20260105": {"zt": 99, "dt": 9, "touch": 120, "zb": 21, "n": 4800, "zb_rate": 0.17},
                "20260101": {"zt": 1, "dt": 0, "touch": 1, "zb": 0, "n": 100, "zb_rate": 0.0},
            }}, f)
        gui.Api._SENT_MEM = {"ts": 0.0, "mtime": None, "val": None}
        r = self.api.load_market_sentiment(600)
        rows = {x["date"]: x for x in r["series"]}
        self.assertIn("2026-01-02", rows)              # 本地独有 → 保留
        self.assertEqual(rows["2026-01-02"]["src"], "local")
        self.assertEqual(rows["2026-01-02"]["zt"], 40)
        self.assertEqual(rows["2026-01-05"]["src"], "ths")   # 同日 → 同花顺覆盖
        self.assertEqual(rows["2026-01-05"]["zt"], 50)
        self.assertNotIn("2026-01-01", rows)           # 覆盖家数不足（n=100）→ 丢弃
        self.assertEqual(r["stats"]["src_ths"], 2)
        self.assertEqual(r["stats"]["src_local"], 1)
        self.assertEqual(r["start"], "2026-01-02")

    def test_missing_file(self):
        r = self.api.load_market_sentiment(120)
        self.assertFalse(r["available"])
        self.assertIn("error", r)

    def test_dirty_rows_skipped(self):
        from datetime import date, timedelta
        d0 = date(2026, 1, 1)
        data = {}
        for i in range(30):
            data[(d0 + timedelta(days=i)).strftime("%Y%m%d")] = _rec(i, 10 + i, 1)
        data["20260220"] = {"zt_count": None, "dt_count": None}     # 缺值（不在 30 天窗口内）
        data["badkey"] = _rec(1, 5, 1)                              # 非 8 位键
        self._write(data)
        r = self.api.load_market_sentiment(600)
        self.assertEqual(r["stats"]["n_days"], 30)
        self.assertTrue(all(len(x["date"]) == 10 for x in r["series"]))

    def test_memo_by_mtime(self):
        from datetime import date, timedelta
        d0 = date(2026, 1, 1)
        data = {(d0 + timedelta(days=i)).strftime("%Y%m%d"): _rec(i, 10 + i, 1) for i in range(30)}
        self._write(data)
        r1 = self.api.load_market_sentiment(120)
        self.assertEqual(r1["last"]["zt"], 39)
        # 不重写文件（mtime 不变）⇒ 命中记忆
        memo = gui.Api._SENT_MEM
        r2 = self.api.load_market_sentiment(120)
        self.assertIs(gui.Api._SENT_MEM, memo)
        self.assertEqual(r2["last"]["zt"], 39)
        # 改档 ⇒ mtime 变 ⇒ 重新解析
        data[(d0 + timedelta(days=29)).strftime("%Y%m%d")] = _rec(29, 999, 1)
        self._write(data)
        os.utime(self.fp, (os.path.getatime(self.fp), os.path.getmtime(self.fp) + 5))
        r3 = self.api.load_market_sentiment(120)
        self.assertEqual(r3["last"]["zt"], 999)

    def test_live_off_session(self):
        """开盘前/周末 → available=False 且零网络（2026-11-16 起收盘后例外，见下一条）。"""
        import datetime as _dt
        with mock.patch.object(gui, "_in_trading_session", return_value=False), \
                mock.patch.object(gui, "datetime") as _m:
            _m.now.return_value = _dt.datetime(2026, 11, 16, 8, 40)     # 周一开盘前
            r = self.api.load_sentiment_live()
        self.assertFalse(r["available"])
        self.assertFalse(r["intraday"])

    def test_live_after_close_intraday_false(self):
        """收盘后（15:05~当晚）仍给当日读数，但标 intraday=False（已定型）。

        2026-11-16 owner 报「尾盘没有实时更新」：原来 15:05 一到就 available=False，加上
        14:30 尾盘快照任务当天崩溃 ⇒ 面板整天停在上一交易日。"""
        import datetime as _dt
        fake = {"zt": 32, "dt": 60, "touch": 48, "zb_rate": 33.33}
        with mock.patch.object(gui, "_in_trading_session", return_value=False), \
                mock.patch.object(gui, "_limit_counts_from_ths", return_value=fake), \
                mock.patch.object(gui, "datetime") as _m:
            _m.now.return_value = _dt.datetime(2026, 11, 16, 15, 40)
            r = self.api.load_sentiment_live()
        self.assertTrue(r["available"])
        self.assertFalse(r["intraday"])
        self.assertEqual(r["zt"], 32)
        self.assertEqual(r["dt"], 60)

    def test_live_lunch_break_shows_today(self):
        """午休（11:35~12:55）仍给当日读数，标 paused=True —— 不能整行消失。

        2026-11-17 owner 再报「GUI 实盘更新再次消失」：11:35 一到 `_in_sess` 变 False，
        原实现直接 available=False ⇒ 📡 行隐藏到 12:55。午休正是白天看盘时间，上午收盘的
        涨停/跌停数在午休期间是定型的，必须照常显示（前端按 paused 标「午休」）。"""
        import datetime as _dt
        fake = {"zt": 41, "dt": 19, "touch": 48, "zb_rate": 14.58}
        with mock.patch.object(gui, "_in_trading_session", return_value=False), \
                mock.patch.object(gui, "_limit_counts_from_ths", return_value=fake), \
                mock.patch.object(gui, "datetime") as _m:
            _m.now.return_value = _dt.datetime(2026, 11, 17, 12, 4)
            r = self.api.load_sentiment_live()
        self.assertTrue(r["available"])
        self.assertTrue(r["intraday"])
        self.assertTrue(r["paused"])
        self.assertEqual(r["zt"], 41)
        self.assertEqual(r["dt"], 19)

    def test_live_lunch_window_bounds(self):
        """午休窗口只认 11:35~12:55：11:34 / 13:10 在 `_in_sess` 被关掉时不得放行
        （真实世界这两刻属于交易时段，由 `_in_sess` 放行；这里专测 `_lunch` 边界）。"""
        import datetime as _dt
        fake = {"zt": 41, "dt": 19, "touch": 48, "zb_rate": 14.58}
        for hh, mm in ((11, 34), (13, 10)):
            with mock.patch.object(gui, "_in_trading_session", return_value=False), \
                    mock.patch.object(gui, "_limit_counts_from_ths", return_value=fake), \
                    mock.patch.object(gui, "datetime") as _m:
                _m.now.return_value = _dt.datetime(2026, 11, 17, hh, mm)
                r = self.api.load_sentiment_live()
            self.assertFalse(r["available"], f"{hh}:{mm} 不该被午休窗口放行")

    def test_live_weekend_hidden(self):
        """周末（没有"今天的行情"）⇒ 收盘后口径也不放行。"""
        import datetime as _dt
        with mock.patch.object(gui, "_in_trading_session", return_value=False), \
                mock.patch.object(gui, "datetime") as _m:
            _m.now.return_value = _dt.datetime(2026, 11, 14, 16, 0)     # 周六
            r = self.api.load_sentiment_live()
        self.assertFalse(r["available"])

    def test_live_on_session(self):
        """盘中读数：交易时段读同花顺计数（patch 网络），字段齐、不写日频 series。"""
        fake = {"zt": 55, "dt": 3, "touch": 88, "zb_rate": 22.5}
        with mock.patch.object(gui, "_in_trading_session", return_value=True), \
                mock.patch.object(gui, "_limit_counts_from_ths", return_value=fake):
            r = self.api.load_sentiment_live()
        self.assertTrue(r["available"])
        self.assertTrue(r["intraday"])
        self.assertEqual(r["zt"], 55)
        self.assertEqual(r["dt"], 3)
        self.assertEqual(r["touch"], 88)
        self.assertEqual(r["zb_rate"], 22.5)
        self.assertIn("fetched_at", r)

    def test_live_provisional_index(self):
        """盘中读数：今日计数按同口径折算成温度；**不写进**日频 series。"""
        from datetime import date, timedelta
        d0 = date(2026, 1, 1)
        self._write({(d0 + timedelta(days=i)).strftime("%Y%m%d"): _rec(i, 10 + i, i % 5)
                     for i in range(80)})
        fake = {"zt": 30, "dt": 2, "touch": 40, "zb_rate": 20.0}
        with mock.patch.object(gui, "_in_trading_session", return_value=True), \
                mock.patch.object(gui, "_limit_counts_from_ths", return_value=fake):
            r = self.api.load_sentiment_live()
        self.assertTrue(r["available"])
        self.assertIsNotNone(r["sent"])
        self.assertIsNotNone(r["sent_ma"])
        self.assertIn(r["zone"], (0, 1, 2))
        self.assertGreaterEqual(r["zone_days"], 1)
        self.assertEqual(set(r["parts"]), {"touch", "zb", "dt"})
        # 日频序列仍是最后一个定型日 —— 盘中那格只在显示层拼上去
        h = self.api.load_market_sentiment(1200)
        self.assertEqual(h["end"], (d0 + timedelta(days=79)).isoformat())

    def test_pct_of_excludes_self(self):
        """今日分位用历史窗口（不含自身）：顶到历史最高 = 1.0；样本不足/值为空 → None。"""
        self.assertEqual(gui._sent_pct_of([1, 2, 3], 3, min_n=3), 1.0)
        self.assertEqual(gui._sent_pct_of([1, 2, 3], 1, min_n=3), round(1 / 3, 4))
        self.assertIsNone(gui._sent_pct_of([1, 2], 3, min_n=3))
        self.assertIsNone(gui._sent_pct_of([1, 2, 3], None, min_n=3))

    def test_live_fetch_failure(self):
        """盘中读数：交易时段但取数失败/全零 → available=False，不抛。"""
        with mock.patch.object(gui, "_in_trading_session", return_value=True), \
                mock.patch.object(gui, "_limit_counts_from_ths", return_value=None):
            r = self.api.load_sentiment_live()
        self.assertFalse(r["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
