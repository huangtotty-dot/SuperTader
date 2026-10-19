# -*- coding: utf-8 -*-
"""equity_daily bar 完整性校验单测（2026-10-19 复盘：600127 早盘残缺 bar 冒充 EOD）。

事故现场：t_io/cache/daily_kline/600127.json saved_at=09:43（早盘快照），
当日 bar high=14.26 < 10:00 实际成交 14.35（物理不可能），close=13.96 冒充 EOD，
而 14:50 真实价 13.16 ⇒ 净值高估约 5,600 元（439,251 → 真实 ≈433,600）。

覆盖：
1) 早盘快照（saved_at<15:00）→ 判残缺，强制腾讯换源，via 带 stale_bar_warn；
2) saved_at 覆盖收盘但 high < 当日 fill 观测价（物理不可能）→ 判残缺换源；
3) 换源也失败 → 沿用残缺值但 via 标 stale_bar_warn（复盘 KPI 可见）；
4) 完整 bar（saved_at≥15:00 且 high 覆盖观测）→ 走 cache 不换源；
5) 无 saved_at 无观测 → 无法证伪放行（保住历史回填行为）；
6) _observed_day_high：fill price/fill_vwap 取最大，忽略其他 code/非 fill 事件。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import scripts.equity_daily as ed  # noqa: E402

_DATE = "2026-10-19"
_CODE = "600127"


def _w(fp, recs):
    with open(fp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestBarIntegrity(unittest.TestCase):
    def setUp(self):
        self._old_k = ed._KLINE_DIR
        self._old_b = ed._BRIDGE_DIR
        self._old_tc = ed._tencent_close
        self._k = tempfile.TemporaryDirectory()
        self._b = tempfile.TemporaryDirectory()
        ed._KLINE_DIR = self._k.name
        ed._BRIDGE_DIR = self._b.name
        self.tencent_calls = []

        def _fake_tc(code, date, force=False):
            self.tencent_calls.append((code, date, force))
            return self._tc_ret
        ed._tencent_close = _fake_tc
        self._tc_ret = (None, None)

    def tearDown(self):
        ed._KLINE_DIR = self._old_k
        ed._BRIDGE_DIR = self._old_b
        ed._tencent_close = self._old_tc
        self._k.cleanup()
        self._b.cleanup()

    def _cache(self, rows, saved_at):
        with open(os.path.join(self._k.name, f"{_CODE}.json"), "w", encoding="utf-8") as f:
            json.dump({"date": _DATE, "saved_at": saved_at, "rows": rows}, f)

    def _events(self, recs):
        _w(os.path.join(self._b.name, f"events_{_DATE.replace('-', '')}.jsonl"), recs)

    _BAR_STALE = [{"date": _DATE, "open": 14.0, "close": 13.96,
                   "high": 14.26, "low": 13.68, "volume": 465846.0}]

    def test_morning_snapshot_switches_source(self):
        """10-19 事故原样复现：09:43 快照 + high<观测 → 换源腾讯，close 取真实 EOD。"""
        self._cache(self._BAR_STALE, "2026-10-19 09:43:03")
        self._events([{"event": "fill", "code": _CODE, "side": "BUY",
                       "qty": 7000, "price": 14.35}])
        self._tc_ret = (13.16, _DATE)
        close, via = ed._eod_close(_CODE, _DATE)
        self.assertEqual(close, 13.16)
        self.assertIn("stale_bar_warn", via)
        self.assertTrue(via.startswith("tencent"))
        self.assertEqual(self.tencent_calls, [(_CODE, _DATE, True)])  # 强制重取

    def test_high_below_observed_is_impossible(self):
        """saved_at 覆盖收盘但 high<fill 观测价（数据损坏）→ 同样判残缺。"""
        self._cache(self._BAR_STALE, "2026-10-19 15:31:00")
        self._events([{"event": "fill", "code": _CODE, "price": 14.35, "fill_vwap": 14.35}])
        self._tc_ret = (13.16, _DATE)
        close, via = ed._eod_close(_CODE, _DATE)
        self.assertEqual(close, 13.16)
        self.assertIn("stale_bar_warn", via)

    def test_refetch_failed_marks_warn_and_keeps_value(self):
        """备用源失败 → 沿用残缺 bar 但 via 必须带 stale_bar_warn（进 source 字段）。"""
        self._cache(self._BAR_STALE, "2026-10-19 09:43:03")
        self._tc_ret = (None, None)
        close, via = ed._eod_close(_CODE, _DATE)
        self.assertEqual(close, 13.96)
        self.assertTrue(via.startswith("stale_bar_warn"))
        self.assertIn("saved_at=2026-10-19 09:43:03", via)

    def test_complete_bar_stays_cache(self):
        """完整 bar（15:00 后落盘、high 覆盖观测）→ cache，不触发换源。"""
        self._cache([{"date": _DATE, "open": 14.0, "close": 13.16,
                      "high": 14.40, "low": 13.10, "volume": 900000.0}],
                    "2026-10-19 15:05:12")
        self._events([{"event": "fill", "code": _CODE, "price": 14.35}])
        close, via = ed._eod_close(_CODE, _DATE)
        self.assertEqual((close, via), (13.16, "cache"))
        self.assertEqual(self.tencent_calls, [])

    def test_no_timestamp_no_observation_passes(self):
        """saved_at 缺失且无事件观测 → 无法证伪，放行（历史回填行为不变）。"""
        self._cache(self._BAR_STALE, None)
        close, via = ed._eod_close(_CODE, _DATE)
        self.assertEqual((close, via), (13.96, "cache"))
        self.assertEqual(self.tencent_calls, [])

    def test_observed_day_high_max_of_fills(self):
        self._events([
            {"event": "fill", "code": _CODE, "price": 14.20},
            {"event": "fill", "code": _CODE, "price": 14.35, "fill_vwap": 14.30},
            {"event": "fill", "code": "601118", "price": 99.0},      # 其他票忽略
            {"event": "order", "code": _CODE, "price": 99.0},         # 非 fill 忽略
        ])
        self.assertAlmostEqual(ed._observed_day_high(_CODE, _DATE), 14.35)
        self.assertIsNone(ed._observed_day_high("601118", "2026-10-18"))  # 无文件


if __name__ == "__main__":
    unittest.main()
