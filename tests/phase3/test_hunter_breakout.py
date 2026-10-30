# -*- coding: utf-8 -*-
"""猎手轮询 · **箱体突破飞书提示**（2026-10-29 owner）。

「对于选股猎手 30 分钟轮询一次、对于刚刚突破箱体的股票发送飞书进行提示」——
30 分钟轮询本来就存在（`should_poll`，2026-10-11 上线），本轮只新增突破提示。

守四条：
  ① 判定口径与 GUI「突破箱体」卡**同源**：四道闸（昨收≤上沿<现价 / 幅度 0.3~3% /
     当日涨幅 ≤7% / ≥30 根历史）一个都不能少，常量不许与 `t_gui.Api._BK_*` 漂移；
  ② 「刚刚」= **一天一只只提示一次**（去重文件按日期存 code）；
  ③ 提示**并进本轮那张卡**（心跳/异常卡各一份），不额外多发一条 ——
     owner 2026-10-12 定的「每轮恰好一条消息」；
  ④ 探测失败绝不影响异常判定（失败静默 + 只记日志）。

铁律：全离线（日线缓存与箱体识别全部打桩）。
运行：python -m pytest -q tests/phase3/test_hunter_breakout.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import core.chart_cache as cc  # noqa: E402
import core.scanners as SC  # noqa: E402
from core import hunter_poll as HP  # noqa: E402

TODAY = "2026-10-29"


def _hist(n=60, close=10.8):
    """≥30 根的日线帧（日期都早于今天）。"""
    return pd.DataFrame({
        "date": [f"2026-0{1 + i // 28}-{(i % 28) + 1:02d}" for i in range(n)],
        "open": [close] * n, "high": [close * 1.01] * n, "low": [close * 0.99] * n,
        "close": [close] * n, "volume": [1000.0] * n,
    })


def _wl(**kw):
    row = {"代码": "000001", "名称": "平安银行", "现价": 11.90, "昨收": 11.30,
           "涨跌幅": 5.3, "量比": 1.1}
    row.update(kw)
    return pd.DataFrame([row])


class TestBreakoutDetect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="bk_")
        self._fp = HP._BK_DEDUP_FP
        self._ld = cc.load_daily_display
        self._top = SC._box_top_for
        HP._BK_DEDUP_FP = os.path.join(self._tmp, "bk.json")
        cc.load_daily_display = lambda code: _hist()
        SC._box_top_for = lambda hist, min_bars=30: 11.60   # 昨收 11.30 在箱内，现价 11.90 破沿 +2.6%

    def tearDown(self):
        HP._BK_DEDUP_FP = self._fp
        cc.load_daily_display = self._ld
        SC._box_top_for = self._top

    def test_命中(self):
        r = HP.find_box_breakouts(_wl(), TODAY)
        self.assertEqual(len(r), 1)
        b = r[0]
        self.assertEqual(b["code"], "000001")
        self.assertAlmostEqual(b["box_top"], 11.60, places=3)
        self.assertAlmostEqual(b["pct"], (11.90 / 11.60 - 1) * 100, places=1)
        self.assertAlmostEqual(b["chg"], (11.90 / 11.30 - 1) * 100, places=1)

    def test_四道闸(self):
        # ① 昨收 > 上沿（已经在里面/上面）⇒ 不算突破
        self.assertEqual(HP.find_box_breakouts(_wl(昨收=11.70, 现价=11.9), TODAY), [])
        # ② 幅度 < 0.3%（预筛就剔掉）
        self.assertEqual(HP.find_box_breakouts(_wl(现价=11.62, 昨收=11.6, 涨跌幅=0.17), TODAY), [])
        # ③ 幅度 > 3%
        self.assertEqual(HP.find_box_breakouts(_wl(现价=12.00, 昨收=11.5, 涨跌幅=4.35), TODAY), [])
        # ④ 当日涨幅 > 7%
        self.assertEqual(HP.find_box_breakouts(_wl(现价=11.90, 昨收=10.9, 涨跌幅=9.2), TODAY), [])

    def test_一天只提示一次(self):
        self.assertEqual(len(HP.find_box_breakouts(_wl(), TODAY)), 1)
        self.assertEqual(HP.find_box_breakouts(_wl(), TODAY), [], "同一只票当天第二次不该再报")
        # 换一天 ⇒ 重新可报（去重按日期，不是按代码永久静音）
        self.assertEqual(len(HP.find_box_breakouts(_wl(), "2026-10-30")), 1)

    def test_探测失败不影响轮询(self):
        cc.load_daily_display = lambda code: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(HP.find_box_breakouts(_wl(), TODAY), [], "单只失败只跳过，不抛")

    def test_常量与GUI同源(self):
        import t_gui
        self.assertAlmostEqual(HP.BK_MIN_PCT, t_gui.Api._BK_MIN_PCT)
        self.assertAlmostEqual(HP.BK_MAX_PCT, t_gui.Api._BK_MAX_PCT)
        self.assertAlmostEqual(HP.BK_MAX_CHG, t_gui.Api._BK_MAX_CHG)
        self.assertEqual(HP.BK_MIN_BARS, t_gui.Api._BK_MIN_BARS)


class TestBreakoutCard(unittest.TestCase):
    """提示要**并进本轮那张卡**，不额外多发一条。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path as _P
        cls.src = (_P(_ROOT) / "core" / "hunter_poll.py").read_text(encoding="utf-8")

    def test_两种卡片都带突破几行(self):
        i = self.src.index("def format_summary_card")
        j = self.src.index("def push_summary")
        self.assertIn("_breakout_lines(res)", self.src[i:j], "心跳卡要带")
        i2 = self.src.index("def format_anomaly_card")
        j2 = self.src.index("def _load_dedup")
        self.assertIn("_breakout_lines(res", self.src[i2:j2], "异常卡也要带（仍是同一条消息）")

    def test_轮询里调用探测(self):
        self.assertIn("find_box_breakouts(wl, date_str)", self.src)

    def test_默认全列不截断(self):
        """owner 2026-10-29：「突破箱体通知的数量不全」—— 默认必须把命中的**全部**列出来。"""
        bk = [{"code": f"0000{i:02d}", "name": "样本", "prev_close": 10.0, "price": 10.2,
               "box_top": 10.1, "pct": 1.0, "chg": 2.0, "vol_ratio": None} for i in range(12)]
        lines = HP._breakout_lines({"breakouts": bk})
        self.assertEqual(len(lines), 13, "12 只全列 + 1 行标题")
        self.assertFalse(any("未列出" in x for x in lines), "没截断就不该出现『未列出』")
        # 真的截断时，文案要说清「还有几只没列」，不能写「…等 12 只」（读起来像漏了更多）
        lines3 = HP._breakout_lines({"breakouts": bk}, limit=3)
        self.assertTrue(any("另有 9 只未列出" in x for x in lines3))

    def test_每行是分字段而不是一坨数字(self):
        lines = HP._breakout_lines({"breakouts": [
            {"code": "002347", "name": "泰尔股份", "prev_close": 6.57, "price": 6.90,
             "box_top": 6.704, "pct": 2.92, "chg": 5.02, "vol_ratio": 1.0}]})
        row = lines[1]
        for frag in ("现价 6.9", "箱体上沿 6.704", "突破 +2.9%", "今日 +5.0%", "量比 1.0"):
            self.assertIn(frag, row)
        self.assertNotIn("→", row, "别再用『昨收6.57→6.9』这种挤在一起的写法")

    def test_没突破时不占位(self):
        self.assertEqual(HP._breakout_lines({"breakouts": []}), [])
        self.assertEqual(HP._breakout_lines({}), [])
        lines = HP._breakout_lines({"breakouts": [
            {"code": "000001", "name": "平安银行", "prev_close": 11.30, "price": 11.90,
             "box_top": 11.60, "pct": 2.59, "chg": 5.31, "vol_ratio": 1.1}]})
        self.assertTrue(lines[0].startswith("📦 刚突破箱体 1 只"))
        self.assertIn("000001", lines[1])


class TestPollCadence(unittest.TestCase):
    """30 分钟轮询本来就存在 —— 钉住它，别被后来的改动悄悄改掉节奏。"""

    def test_默认30分钟且只在交易时段(self):
        from datetime import datetime, timedelta
        self.assertEqual(HP.POLL_INTERVAL_SEC, 1800)
        now = datetime(2026, 10, 29, 10, 30)
        self.assertTrue(HP.should_poll(now, now - timedelta(minutes=31)))
        self.assertFalse(HP.should_poll(now, now - timedelta(minutes=10)), "不到 30 分钟不许再跑")
        self.assertFalse(HP.should_poll(datetime(2026, 10, 29, 12, 0), None), "午休不跑")


if __name__ == "__main__":
    unittest.main(verbosity=2)
