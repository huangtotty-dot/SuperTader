# -*- coding: utf-8 -*-
"""
test_rsi_nan_guard.py — V1.1.2 RSI NaN 兜底（C 语义）可复跑单元测试

C 语义（父代理 2026-08-04 裁决）：
  - 0/0 钉平窗（gain==0 & loss==0）→ RSI 填 50 中性
  - 纯上涨窗（loss==0 & gain>0）→ 保持 NaN（与现网一致盲，不激活超买因子）
  - 预热 leading NaN（窗口内 delta 全 NaN）→ 保持 NaN
  - 正常窗口（loss>0）→ 与旧公式逐点一致

用法：python t_io/validation/rsi_nan_guard/test_rsi_nan_guard.py
退出码 0=全过，1=失败。
"""
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, BASE_DIR)

from config import PARAMS  # noqa: E402
import indicators  # noqa: E402

RSI_PERIOD = PARAMS.get("rsi_period", 6)


def _make_df(closes):
    """用收盘价序列构造最小 1 分钟 K 线 df（钉平段 high=low=close）。"""
    n = len(closes)
    times = pd.date_range("2026-08-03 09:30:00", periods=n, freq="min").strftime("%Y-%m-%d %H:%M:%S")
    return pd.DataFrame({
        "time": times,
        "open": closes, "close": closes,
        "high": closes, "low": closes,
        "volume": [1000.0] * n, "amount": [100000.0] * n,
    })


def _old_rsi(close, period):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(period, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def test_pinned_tail_fills_50():
    """钉平段（先跌后钉平）：0/0 窗口 → rsi == 50.0；此前的全跌窗保持旧值 0.0。"""
    closes = [10.0, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3] + [9.3] * (RSI_PERIOD + 3)
    df = indicators.add_indicators(_make_df(closes))
    rsi = df["rsi"]
    # 钉平持续足够根数后，窗口内 delta 全 0 → 0/0 窗 → 50（至少最后 3 根）
    assert (rsi.iloc[-3:] == 50.0).all(), f"钉平窗应填 50，实际 {rsi.tolist()}"
    # 钉平前的全跌窗口不受影响（loss>0、gain==0 → rsi==0，旧公式真值）
    assert (rsi.iloc[6:9] == 0.0).all(), f"全跌窗应为旧公式值 0.0，实际 {rsi.tolist()}"
    print("PASS test_pinned_tail_fills_50")


def test_pure_rise_window_stays_nan():
    """纯上涨窗（loss==0 & gain>0）→ 保持 NaN（C 语义核心：不激活超买因子）。"""
    closes = [10.0 + 0.1 * i for i in range(RSI_PERIOD + 5)]  # 单调上涨
    df = indicators.add_indicators(_make_df(closes))
    rsi = df["rsi"]
    # 单调上涨：每根窗口都无下跌 → 全部保持 NaN
    assert rsi.isna().all(), f"纯上涨窗应保持 NaN，实际 {rsi.tolist()}"
    print("PASS test_pure_rise_window_stays_nan")


def test_leading_warmup_nan_kept():
    """首根 K 线（delta 为 NaN、窗口无有效数据）→ 保持 NaN。"""
    closes = [10.0, 10.1, 10.2, 9.9, 10.0, 9.8, 10.1, 10.0]
    df = indicators.add_indicators(_make_df(closes))
    assert pd.isna(df["rsi"].iloc[0]), "首根（预热）应保持 NaN"
    print("PASS test_leading_warmup_nan_kept")


def test_normal_window_bit_identical():
    """正常混合序列：旧有效值与旧公式逐点一致；无 旧有效→新NaN 回退；
    若序列中天然出现纯上涨窗（旧公式 NaN），C 语义下必须保持 NaN。"""
    closes = [10.0, 10.1, 9.9, 10.2, 10.0, 9.8, 10.3, 10.1, 9.9, 10.2, 10.4, 10.2]
    df = _make_df(closes)
    new = indicators.add_indicators(df.copy())["rsi"]
    old = _old_rsi(df["close"], RSI_PERIOD)
    both = old.notna() & new.notna()
    assert both.sum() >= len(closes) - 2, "混合序列旧有效值不应大面积缺失"
    assert np.allclose(old[both], new[both], atol=1e-12), "正常窗口数值与旧公式不一致"
    assert not (old.notna() & new.isna()).any(), "出现 旧有效→新NaN 回退"
    # 混合序列无 0/0 钉平窗 → 不应有任何填充
    assert not (old.isna() & new.notna()).any(), "混合序列不应出现填充（无钉平窗）"
    print("PASS test_normal_window_bit_identical")


def test_flat_open_bars_fill_50():
    """开盘即钉平：首根 NaN（预热），第 2 根起 0/0 窗 → 50。"""
    closes = [10.0] * (RSI_PERIOD + 2)
    df = indicators.add_indicators(_make_df(closes))
    rsi = df["rsi"]
    assert pd.isna(rsi.iloc[0]), "首根应保持 NaN"
    assert (rsi.iloc[1:] == 50.0).all(), f"开盘钉平自第2根应填 50，实际 {rsi.tolist()}"
    print("PASS test_flat_open_bars_fill_50")


def test_5m_and_15m_same_semantics():
    """5 分 RSI(14) 与 15 分 RSI(6) 同一 C 语义。"""
    # 5m: 先跌后钉平（钉平 15 根覆盖 period=14 窗口）
    closes = [20.0 - 0.1 * i for i in range(5)] + [19.5] * 16
    df5 = pd.DataFrame({
        "time": pd.date_range("2026-08-03 09:35:00", periods=len(closes), freq="5min"),
        "open": closes, "close": closes, "high": closes, "low": closes,
        "volume": [1000.0] * len(closes), "amount": [100000.0] * len(closes),
    })
    r5 = indicators.add_5min_indicators(df5)["rsi_5m"]
    # period=14：钉平 16 根仅够最后一根窗口全 0 → 末根填 50
    assert r5.iloc[-1] == 50.0, f"5分钉平窗末根应填 50，实际 {r5.tolist()}"

    closes_up = [20.0 + 0.1 * i for i in range(10)]
    df5u = pd.DataFrame({
        "time": pd.date_range("2026-08-03 09:35:00", periods=len(closes_up), freq="5min"),
        "open": closes_up, "close": closes_up, "high": closes_up, "low": closes_up,
        "volume": [1000.0] * len(closes_up), "amount": [100000.0] * len(closes_up),
    })
    r5u = indicators.add_5min_indicators(df5u)["rsi_5m"]
    assert r5u.isna().all(), f"5分纯上涨窗应保持 NaN，实际 {r5u.tolist()}"

    # 15m: period=6，先跌后钉平
    closes15 = [30.0 - 0.1 * i for i in range(4)] + [29.7] * 8
    df15 = pd.DataFrame({
        "time": pd.date_range("2026-08-03 09:45:00", periods=len(closes15), freq="15min"),
        "open": closes15, "close": closes15, "high": closes15, "low": closes15,
        "volume": [1000.0] * len(closes15), "amount": [100000.0] * len(closes15),
    })
    r15 = indicators.add_15min_indicators(df15)["rsi_15m"]
    # period=6：钉平 8 根产生 7 个零 delta → 最后 2 根窗口全 0 → 50
    assert (r15.iloc[-2:] == 50.0).all(), f"15分钉平窗应填 50，实际 {r15.tolist()}"
    print("PASS test_5m_and_15m_same_semantics")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
