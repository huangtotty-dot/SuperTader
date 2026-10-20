# -*- coding: utf-8 -*-
"""日线「今天那一根」必须用实时快照覆盖（2026-10-20，owner 报 688003 天准科技）。

现象：天准科技当天已站上 10 日均线，但日线图把它画在线下方。
根因：日线缓存/payload 里的当日根是**开盘时写下的半成品**（688003 实测 09:31 落盘
close=61.08 / high=61.99），而新鲜度闸只看 `date == 今天` ⇒ 一整天不再更新；
MA5/MA10/MA20、current_price、支撑压力全部按 9 小时前的价算（真实 66.42 / 67.98）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import t_gui   # noqa: E402


def _df(last_date, close):
    rows = [{"date": "2026-10-16", "open": 62.2, "high": 62.4, "low": 59.7, "close": 60.6, "volume": 25846.0},
            {"date": "2026-10-19", "open": 60.0, "high": 60.9, "low": 59.0, "close": 59.4, "volume": 366354.0},
            {"date": last_date, "open": 61.73, "high": 61.99, "low": 60.61, "close": close, "volume": 210898.0}]
    return pd.DataFrame(rows)


@pytest.fixture
def api(monkeypatch):
    a = t_gui.Api()
    monkeypatch.setattr(a, "_daily_patch_memo", {}, raising=False)
    return a


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class TestPatchDailyToday:
    def test_replaces_stale_close_with_live(self, api):
        """末根==今天 ⇒ 用快照的 OHLCV 覆盖那根半成品（这就是"站上10日线却显示在下方"的修复）。"""
        live = {"date": _today(), "open": 61.73, "high": 67.98, "low": 60.61,
                "close": 66.42, "volume": 6549107.0}
        api._today_forming_bar = lambda code, allow_network=True: live
        out = api._patch_daily_today(_df(_today(), 61.08), "688003")
        row = out.iloc[-1]
        assert float(row["close"]) == pytest.approx(66.42)
        assert float(row["high"]) == pytest.approx(67.98)
        assert float(row["volume"]) == pytest.approx(6549107.0)
        # MA10 随之改变：旧的 61.08 会让人以为"在线下"
        closes = out["close"].astype(float).values
        assert closes[-1] > sum(closes[-10:]) / min(10, len(closes))

    def test_leaves_yesterday_bar_alone(self, api):
        """末根不是今天（还没开盘/停牌）⇒ 一根都不许动，更不许造根。"""
        called = {"n": 0}

        def _boom(code, allow_network=True):
            called["n"] += 1
            return {"date": _today(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        api._today_forming_bar = _boom
        df = _df("2026-10-19", 59.44)
        out = api._patch_daily_today(df, "688003")
        assert called["n"] == 0
        assert float(out.iloc[-1]["close"]) == pytest.approx(59.44)

    def test_no_snapshot_keeps_original(self, api):
        """快照拿不到 ⇒ 原样返回（宁可显示缓存值，也不猜）。"""
        api._today_forming_bar = lambda code, allow_network=True: None
        out = api._patch_daily_today(_df(_today(), 61.08), "688003")
        assert float(out.iloc[-1]["close"]) == pytest.approx(61.08)

    def test_memo_avoids_repeat_snapshot(self, api):
        """同票 20 秒内复用快照 —— 体检会逐只调本函数，不能每次都打一次网络。"""
        calls = {"n": 0}
        live = {"date": _today(), "open": 61.73, "high": 67.98, "low": 60.61,
                "close": 66.42, "volume": 1.0}

        def _snap(code, allow_network=True):
            calls["n"] += 1
            return live
        api._today_forming_bar = _snap
        for _ in range(3):
            api._patch_daily_today(_df(_today(), 61.08), "688003")
        assert calls["n"] == 1

    def test_weekend_short_circuits(self, api, monkeypatch):
        """周末没有盘中价：直接返回，不打网络。"""
        called = {"n": 0}
        api._today_forming_bar = lambda code, allow_network=True: called.__setitem__("n", called["n"] + 1)
        monkeypatch.setattr(t_gui, "datetime", _FakeSat)
        out = api._patch_daily_today(_df(_today(), 61.08), "688003")
        assert called["n"] == 0
        assert float(out.iloc[-1]["close"]) == pytest.approx(61.08)


class _FakeSat(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 24, 10, 0, 0)      # 周六
