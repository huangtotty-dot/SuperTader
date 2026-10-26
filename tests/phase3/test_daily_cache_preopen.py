# -*- coding: utf-8 -*-
"""P1-12：`TencentProvider.daily_cache` 盘前接受「昨日档」离线单测（2026-10-26）。

背景：原硬闸 `cached["date"]==today` 让全部 ~5400 档在每日 00:00 集体失效，
早盘首波取数尖峰全压给 prefetch/猎手首轮。修复后：

  · 盘前（<09:15）：接受「末根=最近已知交易日（index_sh000001.json 末根）」的昨日档；
  · 盘中（≥09:15）：维持 `date==today` 硬闸 + 15 分钟 TTL，昨日档必须 miss；
  · 指数档缺失 / 个股末根落后于指数末根（停牌）⇒ 退回原硬闸，保守重取。

**全离线**：monkeypatch 缓存目录到 tmp + 冻结 `TP.datetime.now`，不打网络。

运行：python -m unittest tests.phase3.test_daily_cache_preopen -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.market_data import tencent_provider as TP  # noqa: E402

# 固定一个周三作为"今天"，昨日档末根 = 周二（上一交易日）
_TODAY = "2026-10-21"      # 周三
_PREV = "2026-10-20"       # 周二（上一交易日）


class _FrozenDatetime(datetime):
    """冻结 `TP.datetime.now()`；strptime/fromtimestamp 等其余接口保持真实行为。"""

    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _set_now(hhmm):
    _FrozenDatetime._now = datetime.strptime(f"{_TODAY} {hhmm}", "%Y-%m-%d %H:%M")


def _rows(last_date, n=5):
    """n 根平价日线（开=收=高=低 ⇒ 不会触发 has_impossible_gap），末根 = last_date。"""
    base = datetime.strptime(last_date, "%Y-%m-%d")
    return [{"date": (base - timedelta(days=n - 1 - i)).strftime("%Y-%m-%d"),
             "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0, "volume": 100.0}
            for i in range(n)]


class TestDailyCachePreopen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = TP._DAILY_CACHE_DIR
        TP._DAILY_CACHE_DIR = self._tmp.name
        self._orig_dt = TP.datetime
        TP.datetime = _FrozenDatetime
        TP._IDX_BAR_MEMO["key"] = None      # 防跨用例/跨文件串味
        TP._IDX_BAR_MEMO["date"] = ""

    def tearDown(self):
        TP._DAILY_CACHE_DIR = self._orig_dir
        TP.datetime = self._orig_dt
        self._tmp.cleanup()

    def _write_stock(self, code, head_date, last_row_date, saved_at):
        rec = {"date": head_date, "saved_at": saved_at,
               "forming": [], "rows": _rows(last_row_date)}
        (Path(self._tmp.name) / f"{code}.json").write_text(
            json.dumps(rec), encoding="utf-8")

    def _write_index(self, last_row_date):
        (Path(self._tmp.name) / "index_sh000001.json").write_text(
            json.dumps({"rows": _rows(last_row_date)}), encoding="utf-8")

    # ── P1-12 核心两个分支 ──────────────────────────────────────────
    def test_盘前接受昨日档(self):
        """<09:15 + 末根=指数末根（上一交易日）⇒ 命中，source=cache，契约不变。"""
        _set_now("08:50")
        self._write_index(_PREV)
        self._write_stock("600000", head_date=_PREV, last_row_date=_PREV,
                          saved_at=f"{_PREV} 15:05:00")
        df = TP.TencentProvider().daily_cache("600000")
        self.assertIsNotNone(df, "盘前昨日档应命中（削平早盘取数尖峰）")
        self.assertEqual(df.attrs.get("source"), "cache")
        self.assertEqual(str(df["date"].iloc[-1]), _PREV)
        self.assertEqual(len(df), 5)

    def test_盘中拒绝昨日档(self):
        """≥09:15：同一昨日档必须 miss（盘中语义不变，不拿旧数据当新数据）。"""
        _set_now("10:00")
        self._write_index(_PREV)
        self._write_stock("600000", head_date=_PREV, last_row_date=_PREV,
                          saved_at=f"{_PREV} 15:05:00")
        self.assertIsNone(TP.TencentProvider().daily_cache("600000"))

    # ── 保守回退分支 ────────────────────────────────────────────────
    def test_盘前指数档缺失退回硬闸(self):
        """指数档读不到 ⇒ 无法判定「最近交易日」⇒ 维持原硬闸，保守重取。"""
        _set_now("08:50")
        self._write_stock("600000", head_date=_PREV, last_row_date=_PREV,
                          saved_at=f"{_PREV} 15:05:00")
        self.assertIsNone(TP.TencentProvider().daily_cache("600000"))

    def test_盘前末根落后于指数仍拒绝(self):
        """个股末根（停牌等）落后指数末根 ⇒ 不与市场齐平 ⇒ 重取。"""
        _set_now("08:50")
        self._write_index(_PREV)
        self._write_stock("600000", head_date=_PREV, last_row_date="2026-10-16",
                          saved_at=f"{_PREV} 15:05:00")
        self.assertIsNone(TP.TencentProvider().daily_cache("600000"))

    # ── 原语义回归钉 ────────────────────────────────────────────────
    def test_当日档盘前照常命中(self):
        """date==today 的当日档（如夜间任务凌晨落盘）盘前照常命中 —— 原语义不变。"""
        _set_now("08:50")
        self._write_stock("600000", head_date=_TODAY, last_row_date=_PREV,
                          saved_at=f"{_TODAY} 06:00:00")
        self.assertIsNotNone(TP.TencentProvider().daily_cache("600000"))

    def test_盘中15分钟TTL不变(self):
        """date==today 且末根=今日，但写入超过 15 分钟 ⇒ miss（TTL 语义不变）。"""
        _set_now("10:00")
        self._write_stock("600000", head_date=_TODAY, last_row_date=_TODAY,
                          saved_at=f"{_TODAY} 09:00:00")
        self.assertIsNone(TP.TencentProvider().daily_cache("600000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
