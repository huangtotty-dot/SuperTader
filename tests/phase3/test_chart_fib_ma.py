# -*- coding: utf-8 -*-
"""K 线均线改斐波那契（2026-11-20 owner：5 13 21 34 55 …，单图与多窗口一致）。

契约：
  A. 后端 `gui._CHART_MA_PERIODS = (5,13,21,34,55,89,233,610)`，`_to_series()["ma"]` 按此顺序给 8 条；
     233 = 「年线」（青色虚线，2026-11-20 起替代 365），610 = 长线（金色点线）。
  B. 支撑/压力 `_calc_support_resistance` 的口径不变（MA20/60/180），且**不要求**输入 df
     带这些列（图表均线改期后 df 只有 ma5/13/21/34/55/89/233/610 ⇒ 曾 KeyError: 'ma20'）。
  C. 多窗口日线均线包 `_daily_ma_bundle` 键 = ma5/ma13/ma21/ma34/ma55。

运行：python -m pytest tests/phase3/test_chart_fib_ma.py -q
"""
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gui  # noqa: E402
from core.chart_analytics import _calc_support_resistance  # noqa: E402


def _df(n=700):
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n, freq="D"),
        "open": np.arange(n, dtype=float) + 10.0,
        "close": np.arange(n, dtype=float) + 10.0,
        "high": np.arange(n, dtype=float) + 10.5,
        "low": np.arange(n, dtype=float) + 9.5,
        "volume": np.ones(n) * 100,
    })


def test_chart_ma_periods_are_fibonacci():
    assert gui._CHART_MA_PERIODS == (5, 13, 21, 34, 55, 89, 233, 610)


def test_to_series_ma_order_and_values():
    d = gui._calc_ma_and_indicators(_df())
    s = gui._to_series(d)
    assert len(s["ma"]) == len(gui._CHART_MA_PERIODS) == 8
    # 末值 = 对应窗口的均值（同一份自增序列，窗口越长均值越低）
    closes = _df()["close"].values
    for i, n in enumerate(gui._CHART_MA_PERIODS):
        assert abs(s["ma"][i][-1] - round(float(closes[-n:].mean()), 3)) < 1e-6, n


def test_support_resistance_without_legacy_ma_columns():
    """图表均线改期后 df 无 ma20/60/180 ⇒ 支撑压力必须自算，不许 KeyError。"""
    d = gui._calc_ma_and_indicators(_df())          # 只有 ma5/13/21/34/55/89/365
    assert "ma20" not in d.columns
    out = _calc_support_resistance(d)
    assert isinstance(out, dict) and ("supports" in out and "resistances" in out)


def test_daily_ma_bundle_fib_keys(monkeypatch):
    import core.chart_cache as cc
    api = gui.Api.__new__(gui.Api)
    api._dma_memo = {}
    dates = pd.date_range("2026-01-01", periods=100).strftime("%Y-%m-%d").tolist()
    df = pd.DataFrame({"date": dates, "close": [10.0 + i * 0.01 for i in range(100)]})
    monkeypatch.setattr(cc, "load_daily_display", lambda c: df)
    d = api._daily_ma_bundle("600000", 11.0)
    assert set(d) == {"ma5", "ma13", "ma21", "ma34", "ma55"}
    basis = [10.0 + i * 0.01 for i in range(100)]
    assert abs(d["ma34"] - round((sum(basis[-33:]) + 11.0) / 34, 3)) < 1e-9
