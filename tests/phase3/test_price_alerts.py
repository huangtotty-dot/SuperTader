# -*- coding: utf-8 -*-
"""画框提醒（2026-10-28）：存储 + 穿越口径 + **可重复报**的回归。

守五条：
  ① 同一个框重复画幂等（不会在图上叠两个，也不会变成两个各自报一遍）；
  ② 只认穿越：上一拍 < 上沿 ≤ 这一拍 才算上破；上一拍 > 下沿 ≥ 这一拍 才算下破。
     画框时把现价写进 last_px ⇒ "框整个画在现价上方/下方"不会一画上去就误报；
  ③ **每次穿越都报**（owner：「价格有可能全天波动 一会在价格上面一会在价格下面
     需要连续报 不要只报一次」），但同侧要**价格先退回框内**才重新可报 ——
     贴边抖动（30.89/30.91）不许每 10s 刷一条；
  ④ 事件 id 必须**每次都不一样**：GUI 靠 id 去重响铃，id 固定成 `#up` 第二次就被吞；
  ⑤ 事件留痕（GUI 靠 events 响铃），只留最近 KEEP_EVENTS 条。

运行：python -m pytest -q tests/phase3/test_price_alerts.py
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import price_alerts as pa


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="pa_test_")) / "price_alerts.json"
        self._patch = mock.patch.object(pa, "STORE", self._tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def one(self, code="600900"):
        return pa.load()["items"][code][0]


class TestStore(_Base):
    def test_画框幂等(self):
        a = pa.add_box("600900", 30.90, 29.50, x0="2026-10-20", x1="2026-10-27", ref_px=30.10)
        b = pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        self.assertEqual(len(a), 1, "同一个框重复画不该叠两个")
        self.assertEqual(len(b), 1)
        self.assertEqual(len(pa.add_box("600900", 31.5, 29.5)), 2)

    def test_上下沿顺序自动纠正(self):
        pa.add_box("600900", 29.0, 31.0)          # 上沿 < 下沿 ⇒ 自动对调
        x = self.one()
        self.assertEqual((x["upper"], x["lower"]), (31.0, 29.0))

    def test_删除单条与全部(self):
        a = pa.add_box("600900", 30.9, 29.5)
        pa.add_box("600900", 33.0, 32.0)
        left = pa.remove("600900", a[0]["id"])
        self.assertEqual([x["upper"] for x in left], [33.0])
        self.assertEqual(pa.remove("600900", None), [])

    def test_坏文件不影响(self):
        self._tmp.write_text("{not json", encoding="utf-8")
        self.assertEqual(pa.load()["items"], {})
        self.assertEqual(pa.add_box("000001", 11.0, 10.0)[0]["upper"], 11.0)


class TestCrossOnly(_Base):
    def _ev(self, code, px, name=""):
        return pa.evaluate(code, self.one(code)["id"], px, name=name)

    def test_上破成立(self):
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        evs = self._ev("600900", 30.95, "长江电力")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["side"], "up")
        self.assertEqual(evs[0]["level"], 30.9)
        self.assertEqual(evs[0]["price"], 30.95)

    def test_下破成立(self):
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        evs = self._ev("600900", 29.40)
        self.assertEqual([e["side"] for e in evs], ["down"])

    def test_框整体在现价上方时不误报(self):
        """画框时现价 28.0，框 30.9~32.0 ⇒ 第一拍 28.1 既不破上沿也不破下沿。"""
        pa.add_box("600900", 32.0, 30.9, ref_px=28.0)
        self.assertEqual(self._ev("600900", 28.1), [])

    def test_框整体在现价下方时不误报(self):
        """画框时现价 35.0，框 30.9~32.0 ⇒ 价格 34.9 不该判成"跌破下沿"。"""
        pa.add_box("600900", 32.0, 30.9, ref_px=35.0)
        self.assertEqual(self._ev("600900", 34.9), [])

    def test_首次观测无ref_px不判(self):
        pa.add_box("000002", 42.0, 40.0)          # 没有现价参考
        self.assertEqual(self._ev("000002", 41.0), [], "第一拍只记价，不判穿越")

    def test_同拍不重复(self):
        """同一拍连着喂两次同一个价，第二次没有"前值"，不该再报。"""
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        self.assertEqual(len(self._ev("600900", 30.95)), 1)
        self.assertEqual(self._ev("600900", 30.95), [])


class TestRepeat(_Base):
    """2026-10-28 owner：**不要只报一次**。"""

    def _ev(self, code, px, name=""):
        return pa.evaluate(code, self.one(code)["id"], px, name=name)

    def test_来回穿越每次都报(self):
        """30.10 → 30.95(上破) → 30.60(退回) → 31.00(再上破) ⇒ 两条事件，up_n=2。"""
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        self.assertEqual(len(self._ev("600900", 30.95)), 1)
        self.assertEqual(self._ev("600900", 30.60), [], "退回框内这一次本身不是穿越")
        again = self._ev("600900", 31.00)
        self.assertEqual(len(again), 1, "第二次上破必须再报 —— 就是 owner 说的'连续报'")
        self.assertEqual(again[0]["side"], "up")
        self.assertEqual(again[0]["n"], 2)
        self.assertEqual(self.one("600900")["up_n"], 2)
        self.assertEqual(self.one("600900")["up_price"], 31.0, "只留最近一次的价格")

    def test_上下沿各自累计互不干扰(self):
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        self.assertEqual([e["side"] for e in self._ev("600900", 30.95)], ["up"])
        self.assertEqual([e["side"] for e in self._ev("600900", 29.40)], ["down"])
        self.assertEqual([e["side"] for e in self._ev("600900", 30.10)], [])
        self.assertEqual([e["side"] for e in self._ev("600900", 31.20)], ["up"])
        x = self.one("600900")
        self.assertEqual((x["up_n"], x["down_n"]), (2, 1))

    def test_贴边抖动不刷屏(self):
        """上沿 30.90，价格在 30.89/30.91 之间抖 —— 退回不足 0.2% ⇒ 不重新武装。"""
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        self.assertEqual(len(self._ev("600900", 30.91)), 1)
        for px in (30.89, 30.91, 30.895, 30.92, 30.88):
            self.assertEqual(self._ev("600900", px), [],
                             f"{px} 只退回不足 REARM_PCT，不该再报")
        self.assertEqual(self.one("600900")["up_n"], 1)

    def test_低价标的最小回框余量(self):
        """ETF 0.65：0.2% 只有 0.0013 < 最小变动价 ⇒ 兜底 0.01 元（10 跳）。"""
        pa.add_box("588170", 0.72, 0.65, ref_px=0.66)
        self.assertEqual([e["side"] for e in self._ev("588170", 0.649)], ["down"])
        self.assertEqual(self._ev("588170", 0.655), [], "0.655 还在币值噪声内")
        self.assertEqual([e["side"] for e in self._ev("588170", 0.661)], [])
        self.assertEqual([e["side"] for e in self._ev("588170", 0.648)], ["down"],
                         "退回 0.661 (>0.65+0.01) 后重新武装，再破就该再报")

    def test_事件id必须每次都不同(self):
        """GUI 靠 `e.id` 去重响铃：id 固定成 `#up` 的话，第二次穿越会被静默吞掉。"""
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        e1 = self._ev("600900", 30.95)[0]
        self._ev("600900", 30.60)
        e2 = self._ev("600900", 31.00)[0]
        self.assertNotEqual(e1["id"], e2["id"])
        self.assertEqual(len({e["id"] for e in pa.load()["events"]}), 2)

    def test_armed不再退休(self):
        """两边都报过之后仍然留在扫描列表里 —— 否则"全天波动"就只看得见第一次。"""
        pa.add_box("600900", 30.90, 29.50, ref_px=30.10)
        self._ev("600900", 30.95)
        self._ev("600900", 29.40)
        self.assertEqual(len(pa.armed()), 1)
        self.assertEqual(len(self._ev("600900", 30.20)), 0)
        self.assertEqual(len(self._ev("600900", 30.99)), 1, "退休了就不会有这条")

    def test_事件留痕与上限(self):
        """反复穿越也不能把 events 撑爆（GUI 每 10s 拉一次全量）。"""
        for i in range(30):
            code = f"0000{i:02d}"
            pa.add_box(code, 11.0, 10.0, ref_px=10.5)
            x = pa.load()["items"][code][0]
            for _ in range(5):                 # 每轮上破+下破各一次 ⇒ 30×10 = 300 条
                pa.evaluate(code, x["id"], 11.5)
                pa.evaluate(code, x["id"], 10.2)   # 回框内，两侧重新武装
                pa.evaluate(code, x["id"], 9.5)
                pa.evaluate(code, x["id"], 10.2)
        evs = pa.load()["events"]
        self.assertEqual(len(evs), pa.KEEP_EVENTS)
        self.assertEqual(sorted({e["side"] for e in evs}), ["down", "up"])
        self.assertEqual(len({e["id"] for e in evs}), pa.KEEP_EVENTS, "id 不能撞")


class TestLegacyStore(_Base):
    """v2 旧框（每边只报一次，没有 up_n）读进来要能直接用。"""

    def test_旧框补计数且立即可再报(self):
        import json
        self._tmp.write_text(json.dumps({"version": 2, "items": {"600900": [{
            "id": "600900-1", "code": "600900", "kind": "box",
            "upper": 30.9, "lower": 29.5, "up_at": "2026-10-28 09:15:09",
            "up_price": 30.95, "down_at": None, "down_price": None,
            "last_px": 30.1}]}, "events": []}), encoding="utf-8")
        x = pa.load()["items"]["600900"][0]
        self.assertEqual((x["up_n"], x["down_n"]), (1, 0), "旧 up_at 要补成 1 次")
        evs = pa.evaluate("600900", "600900-1", 30.95)
        self.assertEqual(len(evs), 1, "重启后照样能再报")
        self.assertEqual(evs[0]["n"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
