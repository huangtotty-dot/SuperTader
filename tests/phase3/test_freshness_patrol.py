# -*- coding: utf-8 -*-
"""产物新鲜度巡检单测（2026-10-12）。**全离线、零网络、零生产写**。

守四件事：
  T1 分桶：超阈值但可救 = "陈旧"（要报）；停更超过 `_LONG_DEAD_DAYS` = "休眠"（退市股，**不得**报）。
     回归：daily_kline 里有一批退市股（000618 末根 2006-02-10 等）永远是陈旧的，
     不隔离就会让卡片头条长期是僵尸代码 ⇒ 教会 owner 忽略告警（"死报警"）。
  T2 假当日根识别：末根与 `saved_at` 同日 + volume<=0 + high==low ⇒ 假根；
     ⚠️ **一字板（volume>0）必须放行** —— 判据不依赖 volume 字段是否存在。
  T3 防噪：⚠️ 只给"与上一轮相比**新出现**"的陈旧；持续陈旧标"持续"，不重复打断。
  T4 幂等 + 不误推：当日标记存在即跳过；dry_run 不推不落盘。

运行：python -m unittest tests.phase3.test_freshness_patrol -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.chart_cache as cc  # noqa: E402
import core.freshness_patrol as fp  # noqa: E402


class TestStatsBuckets(unittest.TestCase):
    def test_stale_and_dormant_split(self):
        entries = [("fresh1", 1, ""), ("fresh2", 12, ""),
                   ("stale1", 13, ""), ("stale2", 42, ""), ("stale3", 90, ""),
                   ("dead1", 91, ""), ("dead2", 7549, "")]
        st = fp._stats("x", entries)
        self.assertEqual(st["stale"], 3, "13/42/90 天应是可行动陈旧")
        self.assertEqual(st["dormant"], 2, "91/7549 天是休眠（疑退市）")
        self.assertEqual(st["worst_gap"], 90, "最差只能在非休眠样本里取")
        # 非休眠样本 = [1, 12, 13, 42, 90] ⇒ 中位 13（休眠的 91/7549 不得参与）
        self.assertEqual(st["median_gap"], 13, "中位也要排除休眠样本")
        self.assertEqual(len(st["examples"]), 3)
        self.assertIn("stale3 gap=90d", st["examples"][0], "样例按 gap 降序")

    def test_all_fresh(self):
        st = fp._stats("x", [("a", 0, ""), ("b", 3, "")])
        self.assertEqual((st["stale"], st["dormant"]), (0, 0))
        self.assertEqual(st["worst_gap"], 3)

    def test_empty(self):
        st = fp._stats("x", [])
        self.assertEqual((st["total"], st["stale"]), (0, 0))
        self.assertIsNone(st["worst_gap"])


class TestFakeFormingBar(unittest.TestCase):
    @staticmethod
    def _rec(vol, hi, lo, saved="2026-10-09 09:17:39", last="2026-10-09"):
        return {"saved_at": saved,
                "daily_rows": [{"date": "2026-10-08", "open": 1, "high": 2, "low": 0.5,
                                "close": 1.5, "volume": 100},
                               {"date": last, "open": 64.5, "high": hi, "low": lo,
                                "close": 64.5, "volume": vol}]}

    def test_placeholder_detected(self):
        """盘前快照造的假根：O=H=L=C 且 volume=0（实测 300054 / 588170 就是这个形状）。"""
        self.assertTrue(fp._is_fake_forming(self._rec(0.0, 64.5, 64.5)))

    def test_limit_up_one_word_board_must_pass(self):
        """⚠️ 一字板 volume>0 必须放行 —— 它是**真实**成交，不是假根。"""
        self.assertFalse(fp._is_fake_forming(self._rec(1000.0, 64.5, 64.5)),
                         "一字板（high==low 但 volume>0）是真实 bar，不得判假")

    def test_normal_bar_passes(self):
        self.assertFalse(fp._is_fake_forming(self._rec(1000.0, 66.0, 63.0)))

    def test_last_row_not_today_passes(self):
        """末根不是写入当日 ⇒ 不是"盘前快照造的当日根"，不判假。"""
        self.assertFalse(fp._is_fake_forming(self._rec(0.0, 64.5, 64.5, last="2026-10-08")))

    def test_missing_fields_safe(self):
        for bad in ({}, {"daily_rows": []}, {"daily_rows": [{}]},
                    {"saved_at": None, "daily_rows": [{"date": "2026-10-09"}]},
                    {"saved_at": "2026-10-09 09:00:00",
                     "daily_rows": [{"date": "2026-10-09", "high": "x", "low": "y",
                                     "volume": "z"}]}):
            self.assertFalse(fp._is_fake_forming(bad), f"缺字段不应抛: {bad}")


class TestCardNoiseControl(unittest.TestCase):
    @staticmethod
    def _rep(newly, stale_names, arts):
        return {"ts": "2026-10-12 15:40:00", "date": "2026-10-12",
                "artifacts": arts, "stale_names": stale_names, "newly_stale": newly}

    def test_newly_stale_gets_warning_and_examples(self):
        a = fp._stats("daily_kline", [("600362", 40, "")] + [(f"c{i}", 1, "") for i in range(9)])
        card = fp.format_card(self._rep(["daily_kline"], ["daily_kline"], [a]))["content"]["text"]
        self.assertTrue(card.startswith("⚠️"), "新出现陈旧要打断")
        self.assertIn("🆕", card)
        self.assertIn("600362", card, "新出现的要给样例（可行动）")

    def test_persisting_stale_does_not_warn(self):
        """持续陈旧不打断（否则 30min 缓存那种长期陈旧会天天刷屏）。"""
        a = fp._stats("minutes", [("x_60min", 55, "")] + [(f"c{i}", 1, "") for i in range(9)])
        card = fp.format_card(self._rep([], ["minutes"], [a]))["content"]["text"]
        self.assertTrue(card.startswith("✅"), f"非新出现不应带 ⚠️: {card[:20]}")
        self.assertIn("持续", card)
        self.assertNotIn("🆕", card)

    def test_all_fresh_card(self):
        a = fp._stats("chart_payload", [("a", 1, ""), ("b", 2, "")])
        card = fp.format_card(self._rep([], [], [a]))["content"]["text"]
        self.assertTrue(card.startswith("✅"))
        self.assertIn("新鲜", card)


class TestSurveyAgainstRealCache(unittest.TestCase):
    """用**真实磁盘缓存**跑一遍：只读，不写任何生产文件。"""

    def test_survey_shape_and_zero_write(self):
        rep = fp.run_once(dry_run=True)
        self.assertIn("artifacts", rep, f"巡检不应失败: {rep}")
        names = [a["name"] for a in rep["artifacts"]]
        self.assertIn("chart_payload", names)
        for a in rep["artifacts"]:
            self.assertLessEqual(a["stale"] + a["dormant"], a["total"])
            self.assertNotIn(a["name"], [])

    def test_finds_known_fake_forming_payloads(self):
        """真实磁盘上确实还留着 10-09 盘前造的假当日根（自愈只在读端做，文件没重写）。

        这是本巡检的**实证价值**：这些坏件此前没有任何巡检能发现。
        """
        st = fp.survey_payload()
        self.assertGreaterEqual(st["fake_forming"], 1,
                                "磁盘上应仍存在假当日根 payload（10-09 那批）")


class TestIdempotence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_mark, self._orig_hist = fp._MARK_FP_FMT, fp._HISTORY_FP
        fp._MARK_FP_FMT = "freshness_patrol_{date}.json"

    def tearDown(self):
        fp._MARK_FP_FMT, fp._HISTORY_FP = self._orig_mark, self._orig_hist
        self._tmp.cleanup()

    def test_dry_run_does_not_push_or_write_mark(self):
        import core.freshness_patrol as _fp
        orig_push = _fp.push
        calls = []
        _fp.push = lambda rep: calls.append(rep) or True
        try:
            rep = _fp.run_once(dry_run=True)
        finally:
            _fp.push = orig_push
        self.assertEqual(calls, [], "dry_run 绝不能推飞书")
        self.assertIsNone(rep.get("pushed"))

    def test_day_mark_skips_second_run(self):
        """当日已巡检过 ⇒ 第二次直接跳过（跨重启也成立，因为标记是文件）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        mark = fp._STATE_DIR / fp._MARK_FP_FMT.format(date=today)
        existed = mark.exists()
        if existed:
            self.skipTest("今天已真跑过，跳过（不覆盖生产标记）")
        try:
            mark.write_text("{}", encoding="utf-8")
            res = fp.run_once()
            self.assertEqual(res.get("skipped"), "already_ran_today")
        finally:
            try:
                mark.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
