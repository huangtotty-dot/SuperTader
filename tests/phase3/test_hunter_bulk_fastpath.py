# -*- coding: utf-8 -*-
"""猎手「本地批量帧直取」快路径的**等价性验证**（2026-10-20）。

背景：`fetch_one` 原来对每只票走「DataFrame → `_df_to_kline_rows` → 按腾讯格式解析回
DataFrame」的往返（全池实测 ~4.5s，且 pandas 3 的 Arrow 字符串 `.astype(str)` 是 4.5ms/只）。
快路径直接复用 `_bulk_kline` 的帧。**这种"省掉一趟转换"的改动必须证明逐元素等价**，
否则会静默改变涨跌幅/成交额这类下游判据。

本文件离线跑（不联网、不写盘）：把同一批帧分别喂给两条路径，逐列比对。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "stock_hunter"))

from modules import market_data as md   # noqa: E402


def _frame(rows):
    return pd.DataFrame(rows)


def _old_path_rows(df, code):
    """旧路径：DataFrame → 腾讯行 → 解析（复刻 fetch_one 里的那段逻辑）。"""
    rows = md._df_to_kline_rows(df)
    is_kcb = str(code).startswith("688")
    unit = 1.0 if is_kcb else 100.0
    out = []
    for item in rows:
        if not (isinstance(item, list) and len(item) >= 6):
            continue
        _close, _volume = float(item[2]), float(item[5])
        _high, _low = float(item[3]), float(item[4])
        if len(item) >= 7:
            _amount = float(item[6])
        else:
            _amount = (_high + _low + _close) / 3.0 * _volume * unit
        out.append({"date": item[0], "open": float(item[1]), "close": _close,
                    "high": _high, "low": _low, "volume": _volume, "amount": _amount})
    return pd.DataFrame(out).sort_values("date").reset_index(drop=True)


def _fast_path_rows(df, code):
    """新路径：直接用批量帧，缺 amount 时按同一口径推导。"""
    d = df.copy()
    if "date" in d.columns:
        d["date"] = d["date"].astype(str)
    if "amount" not in d.columns:
        unit = 1.0 if str(code).startswith("688") else 100.0
        mid = (d["high"].astype(float) + d["low"].astype(float) + d["close"].astype(float)) / 3.0
        d["amount"] = mid * d["volume"].astype(float) * unit
    return d.sort_values("date").reset_index(drop=True)


@pytest.mark.parametrize("code", ["600000", "688003", "300750", "920008"])
def test_fastpath_matches_old_parse(code):
    src = _frame([
        {"date": "2026-10-16", "open": 62.23, "high": 62.40, "low": 59.66, "close": 60.56, "volume": 25846.77},
        {"date": "2026-10-19", "open": 59.96, "high": 60.85, "low": 59.00, "close": 59.44, "volume": 366354.0},
        {"date": "2026-10-20", "open": 61.73, "high": 67.98, "low": 60.61, "close": 66.42, "volume": 6549107.0},
    ])
    old = _old_path_rows(src, code)
    new = _fast_path_rows(src, code)
    # 列**顺序**不参与契约（下游全按列名取），只比列集合与逐列数值
    assert set(old.columns) == set(new.columns)
    assert len(old) == len(new)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        assert old[col].astype(float).tolist() == pytest.approx(new[col].astype(float).tolist(),
                                                               rel=1e-9), f"{code} 列 {col} 不等价"
    assert old["date"].tolist() == new["date"].tolist()


def test_fastpath_keeps_688_volume_unit():
    """688（科创板）成交量单位是**股**、其它是**手**：推导 amount 必须区分，差 100 倍。"""
    src = _frame([{"date": "2026-10-20", "open": 10.0, "high": 12.0, "low": 10.0,
                   "close": 11.0, "volume": 1000.0}])
    a = _fast_path_rows(src, "688003")["amount"].iloc[0]
    b = _fast_path_rows(src, "600000")["amount"].iloc[0]
    assert pytest.approx(b, rel=1e-9) == a * 100


def test_missing_amount_would_poison_downstream():
    """守住那条静默错误的入口：缺 amount 列且不推导 ⇒ 下游 `NaN <= 0` 恒 False。"""
    df = _frame([{"date": "2026-10-20", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10.0}])
    assert "amount" not in df.columns
    nan_case = float(pd.Series([float("nan")]).iloc[0] or 0)
    assert not (nan_case <= 0)                     # ← 这就是"看着有值、其实永远过闸"的坑
    assert _fast_path_rows(df, "600000")["amount"].iloc[0] > 0
