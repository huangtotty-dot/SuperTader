# -*- coding: utf-8 -*-
"""K 线弹窗「10 分钟」+「盘中实时」离线单测（2026-10-17）。**全离线、零网络**。

背景（owner）：①K 线要支持 10 分钟线；②「盘中 K 线的实时显示变得异常重要」。

实测挖出的四层病根（本文件逐一钉住）：
  · tushare `stk_mins` **盘中只回到上一交易日收盘** ⇒ 分钟图盘中是死的；
  · `_fetch_min_bars` 的内存缓存键原是 **30 分钟时段** ⇒ 即便源是活的也冻半小时；
  · 前端版本握手只认 (末日期:根数) ⇒ 当日/当根价格在动但签名不变 ⇒ 回 unchanged 不重画；
  · 10min 合成路径把 `date` 分组列也 rename 成 `time` ⇒ 两个 time 列 ⇒ **永远空**
    （所以 10 分 Tab 之前从未真正可用）。

覆盖：
  · `minute_kline`：腾讯 mkline 行解析（[时间,开,收,高,低,量]）、m10 不支持、空数据
  · `_fetch_min_bars_tencent`：10min 由 m5 合成、**无重复 time 列**、24 根/日
  · `_fetch_min_bars`：tushare 5min 兜底合成同样不炸（回归 date/time 冲突）
  · `load_chart_realtime`：日线补当日 forming bar、分钟档整段替换、2s 缓存
  · `_tx_symbol`：已带 sh/sz 前缀的代码原样返回（指数 K 线）

运行：python -m unittest tests.phase3.test_chart_realtime_20261017
"""
import json
import os
import sys
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import t_gui  # noqa: E402
import core.market_data.tencent_provider as TP  # noqa: E402
from core import chart_cache as cc  # noqa: E402


_DAY1 = "2026-10-15"
_DAY2 = "2026-10-16"


def _mkline_payload(times, base_price=10.0):
    """按腾讯 mkline 的原始形状造 rows：[时间, 开, 收, 高, 低, 量]，时间=右端。"""
    rows = []
    for i, t in enumerate(times):
        o = base_price + i * 0.01
        c = o + 0.02
        rows.append([t.replace("-", "").replace(" ", "").replace(":", ""),
                     f"{o:.2f}", f"{c:.2f}", f"{c + 0.01:.2f}", f"{o - 0.01:.2f}",
                     "1000.00"])
    return json.dumps({"code": 0, "data": {"sh600000": {"m5": rows}}})


def _times_5min(day, n=48):
    """一天 n 根 5min（09:35 → 15:00，右端标签），与腾讯口径一致。"""
    from datetime import datetime as _dt, timedelta as _td
    t_am = _dt.strptime(f"{day} 09:30", "%Y-%m-%d %H:%M")
    t_pm = _dt.strptime(f"{day} 13:00", "%Y-%m-%d %H:%M")
    pts = [t_am + _td(minutes=5 * i) for i in range(1, 25)]
    pts += [t_pm + _td(minutes=5 * i) for i in range(1, 25)]
    return [p.strftime("%Y-%m-%d %H:%M") for p in pts[:n]]


def _frame_5min(day=_DAY1, n=48):
    rows = []
    for t in _times_5min(day, n):
        rows.append({"time": t, "open": 10.0, "high": 10.1, "low": 9.9,
                     "close": 10.0, "volume": 100.0})
    return pd.DataFrame(rows)


class TestMinuteKlineParse(unittest.TestCase):
    def setUp(self):
        self._orig = TP._ifzq_get

    def tearDown(self):
        TP._ifzq_get = self._orig

    def test_parses_rows_in_ohlcv_order(self):
        times = _times_5min(_DAY2, 3)
        TP._ifzq_get = lambda path, timeout=4.0: _mkline_payload(times)
        df = TP.minute_kline("600000", "m5", 640)
        self.assertEqual(list(df.columns), TP._MINUTE_KLINE_COLS)
        self.assertEqual(len(df), 3)
        self.assertEqual(df["time"].iloc[0], f"{_DAY2} 09:35:00")
        # 原始行是 [t, open, close, high, low, volume] —— high/low 不能搞反
        self.assertAlmostEqual(df["open"].iloc[0], 10.00)
        self.assertAlmostEqual(df["close"].iloc[0], 10.02)
        self.assertAlmostEqual(df["high"].iloc[0], 10.03)
        self.assertAlmostEqual(df["low"].iloc[0], 9.99)
        self.assertEqual(df.attrs.get("source"), "tencent_minute")

    def test_m10_is_not_supported_by_tencent(self):
        TP._ifzq_get = lambda path, timeout=4.0: _mkline_payload([])
        self.assertTrue(TP.minute_kline("600000", "m10").empty)

    def test_empty_payload_returns_empty(self):
        TP._ifzq_get = lambda path, timeout=4.0: json.dumps({"data": {}})
        self.assertTrue(TP.minute_kline("600000", "m5").empty)


class TestTencentSymbolPrefix(unittest.TestCase):
    def test_prefixed_code_kept(self):
        """K 线弹窗传指数码 sh000001；`codec.to_tx` 只吃裸 6 位码，不能二次加前缀。"""
        self.assertEqual(TP._tx_symbol("sh000001"), "sh000001")
        self.assertEqual(TP._tx_symbol("sz399001"), "sz399001")
        self.assertEqual(TP._tx_symbol("600000"), "sh600000")


class TestTencent10minSynthesis(unittest.TestCase):
    def setUp(self):
        t_gui._MIN_BARS_CACHE.clear()
        self._orig_mk = TP.minute_kline
        self._orig_tx = t_gui._fetch_min_bars_tencent
        self._orig_online = t_gui._fetch_min_bars_online

    def tearDown(self):
        TP.minute_kline = self._orig_mk
        t_gui._fetch_min_bars_tencent = self._orig_tx
        t_gui._fetch_min_bars_online = self._orig_online
        t_gui._MIN_BARS_CACHE.clear()

    def test_10min_from_m5_has_24_bars_per_day_and_no_dup_columns(self):
        frames = [_frame_5min(d) for d in (_DAY1, _DAY2)]
        TP.minute_kline = lambda code, freq="m5", count=640: pd.concat(
            frames, ignore_index=True)
        df = t_gui._fetch_min_bars_tencent("600000", "10min")
        self.assertFalse(df.empty, "10min 合成结果为空（date/time 重名列又回来了？）")
        self.assertEqual(list(df.columns),
                         ["time", "open", "high", "low", "close", "volume"])
        self.assertEqual(len(df), 48)                     # 24 根/日 × 2 日
        self.assertTrue(str(df["time"].iloc[-1]).endswith("15:00:00"))

    def test_tushare_fallback_path_also_survives(self):
        """回归：`resample_5min_to_10min` 的输出带 `date` 分组列，原样喂 `_norm_min_bars`
        会变成两个 `time` 列 ⇒ `cannot assemble with duplicate keys`（10min 永远空）。"""
        t_gui._fetch_min_bars_tencent = lambda code, freq: pd.DataFrame()
        t_gui._fetch_min_bars_online = lambda ts_code, freq, days: _frame_5min(_DAY1)
        df = t_gui._fetch_min_bars("600000", "10min")
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 24)
        self.assertEqual(list(df.columns),
                         ["time", "open", "high", "low", "close", "volume"])


def _history_df(days=60, end=_DAY1):
    idx = pd.bdate_range(end=end, periods=days)
    return pd.DataFrame({
        "date": idx,
        "open": [10.0] * days, "high": [10.5] * days,
        "low": [9.5] * days, "close": [10.2] * days, "volume": [1000.0] * days,
    })


class TestChartRealtime(unittest.TestCase):
    def setUp(self):
        t_gui._CHART_LIVE_CACHE.clear()
        t_gui._CHART_LIVE_STATE.clear()
        self._orig_session = t_gui._in_trading_session
        self._orig_daily_display = cc.load_daily_display
        self._orig_raw_daily = TP.daily_kline_raw
        self._orig_snap = TP.TencentProvider.snapshot_auction
        t_gui._in_trading_session = lambda now=None: True
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.api = t_gui.Api()

    def tearDown(self):
        t_gui._in_trading_session = self._orig_session
        cc.load_daily_display = self._orig_daily_display
        TP.daily_kline_raw = self._orig_raw_daily
        TP.TencentProvider.snapshot_auction = self._orig_snap
        t_gui._CHART_LIVE_CACHE.clear()
        t_gui._CHART_LIVE_STATE.clear()
        t_gui._CHART_LIVE_TTL = 2.0

    def _fake_quote(self):
        TP.TencentProvider.snapshot_auction = lambda self_, codes: {
            "600000": {"price": 10.9, "pct": 3.5, "open": 10.6, "high": 11.0,
                       "low": 10.5, "pre_close": 10.53, "vol_hand": 12345.0,
                       "ts_date": self.today}}

    def test_daily_series_appends_today_forming_bar(self):
        cc.load_daily_display = lambda code, now=None: _history_df()
        TP.daily_kline_raw = lambda code, days=5, qfq=True, timeout=4.0: pd.DataFrame([{
            "date": self.today, "open": 10.6, "high": 11.0, "low": 10.5,
            "close": 10.9, "volume": 12345.0}])
        self._fake_quote()
        out = self.api._compute_chart_live("600000", "daily")
        self.assertTrue(out["available"], out.get("error"))
        self.assertEqual(out["series"]["dates"][-1], self.today)
        self.assertEqual(out["series"]["ohlc"][-1][1], 10.9)
        self.assertEqual(out["quote"]["price"], 10.9)
        self.assertIn(self.today, out["sig"])

    def test_daily_uses_snapshot_when_raw_daily_has_no_today(self):
        """fqkline 盘中万一不带当日根：用实时快照造 forming bar（与 _mk_forming_bar 同口径）。"""
        cc.load_daily_display = lambda code, now=None: _history_df()
        TP.daily_kline_raw = lambda code, days=5, qfq=True, timeout=4.0: _history_df(
            end=_DAY1).tail(3).assign(date=lambda d: pd.to_datetime(d["date"]))
        self._fake_quote()
        out = self.api._compute_chart_live("600000", "daily")
        self.assertTrue(out["available"], out.get("error"))
        self.assertEqual(out["series"]["dates"][-1], self.today)
        self.assertEqual(out["series"]["ohlc"][-1][1], 10.9)

    def test_preopen_placeholder_bar_is_rejected(self):
        """盘前快照/接口可能给出 O=H=L=C、量=0 的假当日根（曾致「误报破5日线」）⇒ 不许画。"""
        cc.load_daily_display = lambda code, now=None: _history_df()
        TP.daily_kline_raw = lambda code, days=5, qfq=True, timeout=4.0: pd.DataFrame([{
            "date": self.today, "open": 10.9, "high": 10.9, "low": 10.9,
            "close": 10.9, "volume": 0.0}])
        # 快照也只剩一个无成交的占位（高==低、量=0）⇒ 两道闸都应拦住
        TP.TencentProvider.snapshot_auction = lambda self_, codes: {
            "600000": {"price": 10.9, "pct": 0.0, "open": 10.9, "high": 10.9,
                       "low": 10.9, "pre_close": 10.9, "vol_hand": 0.0,
                       "ts_date": self.today}}
        out = self.api._compute_chart_live("600000", "daily")
        self.assertTrue(out["available"], out.get("error"))
        self.assertEqual(out["series"]["dates"][-1], _DAY1,
                         "盘前占位根被画进了图（假 O=H=L=C、量=0）")

    def test_minute_series_with_fake_tencent(self):
        def _fake_tx(code, freq):
            rows = _frame_5min(_DAY2)
            rows["time"] = pd.to_datetime(rows["time"])
            return rows
        orig = t_gui._fetch_min_bars_tencent
        t_gui._fetch_min_bars_tencent = _fake_tx
        try:
            self._fake_quote()
            out = self.api._compute_chart_live("600000", "min30")
            self.assertTrue(out["available"], out.get("error"))
            self.assertEqual(len(out["series"]["dates"]), 48)
            self.assertEqual(out["series"]["dates"][-1], f"{_DAY2} 15:00")
        finally:
            t_gui._fetch_min_bars_tencent = orig

    # ---- SWR 契约（2026-10-18 GUI 卡死专项）----

    def test_swr_returns_immediately_and_workers_in_background(self):
        """`load_chart_realtime` 在 js_api 线程上**不许**取数/算指标：首次 warming，随后缓存命中。"""
        cc.load_daily_display = lambda code, now=None: _history_df()
        TP.daily_kline_raw = lambda code, days=5, qfq=True, timeout=4.0: _history_df().tail(1)
        self._fake_quote()
        import time as _t
        t0 = _t.perf_counter()
        first = self.api.load_chart_realtime("600000", "daily")
        dt = (_t.perf_counter() - t0) * 1000
        self.assertTrue(first.get("warming"), "首次调用应立即回 warming（不能同步取数）")
        self.assertLess(dt, 250, f"首次调用耗时 {dt:.0f}ms —— SWR 失效，js_api 又被同步取数占住")
        # 等后台 worker 落缓存
        for _ in range(100):
            if t_gui._CHART_LIVE_CACHE.get(("600000", "daily")):
                break
            _t.sleep(0.02)
        second = self.api.load_chart_realtime("600000", "daily")
        self.assertTrue(second["available"], second.get("error"))
        self.assertFalse(second.get("warming"))

    def test_swr_does_not_kick_duplicate_workers(self):
        cc.load_daily_display = lambda code, now=None: _history_df()
        TP.daily_kline_raw = lambda code, days=5, qfq=True, timeout=4.0: _history_df().tail(1)
        calls = {"n": 0}

        def _snap(self_, codes):
            calls["n"] += 1
            return {"600000": {"price": 10.9, "pct": 3.5, "ts_date": self.today}}
        TP.TencentProvider.snapshot_auction = _snap
        import time as _t
        self.api.load_chart_realtime("600000", "daily")
        for _ in range(100):
            if t_gui._CHART_LIVE_CACHE.get(("600000", "daily")):
                break
            _t.sleep(0.02)
        # 缓存新鲜 ⇒ 直接回，不再打网络
        self.api.load_chart_realtime("600000", "daily")
        self.assertEqual(calls["n"], 1, "刷新 worker 应单飞/去重")


class TestMinuteSourceOrderAndTushareGuard(unittest.TestCase):
    """2026-10-18 GUI 卡死专项：分钟取数必须**腾讯优先**，tushare 只作兜底且带熔断/硬超时。"""

    def setUp(self):
        t_gui._MIN_BARS_CACHE.clear()
        t_gui._TUSHARE_MIN_STATE["until"] = 0.0
        self._o_session = t_gui._in_trading_session
        self._o_tx = t_gui._fetch_min_bars_tencent
        self._o_online = t_gui._fetch_min_bars_online
        self._o_bounded = t_gui._run_bounded

    def tearDown(self):
        t_gui._in_trading_session = self._o_session
        t_gui._fetch_min_bars_tencent = self._o_tx
        t_gui._fetch_min_bars_online = self._o_online
        t_gui._run_bounded = self._o_bounded
        t_gui._MIN_BARS_CACHE.clear()
        t_gui._TUSHARE_MIN_STATE["until"] = 0.0

    def test_tencent_is_first_even_off_session(self):
        t_gui._in_trading_session = lambda now=None: False
        t_gui._fetch_min_bars_tencent = lambda code, freq: _frame_5min(_DAY1)

        def _boom(*a, **k):
            raise AssertionError("盘后也不该先打 tushare（卡死栈里 3 个 worker 就挂在这）")
        t_gui._fetch_min_bars_online = _boom
        df = t_gui._fetch_min_bars("600000", "30min")
        self.assertFalse(df.empty)

    def test_tushare_timeout_starts_cooldown_and_returns_fast(self):
        t_gui._run_bounded = lambda fn, timeout: (False, None, None)   # 模拟 DNS 卡死超时
        import time as _t
        t0 = _t.perf_counter()
        df = t_gui._fetch_min_bars_online("000001.SZ", "30min", 70)
        dt = _t.perf_counter() - t0
        self.assertTrue(df.empty)
        self.assertLess(dt, 0.5, "超时后必须立刻返回，不能同步等网络")
        self.assertGreater(t_gui._TUSHARE_MIN_STATE["until"], _t.time(), "必须进入冷却")

    def test_cooldown_skips_network_entirely(self):
        import time as _t
        t_gui._TUSHARE_MIN_STATE["until"] = _t.time() + 300
        called = {"n": 0}

        def _should_not_run(fn, timeout):
            called["n"] += 1
            return True, None, None
        t_gui._run_bounded = _should_not_run
        df = t_gui._fetch_min_bars_online("000001.SZ", "30min", 70)
        self.assertTrue(df.empty)
        self.assertEqual(called["n"], 0, "冷却期内不许再发起调用")


class TestInstrumentSearch(unittest.TestCase):
    """手动盘「K线速查」：名称/代码 → 可画 K 线的标的（2026-10-18）。全离线。"""

    def setUp(self):
        t_gui._SEARCH_CACHE.clear()
        t_gui._INSTRUMENT_NAMES["ts"] = 0.0
        t_gui._INSTRUMENT_NAMES["map"] = {}
        self.api = t_gui.Api()
        self._orig_search = TP.search_instruments
        self._orig_names_fn = self.api._instrument_names
        self.api._instrument_names = lambda: {"600362": "江西铜业", "600519": "贵州茅台"}

    def tearDown(self):
        TP.search_instruments = self._orig_search
        self.api._instrument_names = self._orig_names_fn
        t_gui._SEARCH_CACHE.clear()
        t_gui._INSTRUMENT_NAMES["ts"] = 0.0
        t_gui._INSTRUMENT_NAMES["map"] = {}

    def test_local_exact_name_hits_without_network(self):
        calls = {"n": 0}

        def _s(q, limit=12):
            calls["n"] += 1
            return []
        TP.search_instruments = _s
        r = self.api.search_instrument("江西铜业")
        self.assertEqual([x["code"] for x in r["results"]], ["600362"])
        self.assertEqual(r["source"], "local")
        self.assertEqual(calls["n"], 0, "精确名命中不该再打网络")

    def test_bare_code_ambiguity_resolved_by_online(self):
        """000300 既可能是沪深300指数、也可能是场外基金 ⇒ 以在线结果的类型为准。"""
        TP.search_instruments = lambda q, limit=12: [
            {"market": "sh", "code": "000300", "name": "沪深300", "type": "ZS"},
            {"market": "jj", "code": "000300", "name": "某基金", "type": "KJ-HB"},
        ]
        r = self.api.search_instrument("000300")
        self.assertEqual([x["code"] for x in r["results"]], ["sh000300"])
        self.assertEqual(r["excluded"], 1)
        self.assertIn("场外基金", r["note"])

    def test_offline_falls_back_to_bare_code(self):
        TP.search_instruments = lambda q, limit=12: []
        r = self.api.search_instrument("600362")
        self.assertEqual(r["results"][0]["code"], "600362")
        self.assertEqual(r["results"][0]["name"], "江西铜业")

    def test_fund_only_query_gets_helpful_note(self):
        TP.search_instruments = lambda q, limit=12: [
            {"market": "jj", "code": "000001", "name": "华夏成长混合", "type": "KJ"}]
        r = self.api.search_instrument("华夏成长")
        self.assertEqual(r["results"], [])
        self.assertGreaterEqual(r["excluded"], 1)
        self.assertIn("暂无 K 线", r["note"])

    def test_index_query_keeps_market_prefix(self):
        TP.search_instruments = lambda q, limit=12: [
            {"market": "sh", "code": "000300", "name": "沪深300", "type": "ZS"},
            {"market": "sh", "code": "510300", "name": "沪深300ETF", "type": "ETF"},
        ]
        r = self.api.search_instrument("沪深300")
        codes = [x["code"] for x in r["results"]]
        self.assertIn("sh000300", codes)
        self.assertIn("510300", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
