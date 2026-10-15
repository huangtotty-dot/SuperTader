# -*- coding: utf-8 -*-
"""scripts/osc_close_watchdog.py —— 14:50 卖侧补跑看门狗 行为钉测（2026-10-15）。

## 口径（断言）

  T1 **幂等性（本测试的第一公民）**：sells_close_{date}.json 存在 ⇒ decide 永远
     noop_done —— 窗口内也一样、--force 也一样（重复执行 = 重复下单 + days_held
     语义错乱，绝不允许）；脚本 main 干跑路径同样空操作退出（rc=0）。
  T2 时间窗：文件缺失时 [14:52,14:58) 内 run；14:52 前 wait；14:58 后 refuse；
     --force 放行时间窗（仍受 T1 幂等闸约束）。
  T3 取数解析：latest_prices_from_history 每只取最后一根、剔除非本票/非正价。
  T4 干跑集成：账本副本 + 注入现价走 replay_sell_side ⇒ 决策正确且**真账本不动**
     （生产目录不落 sells_close、positions.json 不被改写）。

运行：python -m unittest discover -s tests/phase3 -p "test_osc_close_watchdog.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for _p in (os.path.join(_ROOT, "execution", "auto"),
           os.path.join(_ROOT, "execution", "auto", "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("SUPERTRADER_ROOT", _ROOT)

from scripts import osc_close_watchdog as wd          # noqa: E402


def _dt(hh, mm):
    return datetime(2026, 10, 15, hh, mm, 0)


class TestDecideIdempotency(unittest.TestCase):
    """T1/T2：幂等闸绝对优先 + 时间窗。"""

    def setUp(self):
        self.book_dir = tempfile.mkdtemp(prefix="wd_book_")
        self.addCleanup(shutil.rmtree, self.book_dir, True)

    def _touch_done(self, date="2026-10-15"):
        with open(wd.sells_close_path(date, self.book_dir), "w", encoding="utf-8") as f:
            f.write("{}")

    def test_done_file_means_noop_even_inside_window(self):
        self._touch_done()
        action, reason = wd.decide(_dt(14, 53), "2026-10-15", book_dir=self.book_dir)
        self.assertEqual(action, "noop_done")
        self.assertIn("空操作", reason)

    def test_done_file_means_noop_even_with_force(self):
        self._touch_done()
        action, _ = wd.decide(_dt(14, 53), "2026-10-15",
                              book_dir=self.book_dir, force=True)
        self.assertEqual(action, "noop_done", "--force 不许越过幂等闸")

    def test_missing_file_inside_window_runs(self):
        action, _ = wd.decide(_dt(14, 52), "2026-10-15", book_dir=self.book_dir)
        self.assertEqual(action, "run")
        action, _ = wd.decide(_dt(14, 57), "2026-10-15", book_dir=self.book_dir)
        self.assertEqual(action, "run")

    def test_missing_file_before_window_waits(self):
        action, _ = wd.decide(_dt(14, 50), "2026-10-15", book_dir=self.book_dir)
        self.assertEqual(action, "wait")

    def test_missing_file_after_window_refuses(self):
        action, reason = wd.decide(_dt(14, 58), "2026-10-15", book_dir=self.book_dir)
        self.assertEqual(action, "refuse_outside_window")
        action, _ = wd.decide(_dt(9, 31), "2026-10-16", book_dir=self.book_dir)
        # 次日早盘：当日（10-16）文件缺失但在 14:52 前 ⇒ wait，不会误下昨日单
        self.assertIn(action, ("wait", "refuse_outside_window"))

    def test_force_overrides_window_but_not_idempotency(self):
        action, _ = wd.decide(_dt(15, 30), "2026-10-15",
                              book_dir=self.book_dir, force=True)
        self.assertEqual(action, "run")
        self._touch_done()
        action, _ = wd.decide(_dt(15, 30), "2026-10-15",
                              book_dir=self.book_dir, force=True)
        self.assertEqual(action, "noop_done")


class TestLatestPrices(unittest.TestCase):
    """T3：history(df=False) → 每票最新 close。"""

    def test_last_bar_wins_and_junk_filtered(self):
        bars = [
            {"symbol": "SHSE.605198", "eob": "2026-10-15 14:49:00", "close": 61.5},
            {"symbol": "SZSE.301373", "eob": "2026-10-15 14:49:00", "close": 107.0},
            {"symbol": "SHSE.605198", "eob": "2026-10-15 14:50:00", "close": 62.1},
            {"symbol": "SHSE.688003", "eob": "2026-10-15 14:50:00", "close": 0},   # 坏价
        ]
        px = wd.latest_prices_from_history(bars, ["605198", "688003", "301373"])
        self.assertEqual(px, {"605198": 62.1, "301373": 107.0})

    def test_empty_bars_gives_empty(self):
        self.assertEqual(wd.latest_prices_from_history([], ["605198"]), {})
        self.assertEqual(wd.latest_prices_from_history(None, ["605198"]), {})


class TestDryRunIntegration(unittest.TestCase):
    """T4：干跑重放 ⇒ 决策正确 + 真账本目录零副作用。"""

    def setUp(self):
        self.real_done = wd.sells_close_path("2026-10-15", wd.BOOK_DIR)
        self.assertFalse(os.path.exists(self.real_done),
                         "前置：生产目录不应存在 sells_close_2026-10-15.json（存在则本测试前提不成立）")

    def test_dry_run_noop_when_done(self):
        tmp = tempfile.mkdtemp(prefix="wd_main_")
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = wd.BOOK_DIR
        try:
            wd.BOOK_DIR = tmp
            with open(wd.sells_close_path("2026-10-15", tmp), "w", encoding="utf-8") as f:
                f.write("{}")
            rc = wd.main(["--dry-run", "--date", "2026-10-15",
                          "--now", "2026-10-15 14:53:00"])
            self.assertEqual(rc, 0, "文件存在 ⇒ 干跑空操作 rc=0")
            # 空操作 ⇒ 除了预置的 done 文件，tmp 里不多任何东西
            self.assertEqual(os.listdir(tmp), ["sells_close_2026-10-15.json"])
        finally:
            wd.BOOK_DIR = saved

    def test_dry_run_replay_uses_copy_and_leaves_real_book_untouched(self):
        # 真账本快照（内容 + mtime）
        pos_path = os.path.join(wd.BOOK_DIR, "positions.json")
        with open(pos_path, encoding="utf-8") as f:
            before = f.read()
        before_mtime = os.path.getmtime(pos_path)
        rc = wd.main(["--dry-run", "--date", "2026-10-15",
                      "--now", "2026-10-15 14:53:00", "--force",
                      "--prices", "605198=62.0,688003=65.6,301373=108.0"])
        self.assertEqual(rc, 0)
        with open(pos_path, encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after, "干跑改写了真 positions.json")
        self.assertEqual(before_mtime, os.path.getmtime(pos_path),
                         "干跑动了真 positions.json 的 mtime")
        self.assertFalse(os.path.exists(self.real_done),
                         "干跑在生产目录落了 sells_close（应只落临时副本）")


if __name__ == "__main__":
    unittest.main()
