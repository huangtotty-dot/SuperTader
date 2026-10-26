# -*- coding: utf-8 -*-
"""通达信本地文件读取器（2026-10-26）：`.day` 与 `.lc5`/`.lc1` 的解析与重采样。

背景：平均股价（em47.800005）东财常年风控，改读通达信本地 880003：
  · `vipdoc/sh/lday/sh880003.day`       日线（32 字节/条 `<IIIIIfII>`，价 ×100）
  · `vipdoc/sh/fzline/sh880003.lc5`     5 分钟（32 字节/条 `<HHfffffII>`）
  · `vipdoc/sh/minline/sh880003.lc1`    1 分钟
本机实测这两个分钟目录**默认是空的**（通达信要手动下载），所以读取器必须优雅返回 None。
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import t_gui  # noqa: E402


def _api():
    return object.__new__(t_gui.Api)


def _day_rec(date_int, o, h, l, c, amt, vol):
    return struct.pack("<IIIIIfII", date_int, o, h, l, c, amt, vol, 0)


def _lc_rec(date_int, minute, px):
    return struct.pack("<HHfffffII", date_int, minute, px, px + 0.05, px - 0.05,
                       px + 0.02, 1e9, 100000, 0)


@pytest.fixture()
def tdx_root(tmp_path):
    root = tmp_path / "vipdoc"
    (root / "sh" / "lday").mkdir(parents=True)
    (root / "sh" / "fzline").mkdir(parents=True)
    (root / "sh" / "minline").mkdir(parents=True)
    return root


def test_day_last_two(tdx_root):
    # `.day` 的日期字段是 **YYYYMMDD 整数**（20261024），不是分钟线那套 (year-2004)*2048 编码
    d, d2 = 20261024, 20261026
    (tdx_root / "sh" / "lday" / "sh880003.day").write_bytes(
        _day_rec(d, 2400, 2450, 2350, 2440, 1e9, 100) + _day_rec(d2, 2462, 2471, 2425, 2440, 1e9, 200))
    api = _api(); api._TDX_VIPDOC = tdx_root
    price, pre, asof = api._tdx_index_last("880003")
    assert price == 24.40 and pre == 24.40, "价 = close/100"
    assert asof == "2026-10-26"


def test_minute_missing_returns_none(tdx_root):
    """通达信默认不下载分钟数据 ⇒ 必须返回 None，而不是抛错。"""
    (tdx_root / "sh" / "lday" / "sh880003.day").write_bytes(_day_rec(1, 1, 1, 1, 1, 0, 0) * 2)
    api = _api(); api._TDX_VIPDOC = tdx_root
    for m in (1, 5, 10, 30, 60):
        assert api._tdx_min_df("em47.800005", m) is None


def test_minute_resample_bucket_edges(tdx_root):
    """5→30/60 的桶要用 **A 股标准边界**（10:30 / 11:30 / 14:00 / 15:00），不是 minute//N。"""
    recs = []
    for day in (22, 23):
        d = (2026 - 2004) * 2048 + 10 * 100 + day
        for i in range(48):
            mins = 570 + i * 5 if i < 24 else 780 + (i - 24) * 5
            recs.append(_lc_rec(d, mins, 24.0 + i * 0.01))
    (tdx_root / "sh" / "fzline" / "sh880003.lc5").write_bytes(b"".join(recs))
    (tdx_root / "sh" / "lday" / "sh880003.day").write_bytes(_day_rec(1, 1, 1, 1, 1, 0, 0) * 2)
    api = _api(); api._TDX_VIPDOC = tdx_root

    df5 = api._tdx_min_df("em47.800005", 5)
    assert len(df5) == 96, "2 天 × 48 根"
    df30 = api._tdx_min_df("em47.800005", 30)
    df60 = api._tdx_min_df("em47.800005", 60)
    assert len(df30) == 16 and len(df60) == 8
    assert [str(t)[11:16] for t in df30["time"].head(4)] == ["09:30", "10:00", "10:30", "11:00"]
    assert [str(t)[11:16] for t in df60["time"].head(4)] == ["09:30", "10:30", "13:00", "14:00"]
