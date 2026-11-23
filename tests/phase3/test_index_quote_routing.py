# -*- coding: utf-8 -*-
"""指数行情/均线取数路由（2026-11-23，全离线）。

实报：多窗口里「科创50 16.44 −1.56%」「上证指数 12.47 +2.05%」——都是**个股价**；
图上均线也被带偏（MA5=(前4日+12.46)/5=2859）。
根因：QuoteBus 把代码归一到裸 6 位，`sh000001`→`000001` 被腾讯按股票补成 `sz000001`
（平安银行）；错误现价又被 `_daily_ma_bundle` 当"今日收盘"掺进 MA。

契约：
  A. 指数（带前缀，sh000/sz399/bj899…）`_fetch_live_quote` **不进 bus**，直连带前缀快照；
  B. 个股照旧走 bus；
  C. `_daily_ma_bundle` 发现指数日线缓存落后 ⇒ kick 一次后台补数（每码 10 分钟一次）。

运行：python -m pytest tests/phase3/test_index_quote_routing.py -q
"""
import os
import sys
from unittest import mock

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gui  # noqa: E402


def test_is_index_code():
    for c in ("sh000001", "sh000688", "sz399106", "sh000905", "bj899050", "sh000001_0"):
        assert gui._is_index_code(c), c
    for c in ("600000", "000001", "300750", "688111"):
        assert not gui._is_index_code(c), c


def test_index_quote_bypasses_bus_and_uses_prefixed_snapshot(monkeypatch):
    api = gui.Api()
    bus_calls, snap_calls = [], []

    def _bus(codes, max_age_sec=8.0):
        bus_calls.append(list(codes))
        return {"000001": {"price": 12.46, "pct": 1.96}}      # 裸码→平安银行（错误路径）

    def _snap(self, codes):
        snap_calls.append(list(codes))
        return {"000001": {"price": 3481.35, "pct": -1.48, "vol_hand": 1.0,
                           "pre_close": 3533.79, "ts_date": "2026-11-23"}}

    monkeypatch.setattr("core.market_data.quote_bus.get_quotes", _bus)
    monkeypatch.setattr("core.market_data.tencent_provider.TencentProvider.snapshot_auction", _snap)
    q = api._fetch_live_quote("sh000001")
    assert not bus_calls, "指数不许进 QuoteBus（裸码会串到个股）"
    assert snap_calls and snap_calls[0] == ["sh000001"], "必须带前缀取快照"
    assert abs(q["price"] - 3481.35) < 1e-6 and abs(q["change_pct"] + 1.48) < 1e-6


def test_stock_quote_still_uses_bus(monkeypatch):
    api = gui.Api()
    bus_calls = []

    def _bus(codes, max_age_sec=8.0):
        bus_calls.append(list(codes))
        return {"600000": {"price": 10.8, "pct": -0.64, "ts_date": "2026-11-23"}}

    monkeypatch.setattr("core.market_data.quote_bus.get_quotes", _bus)
    q = api._fetch_live_quote("600000")
    assert bus_calls == [["600000"]]
    assert abs(q["price"] - 10.8) < 1e-6


def test_daily_ma_bundle_kicks_refresh_when_index_cache_stale(monkeypatch):
    import core.chart_cache as cc
    api = gui.Api()
    api._dma_memo = {}
    kicked = []
    monkeypatch.setattr(gui.Api, "_kick_index_daily_refresh", lambda self, sym: kicked.append(sym))
    df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=120).strftime("%Y-%m-%d"),
                       "close": [2400.0] * 120})
    monkeypatch.setattr(cc, "load_daily_display", lambda c: df)
    api._daily_ma_bundle("sz399006", 2192.0)
    assert kicked == ["sz399006"], "指数缓存末根不是今天 ⇒ 必须 kick 后台补数"

    kicked.clear()
    api._dma_memo = {}
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    df2 = pd.DataFrame({"date": pd.date_range(end=today, periods=120).strftime("%Y-%m-%d"),
                        "close": [2400.0] * 120})
    monkeypatch.setattr(cc, "load_daily_display", lambda c: df2)
    api._daily_ma_bundle("sz399006", 2192.0)
    assert not kicked, "缓存已到今天 ⇒ 不再重复 kick"
