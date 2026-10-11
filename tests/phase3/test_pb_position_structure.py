# -*- coding: utf-8 -*-
"""建仓信号扫描「仓位结构」列（30min/10min 结构参考）离线单测（2026-10-11）。**全离线、零网络**。

背景：owner 要「跟建仓信号扫描保持一致，用 30 分钟/10 分钟周期辅助仓位管理」。
本层把 `core/position_structure.py`（纯函数、无 IO）接到 `core/position_structure_feed.py`
（取数适配）+ 建仓表新列（t_gui `_PB_STRUCT_*`，与「30min 判定」列同纪律：主线程零计算零网络）。

⚠️ 纪律：本层是**结构参考、不是买卖信号**（模块实测：机械按阶段加减仓纯择时贡献不显著，
「破前低就清仓」−30pp 强有害；唯一有证据的是破位后在下档支撑挂限价补仓）。

覆盖：
  · resample_5min_to_10min：**生产 tushare 无 amount 列**（回归此前 KeyError 缺陷）、
    剔除 09:30 竞价棒、2:1 配对、**不跨日配对**
  · compute_structure：数据不足 fail-soft；破位时补仓档**严格位于止损位下方**
  · 因果性烟雾：末根变化不改变更早的 stage（截断不变性，完整版见 t_io/validation/pos_struct）
  · feed：ts_code 映射、未知代码 fail-soft、**全程 monkeypatch 零网络**、data_asof 口径
  · feed：_fresh_enough 盘中/盘后判定
  · GUI：冷缓存 → pending（主线程零计算）；历史日 compute=False → None

运行：python -m unittest tests.phase3.test_pb_position_structure
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import threading as _th  # noqa: E402
import time as _time_mod  # noqa: E402

import t_gui  # noqa: E402
from core import chart_cache as cc  # noqa: E402
from core import position_structure as PS  # noqa: E402
from core import position_structure_feed as F  # noqa: E402

DAY = "2026-10-09"


def _times_5min():
    """一天 49 根 5min 时间戳（含 09:30 竞价棒）——对齐生产 tushare 实测的 49 根/日：
    09:30 竞价 + 09:35~11:30(24) + 13:05~15:00(24)。剔竞价后 48 根 ⇒ 24 根 10min。"""
    out = ["09:30"]
    t = datetime(2026, 10, 9, 9, 35)
    while t <= datetime(2026, 10, 9, 11, 30):
        out.append(t.strftime("%H:%M")); t += timedelta(minutes=5)
    t = datetime(2026, 10, 9, 13, 5)
    while t <= datetime(2026, 10, 9, 15, 0):
        out.append(t.strftime("%H:%M")); t += timedelta(minutes=5)
    return out


def _bars_5min(days=6, start=100.0, up=0.06, down=0.10, no_amount=True):
    """合成 5min：每日 49 根（含竞价棒），**逐日递增日期**（勿复用同一天，否则会被当成一天）。

    ⚠️ 价格必须**有摆动**（4 根上 / 4 根下的锯齿 + 净下行），否则序列严格单调 ⇒ 因果摆动点
       一个都找不到 ⇒ `stop_level=None`（模块如实返回"无结构依据"，不是缺陷）。
    no_amount=True 复刻**生产 tushare 列集**（无 amount 列）。"""
    rows, px, op = [], start, start
    for d in range(days):
        day = (datetime(2026, 9, 1) + timedelta(days=d)).strftime("%Y-%m-%d")
        for i, hm in enumerate(_times_5min()):
            op = px
            px += up if (i % 8) < 4 else -down
            r = {"time": f"{day} {hm}:00", "open": op, "high": max(op, px) + 0.02,
                 "low": min(op, px) - 0.02, "close": px, "volume": 1000.0}
            if not no_amount:
                r["amount"] = 10000.0
            rows.append(r)
    return pd.DataFrame(rows)


def _bars_30min(days=12, start=100.0, up=0.20, down=0.35):
    """合成 30min：每日 8 根，净下行 + 摆动（同理需要摆动，MA65/摆动低点才有依据）。"""
    rows, px, op = [], start, start
    for d in range(days):
        for i, hm in enumerate(("10:00", "10:30", "11:00", "11:30",
                                "13:30", "14:00", "14:30", "15:00")):
            op = px
            px += up if (i % 4) < 2 else -down
            rows.append({"time": f"2026-09-{(d % 28) + 1:02d} {hm}:00", "open": op,
                         "high": max(op, px) + 0.05, "low": min(op, px) - 0.05,
                         "close": px, "volume": 5000.0})
    return pd.DataFrame(rows)


def _daily(n=40, start=130.0, drift=-0.9):
    """合成日线：净下行（保证 MA20/前20日最低 落在止损位下方）。"""
    rows, px = [], start
    for i in range(n):
        px += drift
        rows.append({"date": f"2026-0{1 + i // 28}-{(i % 28) + 1:02d}", "open": px - 0.5,
                     "high": px + 1.0, "low": px - 1.0, "close": px, "volume": 100000.0})
    return pd.DataFrame(rows)


class TestResample10min(unittest.TestCase):
    def test_01_no_amount_column(self):
        """生产 tushare 5min **没有 amount 列** —— 旧实现无条件聚合 amount 会 KeyError。"""
        out = PS.resample_5min_to_10min(_bars_5min(no_amount=True))
        self.assertFalse(out.empty)
        self.assertIn("dt", out.columns)
        self.assertNotIn("amount", out.columns)      # 无列就不产出，不得报错

    def test_02_with_amount_column(self):
        out = PS.resample_5min_to_10min(_bars_5min(no_amount=False))
        self.assertFalse(out.empty)
        self.assertIn("amount", out.columns)

    def test_03_drops_auction_and_pairs_2to1(self):
        b5 = _bars_5min(days=2)
        out = PS.resample_5min_to_10min(b5)
        # 每天 49 根 5min（含 09:30）→ 剔除竞价 → 48 → 24 根 10min
        self.assertEqual(len(out), 48)
        self.assertFalse(any(t.strftime("%H:%M") == "09:30" for t in out["dt"]),
                         "09:30 竞价棒必须剔除（价=开盘价、量极小，会污染摆动点）")
        self.assertEqual(out["dt"].iloc[0].strftime("%H:%M"), "09:40",
                         "首根 10min = (09:35,09:40) 配对，标签取配对第二根")

    def test_04_no_cross_day_pairing(self):
        """奇数根的一天不得与次日配对（按 (日, 序号//2) 分组）。"""
        b5 = _bars_5min(days=2)
        b5 = b5[~b5["time"].str.endswith("09:30:00")]      # 剔两天竞价棒 → 各 48 根
        b5 = b5.iloc[1:]                                   # 首日变 47 根（奇数）
        out = PS.resample_5min_to_10min(b5)
        days = out["dt"].dt.normalize()
        self.assertEqual(days.nunique(), 2)
        self.assertEqual(out.groupby(days).size().tolist(), [24, 24])   # 47→24(含单根尾), 48→24
        # 次日须从自己的 09:40 重新起配，而不是接着首日那根落单的 14:55
        _d2 = days.max()
        self.assertEqual(out[days == _d2]["dt"].iloc[0].strftime("%H:%M"), "09:40")

    def test_05_ohlc_aggregation(self):
        b5 = pd.DataFrame([
            {"time": "2026-10-09 09:35:00", "open": 10.0, "high": 10.5, "low": 9.8,
             "close": 10.2, "volume": 100.0},
            {"time": "2026-10-09 09:40:00", "open": 10.2, "high": 11.0, "low": 10.1,
             "close": 10.9, "volume": 200.0},
        ])
        out = PS.resample_5min_to_10min(b5)
        self.assertEqual(len(out), 1)
        r = out.iloc[0]
        self.assertEqual((r["open"], r["high"], r["low"], r["close"]), (10.0, 11.0, 9.8, 10.9))
        self.assertEqual(r["volume"], 300.0)
        self.assertEqual(r["dt"].strftime("%H:%M"), "09:40")


class TestComputeStructure(unittest.TestCase):
    def _build(self, **kw):
        b10 = PS.resample_5min_to_10min(_bars_5min(**kw))
        return PS.compute_structure(b10, _bars_30min(), _daily())

    def test_01_insufficient_data(self):
        st = PS.compute_structure(pd.DataFrame({"dt": [], "open": [], "high": [],
                                                "low": [], "close": []}),
                                  _bars_30min(), _daily())
        self.assertFalse(st["ok"])
        self.assertIn("数据不足", st["reason"])

    def test_02_ok_and_contract(self):
        st = self._build()
        self.assertTrue(st["ok"], st.get("reason"))
        self.assertIn(st["stage10"], PS.STAGE_LABEL.values())
        self.assertIn(st["mid_trend_30"], PS.TREND_LABEL.values())
        self.assertEqual(st["stop_basis"], "10min 最近已确认摆动低点")
        if st["discount_score"] is not None:
            self.assertGreaterEqual(st["discount_score"], 0.0)
            self.assertLessEqual(st["discount_score"], 100.0)

    def test_03_refill_tiers_below_stop_only(self):
        """补仓档**只**能在止损位下方（上方档位自动作废）——这是模块的核心纪律。"""
        st = self._build()
        stop = st["stop_level"]
        self.assertIsNotNone(stop)
        for t in st["refill_tiers"]:
            self.assertLess(t["price"], stop, f"{t['name']} 必须严格低于止损位 {stop}")
            self.assertLess(t["dist_from_stop_pct"], 0)
            self.assertIn(t["name"], PS.TIER_FILL_RATE)
        # 档位次序：距止损越近的档位在前（T1/T2/T3 按深度）
        d = [t["dist_from_stop_pct"] for t in st["refill_tiers"]]
        self.assertEqual(d, sorted(d, reverse=True))

    def test_05_ma5_30min_fields_and_causality(self):
        """30min MA5 是**纯展示项**：值必须等于最后 5 根 30min 收盘均值（只用当根及之前）。"""
        b30 = _bars_30min()
        st = self._build()
        tail5 = float(b30["close"].tail(5).mean())
        self.assertAlmostEqual(st["ma5_30min"], round(tail5, 3), places=3)
        self.assertEqual(st["above_ma5_30"], bool(b30["close"].iloc[-1] > tail5))
        # "距上次翻转几根"用独立算法复核
        c = b30["close"].to_numpy(float)
        ma5 = pd.Series(c).rolling(PS.MA_FAST_30).mean().to_numpy()
        fin = ~np.isnan(ma5)
        s = int(np.flatnonzero(fin)[0])
        abv = c[s:] > ma5[s:]
        flips = np.flatnonzero(abv[1:] != abv[:-1])
        want = (len(abv) - 1 - int(flips[-1])) if len(flips) else None
        self.assertEqual(st["bars_since_ma5_cross"], want)

    def test_06_ma5_must_carry_no_forecasting_label(self):
        """纪律守卫：纯展示项**必须**自带"无前瞻信息量"标注，防日后被当成信号用。"""
        txt = PS.format_structure(self._build())
        self.assertIn("MA5(30min)", txt)
        self.assertIn("无前瞻信息量", txt)

    def test_04_causality_smoke_truncation(self):
        """截断不变性烟雾测试：末根不影响更早的 stage（无未来函数）。"""
        g = PS.resample_5min_to_10min(_bars_5min(days=6))
        h, l, c = (g["high"].to_numpy(float), g["low"].to_numpy(float),
                   g["close"].to_numpy(float))
        full = PS._stage10(c, h, l)["stage"]
        T = len(g) - 3
        part = PS._stage10(c[:T], h[:T], l[:T])["stage"]
        self.assertTrue((full[:T] == part).all(), "截断重算后 T 之前的阶段必须逐位相同")


class TestFillRate(unittest.TestCase):
    def test_01_monotone_decreasing(self):
        ds = [0.010, 0.015, 0.020, 0.025, 0.030, 0.035, 0.040, 0.050, 0.060]
        rs = [PS.expected_fill_rate(-d * 100) for d in ds]
        self.assertEqual(rs, sorted(rs, reverse=True), f"越深成交率越低: {rs}")

    def test_02_floor_and_bounds(self):
        self.assertGreaterEqual(PS.expected_fill_rate(-99.0), 8.0, "深档有 8% 地板")
        self.assertLessEqual(PS.expected_fill_rate(-0.1), 100.0)
        self.assertEqual(PS.expected_fill_rate(-1.0), PS.expected_fill_rate(1.0),
                         "只看距离，符号无关")


class TestFeed(unittest.TestCase):
    def test_01_ts_code_of(self):
        self.assertEqual(F.ts_code_of("002176"), "002176.SZ")
        self.assertEqual(F.ts_code_of("sh600584"), "600584.SH")
        self.assertEqual(F.ts_code_of("600584.SH"), "600584.SH")
        self.assertEqual(F.ts_code_of("830799"), "830799.BJ")
        self.assertEqual(F.ts_code_of("bogus"), "")

    def test_02_unknown_code_fail_soft(self):
        st = F.load_structure("bogus")
        self.assertFalse(st["ok"])
        self.assertIn("代码无法识别", st["reason"])

    def test_03_freshness_session_aware(self):
        """盘中要求**当日**根（每 30min 时段重取）；盘后/周末按自然日窗口（不出网）。"""
        def _frame(last_day):
            d = _bars_5min(days=1)
            d["time"] = pd.to_datetime(last_day + " " + d["time"].str[11:])
            return d
        wed = datetime(2026, 10, 7, 10, 30)          # 周三盘中
        self.assertTrue(F._in_session(wed))
        self.assertTrue(F._fresh_enough(_frame("2026-10-07"), now=wed), "含当日根 → 新鲜")
        self.assertFalse(F._fresh_enough(_frame("2026-10-06"), now=wed), "只有昨日根 → 须重取")
        sat = datetime(2026, 10, 10, 12, 0)          # 周六
        self.assertFalse(F._in_session(sat))
        self.assertTrue(F._fresh_enough(_frame("2026-10-09"), now=sat), "周末周五根仍新鲜")
        self.assertFalse(F._fresh_enough(_frame("2026-09-01"), now=sat), "太旧 → 不新鲜")
        self.assertFalse(F._fresh_enough(pd.DataFrame(), now=sat), "空帧 → 不新鲜")

    def test_04_load_structure_offline_monkeypatched(self):
        """全程 monkeypatch：不碰网络、不碰 provider。"""
        b5, b30, dly = _bars_5min(), _bars_30min(), _daily()
        now_s = datetime.now().strftime("%Y-%m-%d ")
        for b in (b5, b30):
            b["time"] = pd.to_datetime(now_s + b["time"].str[11:])   # 当作新鲜
        saved = cc.load_minute_asof
        cc.load_minute_asof = lambda ts, freq, date_str=None, keep=None: (
            b5 if freq == "5min" else b30)
        try:
            st = F.load_structure("002176", daily_df=dly)
        finally:
            cc.load_minute_asof = saved
        self.assertTrue(st["ok"], st.get("reason"))
        self.assertIn("text", st, "须附展示文本（tooltip 单一真源）")
        a = st["data_asof"]
        for k in ("bars10_last", "bars30_last", "daily_last", "n_bars10", "n_bars30",
                  "n_daily", "src_5min", "src_30min", "stale", "stale_reason"):
            self.assertIn(k, a)
        self.assertFalse(a["stale"], f"当日数据不应标陈旧: {a['stale_reason']}")

    def test_05_historical_marks_stale_when_data_lags(self):
        """历史日：数据止于该日之前 ⇒ 必须显式标 stale（防静默截短）。"""
        b5, b30 = _bars_5min(), _bars_30min()
        saved = cc.load_minute_asof
        cc.load_minute_asof = lambda ts, freq, date_str=None, keep=None: (
            b5 if freq == "5min" else b30)
        try:
            # 合成数据在 2026-10-09 结束；请求 2026-12-31 ⇒ 应标陈旧
            st = F.load_structure("002176", date_str="2026-12-31",
                                  allow_network=False, daily_df=_daily())
        finally:
            cc.load_minute_asof = saved
        self.assertTrue(st["ok"], st.get("reason"))
        self.assertTrue(st["data_asof"]["stale"])
        self.assertIn("早于目标日", st["data_asof"]["stale_reason"])


class TestGuiWiring(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._saved = dict(t_gui._PB_STRUCT_CACHE)
        t_gui._PB_STRUCT_CACHE.clear()

    def tearDown(self):
        t_gui._PB_STRUCT_CACHE.clear()
        t_gui._PB_STRUCT_CACHE.update(self._saved)

    def test_01_cold_is_pending_no_compute(self):
        """冷缓存必须返回 pending —— 主线程零计算零网络（防 GUI 冻结）。"""
        s = self.api._pb_struct_get("002176")
        self.assertTrue(s["pending"])
        self.assertFalse(s["ok"])

    def test_02_warm_returns_cached(self):
        key = f"002176_{t_gui._min_bars_slot()}"
        t_gui._PB_STRUCT_CACHE[key] = {"ok": True, "stop_level": 8.48, "text": "x"}
        s = self.api._pb_struct_get("002176")
        self.assertTrue(s["ok"])
        self.assertEqual(s["stop_level"], 8.48)

    def test_03_hist_not_computed_on_browse(self):
        """浏览历史日不得触发计算（compute=False → None）。"""
        self.assertIsNone(self.api._pb_struct_hist("002176", "2026-09-18", compute=False))

    def test_04_hist_caches_result(self):
        key = "002176_2026-09-18"
        t_gui._PB_STRUCT_HIST.pop(key, None)
        try:
            s = self.api._pb_struct_hist("002176", "2026-09-18", compute=True)
            self.assertIsNotNone(s)                 # 磁盘有无数据都要有结论
            self.assertIn(key, t_gui._PB_STRUCT_HIST, "结论须入缓存（浏览历史不重算）")
            self.assertIs(self.api._pb_struct_hist("002176", "2026-09-18"), s)
        finally:
            t_gui._PB_STRUCT_HIST.pop(key, None)


def _chart_payload(n=60):
    """合成 `_stock_chart_cache` 的 payload（持仓体检用；离线、零网络）。"""
    dates, ohlc, vol, px = [], [], [], 10.0
    for i in range(n):
        px += 0.05 if (i % 6) < 3 else -0.04
        dates.append(f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}")
        ohlc.append([px - 0.01, px, px - 0.03, px + 0.03])   # open,close,low,high
        vol.append(1000.0)
    return {"available": True, "channel": {"direction": "up"},
            "period_data": {"daily": {"dates": dates, "ohlc": ohlc, "volume": vol,
                                      "rsi": [50.0] * n, "boll": {"up": [px + 1] * n}}}}


def _daily_series(n=120, start=100.0, drift=0.0):
    px, rows = start, []
    for i in range(n):
        px += drift
        rows.append({"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "open": px,
                     "high": px + 0.5, "low": px - 0.5, "close": px, "volume": 1e5})
    return pd.DataFrame(rows)


class TestStageDaily(unittest.TestCase):
    """日线四阶段（生产单一真源）；研究脚本直接 import 同一函数。"""

    def test_01_insufficient(self):
        r = PS.stage_daily(_daily_series(30))
        self.assertFalse(r["ok"])
        self.assertIsNone(r["stage_daily"])

    def test_02_trending_series(self):
        self.assertEqual(PS.stage_daily(_daily_series(120, drift=+0.8))["stage_daily"], "②抬升")
        self.assertEqual(PS.stage_daily(_daily_series(120, drift=-0.8))["stage_daily"], "④下跌")
        r = PS.stage_daily(_daily_series(120, drift=0.0))          # 恒定价 → 均线全等 → ①蓄势
        self.assertEqual(r["stage_daily"], "①蓄势")

    def test_03_distribution_after_rise_then_break(self):
        """先涨后急跌：MA20 仍 > MA60，但收盘丢了 MA20 ⇒ ③派发。"""
        d = _daily_series(100, drift=+0.9)                          # 涨到 ~190（日期止于 2026-04）
        # ⚠️ 追加段的日期必须**严格晚于**原序列：函数按 date 排序，日期撞车会把急跌插到中间
        tail = pd.DataFrame([{"date": f"2026-06-{i+1:02d}", "open": 190 - i * 8,
                              "high": 191 - i * 8, "low": 189 - i * 8,
                              "close": 190 - i * 8, "volume": 1e5} for i in range(1, 7)])
        r = PS.stage_daily(pd.concat([d, tail], ignore_index=True))
        self.assertEqual(r["stage_daily"], "③派发", f"实际 {r}")

    def test_04_definition_pin_no_future_data(self):
        """定义钉：逐点复算必须与函数一致 ⇒ ①规则未被意外改 ②只用 ≤t 的数据。"""
        d = _daily_series(160, drift=+0.35)
        d.loc[120:135, "close"] = d.loc[120:135, "close"] - 30      # 制造一段回撤 → 会出现不同阶段
        seen = set()
        for i in (80, 100, 120, 140, 159):
            sub = d.iloc[:i + 1]
            c = sub["close"].astype(float)
            ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
            m20, m60, px = float(ma20.iloc[-1]), float(ma60.iloc[-1]), float(c.iloc[-1])
            up = m20 > float(ma20.iloc[-6])
            want = 1
            if px > m20 > m60 and up:
                want = 2
            if m20 > m60 and px < m20:
                want = 3
            if px < m20 < m60 and m20 < float(ma20.iloc[-6]):
                want = 4
            got = PS.stage_daily(sub)["stage_daily_code"]
            self.assertEqual(got, want, f"i={i}: 函数={got} 复算={want}")
            seen.add(got)
        self.assertGreaterEqual(len(seen), 2, f"样本应覆盖多个阶段，实际 {seen}")


class TestFormingBarPlaceholderGuard(unittest.TestCase):
    """盘前快照造出的「假当日根」不得污染标签（2026-10-11 事故：600362 误报破5日线）。

    集合竞价后开盘前（如 09:28）快照会给 price=开盘价、vol=0 ⇒ 造出 O=H=L=C、volume=0 的根。
    实测 600362：假根 close=41.5 ⇒ cur(41.5) < MA5(41.57) ⇒ 误报「破5日线」，
    而真实 10-09 收盘 42.51 是**站上**。
    """

    def test_01_writer_rejects_preopen_placeholder(self):
        self.assertIsNone(t_gui.Api._mk_forming_bar("2026-10-09", 41.5, 41.5, 41.5, 41.5, 0),
                          "盘前占位(O=H=L=C 且 vol=0)必须拒绝")
        self.assertIsNone(t_gui.Api._mk_forming_bar("2026-10-09", 41.5, 41.5, 41.5, 41.5, None))

    def test_02_writer_keeps_real_intraday_bars(self):
        # 正常盘中
        self.assertIsNotNone(t_gui.Api._mk_forming_bar("2026-10-09", 42.0, 41.5, 42.6, 41.4, 12000))
        # 一字板：O=H=L=C 但**有成交** ⇒ 必须放行（否则会误杀）
        self.assertIsNotNone(t_gui.Api._mk_forming_bar("2026-10-09", 41.5, 41.5, 41.5, 41.5, 500))
        # 缺 vol 但有振幅 ⇒ 放行（不能因字段缺失误杀）
        self.assertIsNotNone(t_gui.Api._mk_forming_bar("2026-10-09", 42.0, 41.5, 42.6, 41.4, None))

    def test_03_consumer_heals_bad_payload(self):
        """磁盘上**已存在**的坏 payload 也要能自愈（只修写入侧不够）。"""
        api = t_gui.Api()
        rows = [{"date": f"2026-09-{20+i:02d}", "open": 45.0 - i * 0.8, "high": 45.2 - i * 0.8,
                 "low": 44.8 - i * 0.8, "close": 45.0 - i * 0.8, "volume": 1e5} for i in range(9)]
        rows += [{"date": "2026-10-08", "open": 42.03, "high": 42.65, "low": 41.58,
                  "close": 41.89, "volume": 2.6e5}]
        # 假 10-09：O=H=L=C=41.5、vol=0（真实收盘 42.51）
        rows += [{"date": "2026-10-09", "open": 41.5, "high": 41.5, "low": 41.5,
                  "close": 41.5, "volume": 0.0}]
        df = pd.DataFrame(rows)
        labels = [t["label"] for t in api._stock_tags_from_df(df, "600362", t30=False)["tags"]]
        self.assertNotIn("破5日线", labels, f"假占位根必须被剔除，实际 {labels}")

    def test_04_consumer_keeps_interior_zero_volume(self):
        """只剔**尾部**：中间的零成交根可能是真实历史（停牌等），不得连带剔除。"""
        api = t_gui.Api()
        rows = [{"date": f"2026-09-{20+i:02d}", "open": 40.0 + i, "high": 40.5 + i,
                 "low": 39.5 + i, "close": 40.0 + i, "volume": 1e5} for i in range(9)]
        rows.insert(4, {"date": "2026-09-24", "open": 44.0, "high": 44.0, "low": 44.0,
                        "close": 44.0, "volume": 0.0})          # 中间零成交根
        rows.append({"date": "2026-10-09", "open": 60.0, "high": 61.0, "low": 59.0,
                     "close": 60.5, "volume": 2e5})             # 末尾是真根
        df = pd.DataFrame(rows)
        n_drop = len(df)
        api._stock_tags_from_df(df, "600362", t30=False)
        self.assertEqual(len(df), n_drop, "不得就地修改调用方的 df")


class TestSectorTier(unittest.TestCase):
    """板块内 龙头/中军/杂毛（**描述性**，实测无前瞻区分度；见 exp1/exp2）。"""

    @staticmethod
    def _mk(pairs):
        return [{"code": f"{i:06d}", "name": f"n{i}", "change_pct": c, "amount": a,
                 "score": 100 - i} for i, (c, a) in enumerate(pairs)]

    def test_01_no_fixed_count_multi_leaders(self):
        """**多龙头**：并列涨停的两只都应标龙头（不硬凑 1 个）。"""
        st = self._mk([(10.0, 16.8e8), (10.0, 4.3e8), (2.0, 39.5e8), (1.0, 37.2e8),
                       (0.0, 30e8), (-1.0, 5e8), (-2.0, 4e8), (-3.0, 3e8)])
        t_gui._tier_sector_stocks(st)
        leads = [s for s in st if s["tier"] == "龙头"]
        self.assertEqual(len(leads), 2, f"并列最强应有两个龙头，实际 {[s['tier'] for s in st]}")
        mids = [s for s in st if s["tier"] == "中军"]
        self.assertTrue(all(s["amount"] >= 30e8 for s in mids), "中军须达成交额门槛")

    def test_02_no_leader_when_flat(self):
        """**无龙头**：板块整体没行情时不硬凑（且整块未分层 ⇒ 全不标，不是全打杂毛）。"""
        st = self._mk([(1.1, 19.6e8), (-4.5, 18.2e8), (-5.0, 16.5e8), (-9.9, 11.3e8),
                       (-7.5, 10e8), (-3.6, 3e8), (-2.0, 2e8), (-1.0, 1e8)])
        t_gui._tier_sector_stocks(st)
        self.assertNotIn("龙头", [s["tier"] for s in st], "最高 +1.1% 不应硬凑龙头")
        self.assertNotIn("杂毛", [s["tier"] for s in st],
                         "整个板块都没分层 ⇒ 不得全打杂毛（它们只是成员）")

    def test_03_mid_can_be_zero(self):
        """中军也可能 0 个（成交额都够不上门槛）——不硬凑。"""
        st = self._mk([(12.0, 10e8), (1.0, 1.1e8), (0.5, 1.0e8), (0.2, 0.9e8), (0.1, 0.8e8)])
        t_gui._tier_sector_stocks(st)
        tiers = [s["tier"] for s in st]
        self.assertEqual(tiers.count("龙头"), 1)
        self.assertEqual(tiers.count("中军"), 0, f"不该硬凑中军: {tiers}")

    def test_04_lead_band_uses_gap_not_only_limit_up(self):
        """龙头不只是"涨停"：科创板/创业板涨停 20%，按涨停判会漏掉 +14% 的最强者。"""
        st = self._mk([(14.1, 10.2e8), (2.0, 109e8), (1.0, 100e8), (0.5, 4e8),
                       (0.2, 3e8), (0.1, 2e8)])
        t_gui._tier_sector_stocks(st)
        self.assertEqual(st[0]["tier"], "龙头", "+14.1% 未涨停也应是龙头")

    def test_05_small_sector_unlabeled(self):
        st = self._mk([(10.0, 9e8)] * 4)
        t_gui._tier_sector_stocks(st)
        self.assertEqual([s["tier"] for s in st], [""] * 4, "成分不足不得标")

    def test_06_missing_amount_graceful(self):
        st = [{"code": str(i), "change_pct": 1.0, "amount": 0} for i in range(9)]
        t_gui._tier_sector_stocks(st)
        self.assertEqual([s["tier"] for s in st], [""] * 9, "无成交额不得乱标")

    def test_07_does_not_touch_score_or_order(self):
        """纪律守卫：分层**只写 tier**，不得改动 得分/顺序 —— 它没有前瞻证据，不能影响排序。"""
        st = self._mk([(9.9, 1e8), (5.0, 9e8), (3.0, 8e8), (1.0, 7e8), (-2.0, 5e6),
                       (0.5, 4e6), (-1.0, 3e6), (0.1, 2e6), (0.2, 1e6)])
        before = [(s["code"], s["score"]) for s in st]
        t_gui._tier_sector_stocks(st)
        self.assertEqual([(s["code"], s["score"]) for s in st], before,
                         "分层不得改动顺序或任何既有字段（只加 tier）")
        self.assertTrue(all("tier" in s for s in st))


class TestOfflineTagPath(unittest.TestCase):
    """猎手批量标签的**零网络日线**路径（2026-10-11 提速：150 只 44s → 当前页 50 只 4.5s）。

    根因：`facade.daily()` GM 优先，而 GM 池是 `max_workers=1` ⇒ 逐只 GM 调用**全串行**，
    6 并发形同虚设。故批量视图改读**本地日线缓存**。本测试守卫：offline 路径**不得**
    回退到 `fetch_daily_kline`（逐只 GM）或打实时快照（`_live_quote_forming`，~1.9s/只）。
    """

    def _api_with_cache(self, rows):
        api = t_gui.Api()
        api._daily_rows_cache_first = lambda code: rows
        # 趋势口径另说，本测试只测"日线来源"；注意生产代码解包 (trend, src)
        api._trend30_trend = lambda code: ("flat", "stub")
        return api

    @staticmethod
    def _rows(n=40):
        return [{"date": f"2026-{9 + i // 28:02d}-{i % 28 + 1:02d}", "open": 10.0 + i * 0.05,
                 "close": 10.0 + i * 0.05, "high": 10.2 + i * 0.05, "low": 9.8 + i * 0.05,
                 "volume": 1e5} for i in range(n)]

    def test_offline_does_not_fall_back_to_network(self):
        import core.position_builder as _pb
        api = self._api_with_cache(self._rows())
        calls = []
        orig_fetch, orig_live = _pb.fetch_daily_kline, api._live_quote_forming
        _pb.fetch_daily_kline = lambda c: calls.append(("fetch_daily_kline", c)) or pd.DataFrame()
        api._live_quote_forming = lambda c: calls.append(("live_snapshot", c)) or None
        try:
            r = api._stock_tags_one("600000", offline=True)
        finally:
            _pb.fetch_daily_kline, api._live_quote_forming = orig_fetch, orig_live
        self.assertEqual(calls, [], f"offline 路径不得出网：{calls}")
        self.assertIn("tags", r)

    def test_online_still_appends_forming_bar(self):
        """offline=False（建仓表/体检）仍走实时快照补当日 bar —— 行为不变。"""
        api = self._api_with_cache(self._rows())
        calls = []
        api._live_quote_forming = lambda c: calls.append(c) or None
        api._stock_tags_one("600000", offline=False)
        self.assertEqual(calls, ["600000"], "在线路径应尝试补当日 forming bar")


class TestStageRuleSingleSource(unittest.TestCase):
    """`build_decision.features_from_daily` 内联了一份日线阶段规则（因 goldminer 以绝对路径
    importlib 加载该文件，不能反向依赖 core 包）⇒ **必须**与 `position_structure.stage_daily`
    逐点一致。本测试就是那条守卫；两边任一改动不一致会在这里红。"""

    def test_inlined_rule_matches_stage_daily(self):
        import core.build_decision as bd
        # 覆盖四种阶段：上行 / 下行 / 恒定价(缠绕) / 先涨后急跌
        rise = _daily_series(150, drift=+0.7)
        fall = _daily_series(150, drift=-0.7)
        flat = _daily_series(150, drift=0.0)
        topped = pd.concat([_daily_series(120, drift=+0.9),
                            pd.DataFrame([{"date": f"2026-07-{i+1:02d}", "open": 210 - i * 9,
                                           "high": 211 - i * 9, "low": 209 - i * 9,
                                           "close": 210 - i * 9, "volume": 1e5}
                                          for i in range(1, 7)])], ignore_index=True)
        for name, df in (("rise", rise), ("fall", fall), ("flat", flat), ("topped", topped)):
            last = str(df["date"].iloc[-1])
            a = bd.features_from_daily(df, last).get("stage_daily_code")
            b = PS.stage_daily(df)["stage_daily_code"]
            self.assertEqual(a, b, f"{name}: build_decision={a} vs position_structure={b}")
        # 且四种阶段确实都被覆盖到（否则这个测试等于没测）
        codes = {bd.features_from_daily(df, str(df["date"].iloc[-1])).get("stage_daily_code")
                 for df in (rise, fall, flat, topped)}
        self.assertGreaterEqual(len(codes), 3, f"样本应覆盖多个阶段，实际 {codes}")


class TestPanelWiring(unittest.TestCase):
    """持仓体检(OB) 与 猎手板块 复用同一结构层（同一真源、同一渲染）。"""

    def setUp(self):
        self.api = t_gui.Api()
        self._saved_cache = dict(t_gui._PB_STRUCT_CACHE)
        self._saved_warm = dict(t_gui._PB_STRUCT_WARM)
        self._saved_load_json = t_gui._load_json
        t_gui._PB_STRUCT_CACHE.clear()
        t_gui._PB_STRUCT_WARM.clear()
        # 持仓：一只；图表缓存：命中（否则 OB 会记 pending 并跳过，测不到注入）
        self._today = datetime.now().strftime("%Y-%m-%d")
        self.api._stock_chart_cache = {f"{self._today}_000001": (0, _chart_payload())}
        _orig = self._saved_load_json

        def _fake(fp, default=None):
            if str(fp) == str(t_gui.HOLDINGS_MANUAL):
                return {"000001": {"name": "测试甲", "qty": 100}}
            return _orig(fp, default)

        t_gui._load_json = _fake

    def tearDown(self):
        t_gui._load_json = self._saved_load_json
        t_gui._PB_STRUCT_CACHE.clear()
        t_gui._PB_STRUCT_CACHE.update(self._saved_cache)
        t_gui._PB_STRUCT_WARM.clear()
        t_gui._PB_STRUCT_WARM.update(self._saved_warm)

    def test_01_ob_row_carries_structure_from_same_cache(self):
        """OB 行的 structure 必须来自**同一个** `_PB_STRUCT_CACHE`（与建仓表同源）。"""
        key = f"000001_{t_gui._min_bars_slot()}"
        t_gui._PB_STRUCT_CACHE[key] = {"ok": True, "stop_level": 9.87,
                                       "stage10": "①蓄势", "mid_trend_30": "走平", "text": "x"}
        ob = self.api._load_ob_analysis_impl()
        self.assertTrue(ob["stocks"], f"应产出 1 行持仓，实际 {len(ob['stocks'])}")
        st = ob["stocks"][0]["structure"]
        self.assertTrue(st["ok"])
        self.assertEqual(st["stop_level"], 9.87)
        self.assertNotIn("struct_pending", ob, "已热不应标 pending")

    def test_02_ob_pending_kicks_warm(self):
        """未热 ⇒ 标 struct_pending 并起后台预热（主线程不出网）。"""
        calls = []
        orig = self.api._kick_pb_struct_warm
        self.api._kick_pb_struct_warm = lambda codes, force=False: calls.append(list(codes))
        try:
            ob = self.api._load_ob_analysis_impl()
        finally:
            self.api._kick_pb_struct_warm = orig
        self.assertTrue(ob.get("struct_pending"))
        self.assertTrue(any("000001" in c for c in calls), f"应预热持仓代码: {calls}")

    def test_03_batch_contract(self):
        """批量接口：只回已热的 + 报告 pending；空入参安全。"""
        r = self.api.load_structure_batch(["000001", "000002"])
        self.assertEqual(r["structures"], {})
        self.assertEqual(r["pending"], 2)
        t_gui._PB_STRUCT_CACHE[f"000001_{t_gui._min_bars_slot()}"] = {"ok": True, "stop_level": 1.0}
        r2 = self.api.load_structure_batch(["000001", "000002"])
        self.assertEqual(list(r2["structures"].keys()), ["000001"])
        self.assertEqual(r2["pending"], 1)
        for bad in ([], None):
            self.assertEqual(self.api.load_structure_batch(bad), {"structures": {}, "pending": 0})

    def test_05_hist_warm_is_async_and_capped(self):
        """历史日结构预热**必须异步**：它单只中位 0.72s、50 只 ≈55s，
        同步跑在 `recompute_pb` 请求里会打爆前端 20s 投递层上限（2026-10-11 实测报错）。"""
        seen = []

        def _fake(code, date, compute=False):
            seen.append((_th.current_thread().name, code, date))
            _time_mod.sleep(0.05)
            return None

        orig = self.api._pb_struct_hist
        self.api._pb_struct_hist = _fake
        try:
            codes = [f"{i:06d}" for i in range(200)]
            t0 = _time_mod.time()
            self.api._kick_pb_struct_hist_warm(codes, "2026-10-09")
            elapsed = _time_mod.time() - t0
            self.assertLess(elapsed, 0.5, f"必须立刻返回（实际 {elapsed:.2f}s）")
            _time_mod.sleep(4.0)                     # 等后台线程跑完（200 只 × 0.05s / 2 workers）
        finally:
            self.api._pb_struct_hist = orig
        self.assertTrue(seen, "应触发后台预热")
        self.assertTrue(all(t != "MainThread" for t, _, _ in seen),
                        f"不得在主线程跑：{ {t for t, _, _ in seen} }")
        self.assertLessEqual(len(seen), t_gui._PB_STRUCT_POOL_MAX,
                             f"预热池必须有上限，实际 {len(seen)}")
        self.assertTrue(all(d == "2026-10-09" for _, _, d in seen), "日期须透传")

    def test_04_batch_bounds_warm_pool(self):
        """猎手池可达数千只 ⇒ 预热必须有上限，绝不整池排队。"""
        seen = []
        orig = self.api._kick_pb_struct_warm
        self.api._kick_pb_struct_warm = lambda codes, force=False: seen.append(len(codes))
        try:
            codes = [f"{i:06d}" for i in range(3000)]
            self.api.load_structure_batch(codes)
        finally:
            self.api._kick_pb_struct_warm = orig
        self.assertTrue(seen, "应触发预热")
        self.assertLessEqual(seen[0], t_gui._PB_STRUCT_POOL_MAX, f"预热池未设上限: {seen[0]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
