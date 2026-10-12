# -*- coding: utf-8 -*-
"""日线缓存「不许被静默截短」+ 缺口必须可见（2026-10-12）。

**起因**：owner 报「持仓体检少了江西铜业(600362)」。查下来数据侧全在
（holdings_manual / holdings_daily / load_quotes / load_stock_chart 都有），
真正链条是：

```
某个短窗口写缓存 → save_daily_cache 盲覆盖，把 600362 的 801 根写成 21 根（静默截短）
  → 建图表 payload 时读到残缺缓存 ⇒ payload 只存了 21 根
    → 持仓体检 `if n < 30: continue` **直接丢行、不留痕** ⇒ 界面上"少了一只"，零线索
```

**同类前科**：`t_io/cache/daily_kline/index_*.json` 也这么被截短过
（见 memory「指数日线缓存会被静默截短」）—— 这是**第三次**同源问题，
所以这次修在**写入端**（保护所有读缓存的消费端：图表/体检/技术标签/建仓符合度），
而不只是修那个显示。

三处修复（本文件逐一钉住）：
  ① `save_daily_cache` 既有缓存更长时**改走 merge**（并集），不再覆盖；
  ② 持仓体检 `n < 30` 从"静默丢行"改为**留一行并写明原因**；
  ③ `_serve_payload_cache` 除"比缓存旧"外还判"**比缓存少**"⇒ 触发重建
     （日期闸拦不住：两者末根都可能是同一天）。

运行：python -m unittest tests.phase3.test_daily_cache_no_truncate -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from core.market_data import tencent_provider as TP  # noqa: E402


def _rows_of(fp):
    """读缓存行数（**关文件**，免得留 ResourceWarning）。"""
    with open(fp, encoding="utf-8") as f:
        return json.load(f)["rows"]


def _df(n, start_day=1):
    return pd.DataFrame([{"date": f"2026-09-{start_day + i:02d}", "open": 10.0, "close": 10.5,
                          "high": 10.6, "low": 9.9, "volume": 100.0} for i in range(n)])


class TestSaveDailyCacheNoTruncate(unittest.TestCase):
    """① 写入端不许把长历史写没。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = TP._DAILY_CACHE_DIR
        TP._DAILY_CACHE_DIR = self._tmp.name
        self.fp = Path(self._tmp.name) / "600362.json"

    def tearDown(self):
        TP._DAILY_CACHE_DIR = self._orig
        self._tmp.cleanup()

    def _seed(self, n):
        TP.save_daily_cache("600362", _df(n))

    def test_short_write_does_not_shrink_existing(self):
        self._seed(60)
        self.assertEqual(len(_rows_of(self.fp)), 60)
        # 短窗口写入（模拟"只拉了 1 个月"）
        TP.save_daily_cache("600362", _df(15))
        rows = _rows_of(self.fp)
        self.assertGreaterEqual(len(rows), 60, "⚠️ 短窗口写入不得把长历史截短（600362 曾 801→21）")

    def test_longer_write_still_replaces(self):
        """更长的新数据照常写入（并集后应含新日期）。"""
        self._seed(20)
        TP.save_daily_cache("600362", _df(40))
        rows = _rows_of(self.fp)
        self.assertGreaterEqual(len(rows), 40)

    def test_fresh_code_writes_normally(self):
        TP.save_daily_cache("999999", _df(10))
        self.assertEqual(len(_rows_of(Path(self._tmp.name) / "999999.json")), 10)

    def test_empty_df_is_noop(self):
        self._seed(30)
        TP.save_daily_cache("600362", None)
        TP.save_daily_cache("600362", pd.DataFrame())
        self.assertEqual(len(_rows_of(self.fp)), 30)


class TestObShortSeriesVisible(unittest.TestCase):
    """② 体检表遇到"日线不足"必须**留行+写明原因**，不许静默丢。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (_ROOT / "t_gui.py").read_text(encoding="utf-8")

    def test_short_series_appends_row_with_reason(self):
        i = self.src.index("if n < 30:")
        window = self.src[i:i + 700]
        self.assertIn("out[\"stocks\"].append(", window,
                      "日线不足时若不 append，界面上就是『少了一只』且零线索"
                      "（owner 报的 600362 就是这么消失的）")
        self.assertIn("日线不足", window, "要写明原因")

    def test_no_bare_continue_on_short_series(self):
        i = self.src.index("if n < 30:")
        window = self.src[i:i + 700]
        self.assertNotIn("if n < 30:\n                continue", window)


class TestPayloadShorterThanCacheRebuilds(unittest.TestCase):
    """③ payload 比日线缓存**少** ⇒ 判过期、触发重建（日期闸拦不住）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (_ROOT / "t_gui.py").read_text(encoding="utf-8")

    def test_payload_length_guard_present(self):
        i = self.src.index("def _serve_payload_cache")
        window = self.src[i:i + 2600]
        self.assertIn("daily_rows", window, "要读 payload 的 daily_rows 长度")
        self.assertIn("len(_prows) * 2 < len(_dfc)", window,
                      "payload 明显短于日线缓存时应 return None（触发重建）")

    def test_guard_uses_cache_it_already_reads(self):
        """长度判据必须复用已经读过的 `_dfc` —— 别为此再加一次磁盘 I/O。"""
        i = self.src.index("def _serve_payload_cache")
        window = self.src[i:i + 2600]
        self.assertEqual(window.count("load_daily_display"), 1,
                         "payload 命中路径只应读一次日线缓存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
