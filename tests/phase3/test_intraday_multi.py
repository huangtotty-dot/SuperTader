# -*- coding: utf-8 -*-
"""多日分时（core.intraday_multi，2026-11-25，全离线）。

口径：
  · 历史日（不含今天）→ GM/新浪 1min；今天 → 腾讯分时（3s 缓存，强实时）；
  · pts 压缩成 [hhmm, px×1000, amt_万元] 整数（过 pywebview 串行桥）；
  · `hist_sig` 用 dict {code: sig} 握手：一致 ⇒ hist=None（只发今日 tail）；
  · js_api 只读内存，网络全在后台单飞线程。

运行：python -m pytest tests/phase3/test_intraday_multi.py -q
"""
import os
import sys

import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.intraday_multi as IM  # noqa: E402


def _df():
    return pd.DataFrame([
        {"time": pd.Timestamp("2026-11-24 09:31:00"), "close": 3556.123, "amount": 1.2e8},
        {"time": pd.Timestamp("2026-11-24 09:32:00"), "close": 3557.0, "amount": 8.5e7},
        {"time": pd.Timestamp("2026-11-25 09:31:00"), "close": 3500.0, "amount": 9.9e8},
    ])


class TestNorm:
    def test_压缩为整数且按分钟升序去重(self):
        pts = IM._norm_pts([("09:32", 3557.0, 8.5e7), ("09:31", 3556.123, 1.2e8),
                            ("09:31", 3556.2, 1.3e8), ("bad", 1, 1)])
        assert [p[0] for p in pts] == [931, 932]
        # 同分钟后者覆盖前者（保留最后一次）
        assert pts[0][1] == 3556200   # 3556.2 ×1000
        assert pts[0][2] == 13000     # 1.3e8 元 → 13000 万元
        assert pts[1] == [932, 3557000, 8500]

    def test_非法价丢弃(self):
        assert IM._norm_pts([("09:31", 0, 1e8), ("09:32", -1, 1e8)]) == []


class TestParse:
    def test_按日分组(self):
        got = IM._parse_hist_df(_df())
        assert set(got) == {"2026-11-24", "2026-11-25"}
        assert len(got["2026-11-24"]) == 2
        assert got["2026-11-25"][0][0] == 931


class TestGetCached:
    def _env(self, monkeypatch):
        state = {"hist_kick": [], "today_kick": [], "hist": {}, "today": {}}
        monkeypatch.setattr(IM, "_hist", {})
        monkeypatch.setattr(IM, "_today", {})
        monkeypatch.setattr(IM, "_hist_run", set())
        monkeypatch.setattr(IM, "_today_run", set())
        monkeypatch.setattr(IM, "_kick_hist", lambda c, d: state["hist_kick"].append((c, d)))
        monkeypatch.setattr(IM, "_kick_today", lambda c: state["today_kick"].append(c))
        return state

    def test_冷启动_全踢且warming(self, monkeypatch):
        st = self._env(monkeypatch)
        r = IM.get_cached(5, None)
        assert r["warming"] is True
        assert len(st["hist_kick"]) == 3 and len(st["today_kick"]) == 3
        assert all(v["hist"] is None for v in r["items"].values())
        assert set(r["items"]) == {"sh000001", "sh000688", "sz399006"}

    def test_热缓存_按指纹决定是否重发历史(self, monkeypatch):
        st = self._env(monkeypatch)
        hist_days = {"2026-11-24": [[931, 3556123, 12000], [932, 3557000, 8500]],
                     "2026-11-25": [[931, 3558000, 9000]]}
        monkeypatch.setattr(IM, "_hist", {
            "sh000001": {"ts": IM._time.time(), "days": hist_days, "sig": "SIG1"},
            "sh000688": {"ts": IM._time.time(), "days": hist_days, "sig": "SIG1"},
            "sz399006": {"ts": IM._time.time(), "days": hist_days, "sig": "SIG1"},
        })
        monkeypatch.setattr(IM, "_today", {
            c: {"ts": IM._time.time(), "date": "2026-11-26", "pts": [[931, 3500000, 990000]]}
            for c in ("sh000001", "sh000688", "sz399006")})
        r = IM.get_cached(5, {"sh000001": "SIG1", "sh000688": "SIG1", "sz399006": "SIG1"})
        it = r["items"]["sh000001"]
        assert it["hist"] is None, "指纹一致 ⇒ 不再下发历史"
        assert it["today"][0] == [931, 3500000, 990000]
        assert it["prev_date"] == "2026-11-25"
        assert r["warming"] is False
        # 指纹不一致（例如新开盘多了一天）⇒ 重发历史
        r2 = IM.get_cached(5, {"sh000001": "OLD"})
        assert r2["items"]["sh000001"]["hist"] is not None
        assert [d["date"] for d in r2["items"]["sh000001"]["hist"]] == list(hist_days)

    def test_天数截断(self, monkeypatch):
        self._env(monkeypatch)
        days = {f"2026-11-{d:02d}": [[931, 3000000, 1000]] for d in range(10, 25)}
        monkeypatch.setattr(IM, "_hist", {
            c: {"ts": IM._time.time(), "days": dict(days), "sig": "S"} for c in
            ("sh000001", "sh000688", "sz399006")})
        monkeypatch.setattr(IM, "_today", {})
        r = IM.get_cached(3, None)
        hist = r["items"]["sh000001"]["hist"]
        assert len(hist) == 3
        assert hist[-1]["date"] == "2026-11-24", "取最近 3 天（升序）"
