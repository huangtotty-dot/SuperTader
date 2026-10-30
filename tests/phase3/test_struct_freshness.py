# -*- coding: utf-8 -*-
"""结构层「够不够新」判据（2026-10-30，owner 批准的口径改动）。

旧判据：盘中只判 `末根日期 == 今天`。
问题：数据源改成腾讯（盘中能给当天 bar）后，这个判据**整天都满足** ⇒ 结构冻结在
上午第一次取到的那根；而换源前（tushare 盘中只给到上一交易日）它又永远不满足 ⇒
每个 30min 槽位对每只票重打一次无效网络。两头都错。

新判据：盘中 = 日期是今天 **且** 滞后 ≤ 1 个 30min 时段（+5min 余量）；
盘后/周末 = 末根在 `_FRESH_GAP_DAYS` 自然日内。

运行：python -m pytest -q tests/phase3/test_struct_freshness.py
"""
import os
import sys
import unittest
from datetime import datetime

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import position_structure_feed as psf  # noqa: E402


def _bars(last: datetime, n: int = 5) -> pd.DataFrame:
    times = [last - pd.Timedelta(minutes=30 * (n - 1 - i)) for i in range(n)]
    return pd.DataFrame({"time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
                         "open": [1.0] * n, "high": [1.1] * n, "low": [0.9] * n,
                         "close": [1.0] * n, "volume": [100.0] * n})


def FRI(h, m):
    return datetime(2026, 10, 30, h, m)                    # 周五（交易日）


class TestFreshEnough(unittest.TestCase):

    def test_盘中_一个时段内的滞后算新鲜(self):
        now = FRI(10, 31)
        self.assertTrue(psf._fresh_enough(_bars(FRI(10, 0)), now),
                        "30min 右端时间允许滞后 30min（+5 余量）")

    def test_盘中_超过一个时段必须重取(self):
        now = FRI(10, 36)
        self.assertFalse(psf._fresh_enough(_bars(FRI(10, 0)), now),
                         "滞后 36min ⇒ 必须重取（否则结构冻结在上午）")

    def test_盘中_昨天的bar不算新鲜(self):
        now = FRI(9, 40)
        self.assertFalse(psf._fresh_enough(_bars(datetime(2026, 10, 29, 15, 0)), now))

    def test_盘中_5min帧同样规则(self):
        now = FRI(14, 0)
        self.assertTrue(psf._fresh_enough(_bars(FRI(13, 35)), now))     # 25min
        self.assertFalse(psf._fresh_enough(_bars(FRI(13, 20)), now))    # 40min

    def test_当日收盘后_要今天的最终bar(self):
        """15:05 之后仍要求末根是**今天**：否则「当日已收盘」的 final bars 永远进不来。"""
        now = FRI(15, 16)
        self.assertFalse(psf._fresh_enough(_bars(datetime(2026, 10, 29, 15, 0)), now),
                         "收盘后拿昨天的 bar 必须重取")
        self.assertTrue(psf._fresh_enough(_bars(FRI(15, 0)), now))

    def test_盘前按自然日窗口(self):
        mon = datetime(2026, 11, 2, 9, 20)                 # 下周一盘前
        self.assertTrue(psf._fresh_enough(_bars(datetime(2026, 10, 30, 15, 0)), mon),
                        "盘前看上一交易日收盘 = 新鲜")

    def test_盘后_按自然日窗口(self):
        sat = datetime(2026, 10, 31, 11, 0)
        self.assertTrue(psf._fresh_enough(_bars(datetime(2026, 10, 30, 15, 0)), sat),
                        "周末看周五收盘 = 新鲜")
        self.assertFalse(psf._fresh_enough(_bars(datetime(2026, 10, 20, 15, 0)), sat),
                         "10 天前 = 陈旧")


if __name__ == "__main__":
    unittest.main(verbosity=2)
