# -*- coding: utf-8 -*-
"""选股猎手 · 「建仓信号（按板块）」推送 · 候选提取单测（2026-09-21）。

## 为什么要有这个测试

owner 需求：猎手里出现「符合建仓条件」的股票时推送到飞书，按板块划分。
「符合建仓条件」= GUI「建仓」列的绿色 `x·GO`，即 `t_gui._hunter_build_conformance`
的时机门控 GO（市场有方向/多头结构/回撤到位/金叉加分）。

本测试**只测纯提取函数 `build_build_candidates`**（不联网、不发消息）。

⚠️ 绝不在此调用 `send_build_candidates` —— 那会真的往 owner 的飞书群发消息。

覆盖：只取 GO、按板块分组、板块与组内排序、细分拼接、无候选返回空、
       非列表/缺字段不炸（猎手数据来自多来源，字段可能缺）。

运行：python tests/stage0_2/test_hunter_build_push.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HUNTER = os.path.join(_ROOT, "stock_hunter")
for _p in (_ROOT, _HUNTER):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.push_feishu import build_build_candidates  # noqa: E402
import modules.push_feishu as pf  # noqa: E402


def _stock(code, name, go, met, score, chg=0.0, concepts=None):
    return {"code": code, "name": name, "score": score, "change_pct": chg,
            "concepts": concepts or [], "build_go": go, "build_met": met,
            "build_reason": f"符合{met}/4"}


class TestBuildBuildCandidates(unittest.TestCase):
    def test_only_go_stocks_selected(self):
        sec = {"半导体": [
            _stock("000988", "华工科技", True, 3, 23),
            _stock("300548", "博创科技", False, 2, 11),   # 未 GO → 不应入选
            _stock("688037", "芯源微", False, 2, 12),
        ]}
        g = build_build_candidates(sec)
        self.assertEqual(len(g), 1)
        self.assertEqual([s["代码"] for s in g[0]["stocks"]], ["000988"])

    def test_grouped_by_sector(self):
        sec = {
            "半导体": [_stock("000988", "华工科技", True, 3, 23)],
            "医药": [_stock("300204", "舒泰神", True, 2, 7),
                     _stock("000538", "云南白药", True, 3, 9)],
        }
        g = build_build_candidates(sec)
        self.assertEqual([x["sector"] for x in g], ["医药", "半导体"],
                         "候选多的板块应排在前")
        self.assertEqual(len(g[0]["stocks"]), 2)
        # 组内：符合数 desc → 得分 desc
        self.assertEqual([s["代码"] for s in g[0]["stocks"]], ["000538", "300204"])

    def test_sort_within_sector_by_met_then_score(self):
        sec = {"X": [
            _stock("a", "A", True, 2, 30),
            _stock("b", "B", True, 3, 10),
            _stock("c", "C", True, 3, 20),
        ]}
        g = build_build_candidates(sec)
        self.assertEqual([s["代码"] for s in g[0]["stocks"]], ["c", "b", "a"],
                         "先按符合数 desc；同为 3/4 时按得分 desc")

    def test_concepts_joined(self):
        sec = {"医药": [_stock("000538", "云南白药", True, 3, 9,
                               concepts=["中药", "百年老字号"])]}
        g = build_build_candidates(sec)
        self.assertEqual(g[0]["stocks"][0]["细分"], "中药|百年老字号")

    def test_no_candidates_returns_empty(self):
        sec = {"半导体": [_stock("000988", "华工科技", False, 2, 23)]}
        self.assertEqual(build_build_candidates(sec), [])

    def test_robust_to_missing_or_empty(self):
        """猎手数据字段可能缺（-- 显示为 None/缺失），不能炸。"""
        for bad in (None, {}, {"板块": None}, {"板块": []},
                    {"板块": [{"code": "1"}]}):     # 缺 build_go → 视为未 GO
            with self.subTest(bad=bad):
                self.assertEqual(build_build_candidates(bad), [])

    def test_missing_optional_fields_default(self):
        sec = {"X": [{"code": "600000", "build_go": True}]}   # 只有 code + go
        g = build_build_candidates(sec)
        self.assertEqual(len(g), 1)
        s = g[0]["stocks"][0]
        self.assertEqual(s["名称"], "")
        self.assertEqual(s["总得分"], 0)
        self.assertEqual(s["符合数"], 0)
        self.assertEqual(s["细分"], "")


class TestPerSectorMessage(unittest.TestCase):
    """owner 要求：每个板块一条独立消息，不合并成一条。"""

    def setUp(self):
        self.sent = []

        class _FakeClient:
            def __init__(self, app_id, app_secret):
                pass

            def send_post_message(self, chat_id, title, lines):
                self_ = None
                _record({"title": title, "lines": list(lines)})
                return {"ok": True}

        def _record(msg):
            self.sent.append(msg)

        self._orig_client, self._orig_post = pf.FeishuAppClient, pf.send_post
        pf.FeishuAppClient = _FakeClient
        pf.send_post = lambda url, title, lines: (_record({"title": title, "lines": list(lines)})
                                                 or {"ok": True})

    def tearDown(self):
        pf.FeishuAppClient, pf.send_post = self._orig_client, self._orig_post

    def test_one_message_per_sector(self):
        groups = build_build_candidates({
            "医药": [_stock("000538", "云南白药", True, 3, 9),
                     _stock("300204", "舒泰神", True, 2, 7)],
            "半导体": [_stock("000988", "华工科技", True, 3, 23)],
        })
        cfg = {"feishu": {"app_id": "x", "app_secret": "y", "chat_id": "z"}}
        r = pf.send_build_candidates(cfg, groups, "20260921")

        self.assertEqual(len(self.sent), 2, "应每个板块一条消息")
        self.assertEqual(r["sent"], 2)
        self.assertTrue(r["ok"])
        titles = [m["title"] for m in self.sent]
        self.assertTrue(any("医药" in t for t in titles))
        self.assertTrue(any("半导体" in t for t in titles))
        # 每条消息只含自己板块的股票
        med = next(m for m in self.sent if "医药" in m["title"])
        body = "\n".join(med["lines"])
        self.assertIn("000538", body)
        self.assertIn("300204", body)
        self.assertNotIn("000988", body, "医药那条不该混入半导体的股票")

    def test_failure_of_one_sector_does_not_block_rest(self):
        def _boom(self, chat_id, title, lines):
            if "半导体" in title:
                raise RuntimeError("网络抖动")
            return {"ok": True}
        pf.FeishuAppClient.send_post_message = _boom
        pf.send_post = lambda url, title, lines: (_boom(None, None, title, lines)
                                                 if "半导体" in title else {"ok": True})
        groups = build_build_candidates({
            "医药": [_stock("000538", "云南白药", True, 3, 9)],
            "半导体": [_stock("000988", "华工科技", True, 3, 23)],
        })
        cfg = {"feishu": {"app_id": "x", "app_secret": "y", "chat_id": "z"}}
        r = pf.send_build_candidates(cfg, groups, "20260921")
        self.assertEqual(r["sent"], 1)
        self.assertEqual(r["failed"], 1)
        self.assertFalse(r["ok"], "有板块失败时应如实反映")


class TestPushDedup(unittest.TestCase):
    """定时自动运行按板块当日去重：候选集不变不重推，出现新票才推该板块。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._td = tempfile.TemporaryDirectory()
        self._fp = Path(self._td.name) / "pushed.json"

        import t_gui
        self.t_gui = t_gui
        self._orig_fp = t_gui._HUNTER_BUILD_PUSHED_FP
        t_gui._HUNTER_BUILD_PUSHED_FP = self._fp

        self.calls = []
        self._orig_send = pf.send_build_candidates
        pf.send_build_candidates = lambda cfg, groups, d: (
            self.calls.append([(g["sector"], sorted(s["代码"] for s in g["stocks"])) for g in groups])
            or {"ok": True, "sent": len(groups), "failed": 0})
        self.api = t_gui.Api()

    def tearDown(self):
        pf.send_build_candidates = self._orig_send
        self.t_gui._HUNTER_BUILD_PUSHED_FP = self._orig_fp
        self._td.cleanup()

    @staticmethod
    def _res(med=(), semi=()):
        return {"sector_stocks": {
            "医药": [_stock(c, c, True, 2, 5) for c in med],
            "半导体": [_stock(c, c, True, 2, 5) for c in semi],
        }}

    def _push(self, res, dedup):
        self.api._push_hunter_build_candidates(res, "2026-09-21", dedup=dedup)

    def test_auto_run_skips_when_unchanged_then_pushes_new_stock(self):
        self._push(self._res(med=["000538", "300204"]), dedup=True)
        self.assertEqual(len(self.calls), 1, "首次应推医药")
        self._push(self._res(med=["000538", "300204"]), dedup=True)
        self.assertEqual(len(self.calls), 1, "候选集未变 → 不应重复推")
        self._push(self._res(med=["000538", "300204", "600276"]), dedup=True)
        self.assertEqual(len(self.calls), 2, "出现新票 → 应再推该板块")
        self.assertEqual(self.calls[-1], [("医药", ["000538", "300204", "600276"])])

    def test_only_changed_sector_is_repushed(self):
        self._push(self._res(med=["000538"], semi=["000988"]), dedup=True)
        self.assertEqual(len(self.calls), 1)
        self._push(self._res(med=["000538"], semi=["000988", "300548"]), dedup=True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1], [("半导体", ["000988", "300548"])],
                         "只有变化的板块被重推，医药不动")

    def test_manual_run_always_pushes(self):
        for _ in range(3):
            self._push(self._res(med=["000538"]), dedup=False)
        self.assertEqual(len(self.calls), 3, "手动运行不去重，每次都推")

    def test_no_candidates_never_pushes(self):
        self._push({"sector_stocks": {"医药": [_stock("1", "X", False, 1, 1)]}}, dedup=True)
        self.assertEqual(self.calls, [])


class TestIntradayForce(unittest.TestCase):
    """2026-09-21 owner 拍板：盘中也算建仓符合度——但**仅定时自动运行**；
    手动点击盘中仍跳过（维持省资源）。否则 10:30~14:30 的自动运行算不出信号、
    建仓推送永远只能在盘后发生。"""

    def setUp(self):
        import t_gui
        self.t_gui = t_gui
        self.api = t_gui.Api()
        self._orig_dt = t_gui.datetime

    def tearDown(self):
        self.t_gui.datetime = self._orig_dt

    def _freeze(self, y, mo, d, hh, mm):
        real = self._orig_dt

        class _FrozenDT(real):
            @classmethod
            def now(cls, tz=None):
                return real(y, mo, d, hh, mm)

        self.t_gui.datetime = _FrozenDT

    def test_force_bypasses_intraday_skip(self):
        # 2026-09-21 是周一；10:30 属盘中跳过窗口(09:15-15:00)
        self._freeze(2026, 9, 21, 10, 30)
        codes = ["300153", "002639", "600176"]

        manual = self.api._hunter_build_conformance(codes, "2026-09-21")
        self.assertEqual(manual, {}, "手动盘中应跳过（省资源）")

        forced = self.api._hunter_build_conformance(codes, "2026-09-21", force=True)
        self.assertTrue(forced, "定时自动运行(force)盘中也必须算出结果，否则建仓推送永远只在盘后")

    def test_manual_still_computes_after_close(self):
        self._freeze(2026, 9, 21, 16, 0)          # 盘后
        r = self.api._hunter_build_conformance(["300153"], "2026-09-21")
        self.assertTrue(r, "盘后手动运行应正常计算")


class TestAutorunSlots(unittest.TestCase):
    """定时时点：跳午休、收盘后不跑。"""

    def test_slots(self):
        import t_gui
        self.assertEqual(list(t_gui.HUNTER_AUTORUN_SLOTS),
                         ["10:30", "11:30", "13:30", "14:30"])
        self.assertNotIn("12:30", t_gui.HUNTER_AUTORUN_SLOTS, "午休不该跑（数据无变化）")
        for s in t_gui.HUNTER_AUTORUN_SLOTS:
            hh, mm = (int(x) for x in s.split(":"))
            self.assertTrue(930 < hh * 100 + mm < 1500,
                            f"{s} 应落在开盘后、收盘前")
        self.assertFalse(t_gui._HUNTER_AUTORUN_STATE.get("started"),
                         "导入 t_gui 不该自动启动调度线程（只在 __main__ 启动）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
