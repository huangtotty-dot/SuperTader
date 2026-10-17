# -*- coding: utf-8 -*-
"""技术标签里的「顶/底背离」必须走**已验证口径**（2026-10-17 修复）。

事故：owner 报「飞龙股份 002536 在底部，技术标签却报顶背离」。根因是 `_stock_tags_from_df`
里有一份 `analysis/divergence.py` 的**手抄副本**，漏了三道闸：

  ① **新鲜度** —— 002536 的顶背离形成于 13 个交易日前（09-21 高点 56.80，10-16 已跌到 40.60），
     标签却一直挂着；主模块 `DAILY_ALERT_MAX_AGE_BARS=4`。
  ② **摆动门槛** —— 同一段筑顶里的两个小峰被当成"两个顶"（300475 案例）。
  ③ **价格超出量** —— 高出 0.1% 就算新高（002202 案例）。

全池实测（597 只）：老实现判顶背离 **52 只** → 修复后 **3 只**（bars_ago 3~4，都是新鲜事件）。

本测试钉两件事：
  A. **接线**：标签必须调用 `analysis.divergence.detect_daily_divergence`，
     而不是自己算 —— 用 monkeypatch 证明"下游说什么，标签就显示什么"。
  B. **真实回归**：002536 的缓存还在就必须**不出现**顶背离标签（缓存缺失则跳过）。

运行：python -m unittest tests.phase3.test_hunter_divergence_tag -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _api():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tg_div_test", _ROOT / "t_gui.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod.Api()


def _df(code, n=160):
    sys.path.insert(0, str(_ROOT))
    import core.chart_cache as cc
    df = cc.load_daily_display(code)
    if df is None or len(df) < 61:
        return None
    return df.tail(n).reset_index(drop=True)


class TestTagDelegatesToVerifiedDetector(unittest.TestCase):
    """A. 标签必须**照抄**下游检测器的结论（防止有人再把逻辑抄回来）。"""

    def setUp(self):
        self.api = _api()
        self.df = _df("002536")
        if self.df is None:
            self.skipTest("002536 日线缓存缺失")
        import analysis.divergence as dv
        self.dv = dv
        self._orig = dv.detect_daily_divergence

    def tearDown(self):
        self.dv.detect_daily_divergence = self._orig

    def _labels(self):
        return [t["label"] for t in self.api._stock_tags_from_df(self.df, "002536", t30=False)["tags"]]

    def test_follows_detector_none(self):
        self.dv.detect_daily_divergence = lambda *a, **k: {}
        self.assertNotIn("日线顶背离", self._labels())
        self.assertNotIn("日线底背离", self._labels())

    def test_follows_detector_top(self):
        self.dv.detect_daily_divergence = lambda *a, **k: {
            "type": "顶背离", "bars_ago": 3, "time": "2026-10-13"}
        tags = self.api._stock_tags_from_df(self.df, "002536", t30=False)["tags"]
        top = [t for t in tags if t["label"] == "日线顶背离"]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["color"], "warn")
        # 必须带出"多久之前的"——否则用户无法判断是今天的还是两周前的
        self.assertIn("3", top[0]["title"])
        self.assertIn("2026-10-13", top[0]["title"])

    def test_follows_detector_bottom(self):
        self.dv.detect_daily_divergence = lambda *a, **k: {
            "type": "底背离", "bars_ago": 2, "time": "2026-10-14"}
        self.assertIn("日线底背离", self._labels())


class TestRealRegression002536(unittest.TestCase):
    """B. 真实回归：这只票在底部，**不许**再出顶背离标签。"""

    def test_no_false_top_divergence(self):
        df = _df("002536")
        if df is None:
            self.skipTest("002536 日线缓存缺失")
        tags = _api()._stock_tags_from_df(df, "002536", t30=False)["tags"]
        labels = [t["label"] for t in tags]
        self.assertNotIn("日线顶背离", labels, "002536 的顶背离是 13 个交易日前的事件，必须已过期")
        # 下行趋势与破位标签仍应在（证明不是把整列标签关掉了）
        self.assertIn("破5日线", labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
