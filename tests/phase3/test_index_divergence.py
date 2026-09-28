# -*- coding: utf-8 -*-
"""`analysis/index_divergence.py` 离线单测（2026-09-28）。

覆盖：**30/60min 聚合口径**（一天恰好 8 根 / 4 根；不得带 09:30 竞价根；不得有午休幻影桶）/
      **提醒窗口不变量**（提醒集必须紧于展示集——这是"把 3 天前旧事件当新信号推送"的回归闸）/
      证据分级表 / 事件去重键稳定性 / 事件时点格式化 / 统一窗口截断。

铁律：**全部离线**，不打网络、不读 t_io。依赖实盘数据的部分（靶子复现）不在本文件，
      见 `tmp/verify_index_div.py`（需研究缓存）。

运行：python tests/phase3/test_index_divergence.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from analysis import divergence as dv  # noqa: E402
from analysis import index_divergence as idxd  # noqa: E402

# 30min / 60min 的"正确"时间标签（A股惯例，与 同花顺/通达信 一致）
LBL_30 = ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"]
LBL_60 = ["10:30", "11:30", "14:00", "15:00"]


def _five_min_bars(days):
    """按 A股时段构造若干交易日的 5min bar（时间戳 = bar **结束**时刻）。

    时段：09:35..11:30 与 13:05..15:00（每日各 24 根，共 48 根）。
    """
    rows = []
    for day in days:
        t = pd.Timestamp(f"{day} 09:35")
        for _ in range(24):
            rows.append({"time": t, "open": 1.0, "high": 2.0, "low": 0.5,
                         "close": 1.5, "volume": 100.0})
            t += pd.Timedelta(minutes=5)
        t = pd.Timestamp(f"{day} 13:05")
        for _ in range(24):
            rows.append({"time": t, "open": 1.0, "high": 2.0, "low": 0.5,
                         "close": 1.5, "volume": 100.0})
            t += pd.Timedelta(minutes=5)
    return pd.DataFrame(rows)


class TestAggregation(unittest.TestCase):
    """5min → 30/60min 的切块口径。切错会让背离整体落到错误的 bar 上，且不会报错。"""

    def setUp(self):
        self.df = _five_min_bars(["2026-09-23", "2026-09-24"])

    def test_five_min_bars_per_day(self):
        n = self.df["time"].dt.strftime("%Y-%m-%d").value_counts().to_dict()
        self.assertEqual(sorted(n.values()), [48, 48], f"每日应 48 根 5min：{n}")

    def test_30min_八根且标签正确(self):
        out = idxd._agg_minutes(self.df, "30min")
        per_day = out["time"].dt.strftime("%Y-%m-%d").value_counts().to_dict()
        self.assertEqual(set(per_day.values()), {8}, f"30min 每日应 8 根：{per_day}")
        self.assertEqual(sorted(out["time"].dt.strftime("%H:%M").unique()), LBL_30)

    def test_60min_四根且标签正确(self):
        """A股 60min 非整点对齐（上午自 09:30 起、下午自 13:00 起）。

        若改用 resample('60min') 会按整点切出 5 根，其中 12:00 是午休空档的**幻影桶**。
        """
        out = idxd._agg_minutes(self.df, "60min")
        per_day = out["time"].dt.strftime("%Y-%m-%d").value_counts().to_dict()
        self.assertEqual(set(per_day.values()), {4}, f"60min 每日应 4 根：{per_day}")
        self.assertEqual(sorted(out["time"].dt.strftime("%H:%M").unique()), LBL_60)

    def test_无竞价根与无午休幻影桶(self):
        for rule in ("30min", "60min"):
            labels = set(idxd._agg_minutes(self.df, rule)["time"].dt.strftime("%H:%M"))
            self.assertNotIn("09:30", labels, f"{rule} 不得含 09:30 竞价根")
            self.assertNotIn("12:00", labels, f"{rule} 不得含 12:00 午休幻影桶")

    def test_半天数据不崩(self):
        half = self.df[self.df["time"] <= pd.Timestamp("2026-09-23 10:30")]
        for rule in ("30min", "60min"):
            out = idxd._agg_minutes(half, rule)
            self.assertGreaterEqual(len(out), 1, f"{rule} 半天应至少 1 根")

    def test_空输入返回空(self):
        for rule in ("30min", "60min"):
            self.assertTrue(idxd._agg_minutes(pd.DataFrame(), rule).empty)


class TestAlertWindow(unittest.TestCase):
    """提醒窗口 vs 展示窗口 —— 本模块最要紧的不变量。

    2026-09-28 实测事故：误把**展示**窗口（30min=32 根 ≈4 个交易日）当**提醒**阈值，
    于是当天大盘一路下跌、看盘软件什么都没报，系统却把 09-22（bars_ago=22）的旧事件
    当成当前信号推了出去。此处的两个断言就是那次的回归闸。
    """

    def test_提醒窗口严格紧于展示窗口(self):
        for freq in ("30min", "60min"):
            self.assertLess(
                idxd.ALERT_MAX_AGE_BARS[freq], dv.MAX_AGE_BARS[freq],
                f"{freq}: 提醒窗口必须严格紧于展示窗口，否则旧事件会被当新信号推送")

    def test_仅旧事件时提醒集必须为空(self):
        """只有一个 22 根前的事件：展示集给得出，提醒集必须给不出。"""
        old = {"bars_ago": 22, "tag": "三天前的事件"}
        self.assertIsNone(
            dv._latest_fresh([old], idxd.ALERT_MAX_AGE_BARS["30min"]),
            "提醒窗口内不应有事件 ⇒ 不得推送")
        self.assertIsNotNone(
            dv._latest_fresh([old], dv.MAX_AGE_BARS["30min"]),
            "展示窗口仍应看得到（供 GUI 静默展示）")

    def test_新事件可进入提醒集(self):
        """刚被确认的事件（_local_extrema 需 3 根确认 ⇒ 最早 bars_ago=3）必须能推送。"""
        fresh = {"bars_ago": 3, "tag": "刚确认"}
        got = dv._latest_fresh([{"bars_ago": 22}, fresh], idxd.ALERT_MAX_AGE_BARS["30min"])
        self.assertIsNotNone(got)
        self.assertEqual(got["bars_ago"], 3)

    def test_提醒窗口不窄于确认延迟(self):
        """窗口必须 ≥3（确认延迟），否则永远收不到提醒。"""
        for freq, win in idxd.ALERT_MAX_AGE_BARS.items():
            self.assertGreaterEqual(win, 3, f"{freq}: 窗口 {win} < 确认延迟 3 根 ⇒ 永不触发")


class TestEvidence(unittest.TestCase):
    def test_六个组合齐备(self):
        combos = [("30min", "顶"), ("30min", "底"), ("60min", "顶"),
                  ("60min", "底"), ("日线", "顶"), ("日线", "底")]
        for c in combos:
            self.assertIn(c, idxd.EVIDENCE, f"缺证据分级：{c}")

    def test_只有30min底标记显著(self):
        """仓内研究：30min 底 +5.8pp 显著；其余组合无边际/不显著/未验证。"""
        sig = [c for c, v in idxd.EVIDENCE.items() if v["significant"]]
        self.assertEqual(sig, [("30min", "底")], f"显著项应为且仅为 30min 底：{sig}")

    def test_免责说明不可为空且明确非信号(self):
        self.assertTrue(idxd.DISCLAIMER)
        self.assertIn("交易信号", idxd.DISCLAIMER, "免责说明必须点明不是交易信号")


class TestAlertKey(unittest.TestCase):
    def test_同事件键稳定(self):
        e = {"symbol": "sh000688"}
        a = idxd._alert_key(e, "30min", "顶", "2026-09-22 10:30:00")
        b = idxd._alert_key(e, "30min", "顶", "2026-09-22 10:30:00")
        self.assertEqual(a, b)

    def test_不同维度键不同(self):
        e = {"symbol": "sh000688"}
        base = idxd._alert_key(e, "30min", "顶", "2026-09-22 10:30:00")
        for other in (idxd._alert_key(e, "60min", "顶", "2026-09-22 10:30:00"),
                      idxd._alert_key(e, "30min", "底", "2026-09-22 10:30:00"),
                      idxd._alert_key(e, "30min", "顶", "2026-09-22 11:00:00")):
            self.assertNotEqual(base, other)


class TestEventTimeFormat(unittest.TestCase):
    def test_分钟级格式统一(self):
        got = idxd._fmt_event_time(pd.Timestamp("2026-09-22 10:30:00"), "30min")
        self.assertEqual(got, "2026-09-22 10:30:00")

    def test_日线只到日期(self):
        got = idxd._fmt_event_time(pd.Timestamp("2026-09-22"), "日线")
        self.assertEqual(got, "2026-09-22")

    def test_不可解析时降级为字符串(self):
        self.assertIsInstance(idxd._fmt_event_time("不是时间", "30min"), str)


class TestWindowUniform(unittest.TestCase):
    """各源原生长度不一（GM 30min≈134 / 东财≈250；东财日线可达 6000+），
    而 SWING 门槛按"窗口自身中位振幅"标定 ⇒ 必须统一截断，否则同一张证据表被套在不同门槛上。"""

    def test_三周期均有窗口定义(self):
        for freq in ("30min", "60min", "日线"):
            self.assertIn(freq, idxd.WINDOW_BARS)

    def test_超长被截断(self):
        df = pd.DataFrame({"time": range(1000), "open": 1, "high": 2,
                           "low": 0.5, "close": 1, "volume": 1})
        for freq, n in idxd.WINDOW_BARS.items():
            self.assertEqual(len(idxd._trim(df, freq)), n, f"{freq} 应截断到 {n}")

    def test_短于窗口时原样返回(self):
        df = pd.DataFrame({"time": range(5), "open": 1, "high": 2,
                           "low": 0.5, "close": 1, "volume": 1})
        self.assertEqual(len(idxd._trim(df, "30min")), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
