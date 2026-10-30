# -*- coding: utf-8 -*-
"""结构层「网络段移出 pandas 名额」的**逐元素等价性**验证（2026-10-30，CPU 工程铁律）。

背景：`_kick_pb_struct_warm._one` 在 `with _BG_PANDAS_SEM:` **里**调 `load_structure(bc)`，
而该函数默认允许出网 ⇒ 2 个 pandas 名额会被**网络等待**占住（实测单只 0.9~3.6s，纯磁盘 0.12s），
标签批（同样要这 2 个名额）被堵在后面 —— 这正是每 30 分钟边界 5 分钟尖峰的形状。

改法：把 `_minute_bars(5min/30min)` 的**取数**（IO）挪到信号量外，信号量里只做 CPU
（`load_structure(..., allow_network=False)`）。本测试证明两条路**返回逐元素相同**：

  旧路：load_structure(code)                         # 取数+计算都在里面
  新路：prefetch_minutes(code) → load_structure(code, allow_network=False) → 回填 src_*

用**假 chart_cache**（内存存储）跑，不碰网络、不写磁盘。
运行：python -m pytest -q tests/phase3/test_struct_prefetch_equivalence.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import position_structure_feed as psf  # noqa: E402


def _mk_minute(freq_min: int, days: int, seed: int) -> pd.DataFrame:
    """合成 minutes：每天 09:30-15:00 按 freq 切根，跨 `days` 个交易日，末根=今天。"""
    per_day = {"5min": 48, "30min": 8}[{5: "5min", 30: "30min"}[freq_min]]
    n = per_day * days
    rng = np.random.default_rng(seed)
    px = 10.0 + np.cumsum(rng.normal(0, 0.01, n))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # 最后一个交易日 = 今天；往前 days-1 个自然日（跳过周末无所谓，逐日拼接即可）
    times = []
    for d in range(days - 1, -1, -1):
        day = today - timedelta(days=d)
        base = day.replace(hour=9, minute=30)
        for k in range(per_day):
            times.append(base + timedelta(minutes=freq_min * k))
    o = px + rng.normal(0, 0.005, n)
    h = np.maximum(o, px) + 0.02
    low = np.minimum(o, px) - 0.02
    return pd.DataFrame({"time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
                         "open": o, "high": h, "low": low, "close": px,
                         "volume": np.full(n, 1000.0)})


class _FakeStore:
    """假 chart_cache。`tencent_empty=True` 时模拟腾讯无数据（北交所等）→ 走 tushare 兜底。"""

    def __init__(self, tencent_empty=False):
        self.data = {}
        self.fetches = []          # [(source, freq)]
        self.tencent_empty = tencent_empty

    def load_minute_asof(self, ts_code, freq, date_str=None):
        df = self.data.get((ts_code, freq))
        return df.copy() if df is not None else pd.DataFrame()

    def fetch_minute_tencent(self, ts_code, freq, count=640):
        self.fetches.append(("tencent", freq))
        if self.tencent_empty:
            return pd.DataFrame()
        return _mk_minute(5 if freq == "5min" else 30, 30, seed=hash((ts_code, freq)) % 1000)

    def fetch_minute_online(self, ts_code, freq, days):
        self.fetches.append(("tushare", freq))
        return _mk_minute(5 if freq == "5min" else 30, days, seed=hash((ts_code, freq)) % 1000)

    def save_minute_history(self, ts_code, freq, df, cap=None):
        self.data[(ts_code, freq)] = df.copy()
        return True


def _daily(code: str) -> pd.DataFrame:
    n = 260
    rng = np.random.default_rng(7)
    px = 10 + np.cumsum(rng.normal(0, 0.05, n))
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"),
                         "open": px, "high": px + 0.1, "low": px - 0.1,
                         "close": px, "volume": 1000.0})


def _cmp(a, b, path="$"):
    """递归比较（NaN 视为相等；逐键、逐元素）。返回不一致路径列表。"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(set(a) ^ set(b))}"]
        for k in a:
            out += _cmp(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} vs {len(b)}"]
        for i, (x, y) in enumerate(zip(a, b)):
            out += _cmp(x, y, f"{path}[{i}]")
        return out
    if isinstance(a, float) and isinstance(b, float):
        if pd.isna(a) and pd.isna(b):
            return []
        return [] if a == b else [f"{path}: {a!r} vs {b!r}"]
    return [] if a == b else [f"{path}: {a!r} vs {b!r}"]


class TestPrefetchEquivalence(unittest.TestCase):
    def setUp(self):
        self._orig_cc = psf._cc
        self._store = _FakeStore()
        psf._cc = self._store
        psf._net_cooldown_reset()
        import core.chart_cache as cc
        self._orig_daily = cc.load_daily_display
        cc.load_daily_display = lambda code, **kw: _daily(code)

    def tearDown(self):
        psf._cc = self._orig_cc
        import core.chart_cache as cc
        cc.load_daily_display = self._orig_daily
        psf._net_cooldown_reset()

    def _new_path(self, code):
        """模拟改后的 `_kick_pb_struct_warm._one`：取数（分钟线+日线）在信号量外，
        信号量内只做 CPU。日线按 `load_structure` 内部同款顺序取（本地缓存 → provider）。"""
        import core.chart_cache as cc
        dly = cc.load_daily_display(code)
        src = psf.prefetch_minutes(code)
        st = psf.load_structure(code, allow_network=False, daily_df=dly)
        if isinstance(st, dict) and isinstance(st.get("data_asof"), dict) and src:
            st["data_asof"].update({f"src_{k}": v for k, v in src.items()})
        return st

    def test_逐元素等价(self):
        """冷缓存（磁盘空）下，两条路返回完全一致（含 text / data_asof / stale）。"""
        for code in ("600362", "000001"):
            psf._net_cooldown_reset()
            self._store.data.clear()
            old = psf.load_structure(code)              # 旧路：内部出网
            self.assertNotEqual(old.get("data_asof", {}).get("src_5min"), "none",
                                "旧路应真的走了网络/磁盘")
            psf._net_cooldown_reset()
            self._store.data.clear()
            new = self._new_path(code)                  # 新路：先预取、再纯磁盘算
            diff = _cmp(old, new)
            self.assertEqual(diff, [], f"{code} 新旧路不一致：{diff[:5]}")

    def test_预取只取两次且参数一致(self):
        psf._net_cooldown_reset()
        self._store.data.clear()
        psf.prefetch_minutes("600362")
        self.assertEqual([f for _, f in self._store.fetches], ["5min", "30min"],
                         "预取必须恰好取 load_structure 需要的那两档，且顺序一致")
        self.assertEqual({src for src, _ in self._store.fetches}, {"tencent"},
                         "盘中实时路径应走腾讯（2026-10-30 口径）")

    def test_腾讯空则回退tushare(self):
        """北交所等腾讯无数据时，必须回退 tushare，不能直接空。"""
        psf._net_cooldown_reset()
        self._store.data.clear()
        self._store.tencent_empty = True
        psf.prefetch_minutes("600362")
        self.assertEqual([s for s, _ in self._store.fetches], ["tencent", "tushare",
                                                               "tencent", "tushare"],
                         "每档都应是 腾讯 → tushare 兜底")

    def test_预取后不再出网(self):
        psf._net_cooldown_reset()
        self._store.data.clear()
        psf.prefetch_minutes("600362")
        n0 = len(self._store.fetches)
        psf.load_structure("600362", allow_network=False)
        self.assertEqual(len(self._store.fetches), n0, "信号量内的计算路不得再打网络")


if __name__ == "__main__":
    unittest.main(verbosity=2)
