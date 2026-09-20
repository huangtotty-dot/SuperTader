# -*- coding: utf-8 -*-
"""建仓股池「添加并立即扫描」离线单测（2026-09-20）。

## 为什么要有这个测试

GUI「+ 添加」按钮走 `Api.add_and_scan()`：先写股池，再对该股跑一次单股扫描。
这条链路有三处**静默失败**很难靠肉眼发现，且都会让用户看到"添加了却没反应"：

1. **股池写失败却仍去扫描** —— 扫描必然空返回，用户拿到一个含糊的结果。
   正确行为：写失败直接回传原因、**不触发扫描**。
2. **扫描抛异常被吞** —— 股池其实加成功了，但该股不会出现在下表；
   若把异常当成整体失败，用户会以为没加进去而重复添加。
   正确行为：`ok` 仍为 True，另带 `scan_error` 让前端讲清楚。
3. **异常文本/字段缺失** —— 前端要拿 `verdict / composite_score / block_reason` 渲染弹窗，
   任一为 `None` 都不该崩。

本测试全部 monkeypatch，**不联网**。

运行：python tests/stage0_2/test_add_and_scan.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "config") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "config"))

import core.position_builder as pb_mod  # noqa: E402
import t_gui  # noqa: E402

DATE = datetime.now().strftime("%Y-%m-%d")


def _pick_auto_unheld():
    """挑一只当前「属 auto 池且未持有」的标的。
    动态选取而非硬编码：池归属会随 holdings 变动（实测 600584 就被 owner 从 auto 池删过），
    硬编码会让测试因数据漂移而假红。"""
    import auto_pool
    h = t_gui._load_json(t_gui.HOLDINGS, {})
    for c in sorted(auto_pool.AUTO_POOL):
        if not (h.get(c) or {}).get("qty"):
            return c, (h.get(c) or {}).get("name") or c
    return None, None


class TestAddAndScan(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._orig_add = self.api.add_to_watchlist
        self._orig_scan = pb_mod.run_position_scan
        self.scan_called = []

    def tearDown(self):
        self.api.add_to_watchlist = self._orig_add
        pb_mod.run_position_scan = self._orig_scan

    def _stub_add(self, result):
        self.api.add_to_watchlist = lambda code, name: result

    def _stub_scan(self, result=None, exc=None):
        def _f(*a, **k):
            self.scan_called.append(k)
            if exc:
                raise exc
            return result
        pb_mod.run_position_scan = _f

    # ── 1) 股池写入失败 → 回传原因且不扫描 ──
    def test_pool_write_failure_skips_scan(self):
        self._stub_add({"ok": False, "error": "磁盘只读"})
        self._stub_scan([{"verdict": "signal"}])   # 若被调用，下面断言会失败

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "磁盘只读")
        self.assertEqual(self.scan_called, [], "股池写失败时不该触发扫描")

    # ── 2) 扫描抛异常 → ok 仍为 True，另带 scan_error ──
    def test_scan_exception_reported_not_fatal(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan(exc=RuntimeError("gm 行情服务不可达"))

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertTrue(r["ok"], "股池已加成功，不该因扫描失败而报整体失败")
        self.assertIn("gm 行情服务不可达", r["scan_error"])
        self.assertIsNone(r["scan"])

    # ── 3) 正常路径 → 回传 verdict / 得分 / 卡点 ──
    def test_success_returns_scan_detail(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan([{"verdict": "approaching", "composite_score": 62,
                          "block_reason": "卡「回撤到位」：还差 3.2%"}])

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertTrue(r["ok"])
        self.assertIsNone(r["scan_error"])
        self.assertEqual(r["scan"]["verdict"], "approaching")
        self.assertEqual(r["scan"]["composite_score"], 62)
        self.assertIn("回撤到位", r["scan"]["block_reason"])
        self.assertEqual(r["code"], "600519")
        self.assertEqual(r["name"], "贵州茅台")
        # 扫描须按 target_code 指定该股、且不推飞书（避免重复打扰）
        kw = self.scan_called[0]
        self.assertEqual(kw.get("target_code"), "600519")
        self.assertTrue(kw.get("no_feishu"))
        self.assertTrue(kw.get("silent"))

    # ── 4) 扫描空返回 → 给出可读原因，而不是静默成功 ──
    def test_empty_scan_result_reports_reason(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan([])

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertTrue(r["ok"])
        self.assertIsNone(r["scan"])
        self.assertTrue(r["scan_error"], "空返回必须有可读原因，不能被当成成功")

    # ── 5) 单股扫描自身记了 scan_error（position_builder 既有约定）→ 透传 ──
    def test_inner_scan_error_surfaced(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan([{"verdict": "weak", "composite_score": 0,
                          "scan_error": "无日线数据"}])

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertTrue(r["ok"])
        self.assertIn("无日线数据", r["scan_error"])

    # ── 6) 前端要用的字段形态稳定 ──
    def test_returns_frontend_contract(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan([{"verdict": "weak", "composite_score": 0}])

        r = self.api.add_and_scan("600519", "贵州茅台")

        for k in ("ok", "code", "name", "scan_date", "visible_in_manual", "scan", "scan_error"):
            self.assertIn(k, r, f"缺字段 {k}（前端弹窗依赖）")
        self.assertIsInstance(r["visible_in_manual"], bool)
        self.assertRegex(r["scan_date"], r"^\d{4}-\d{2}-\d{2}$")
        for k in ("verdict", "composite_score", "block_reason", "reason"):
            self.assertIn(k, r["scan"], f"scan 缺字段 {k}（前端弹窗依赖）")

    # ── 7) insufficient_data 这类无卡点结论 → reason 兜底取 errors，不能只给英文枚举 ──
    def test_reason_falls_back_to_errors_for_insufficient_data(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan([{"verdict": "insufficient_data", "composite_score": 0,
                          "block_reason": None,
                          "errors": ["日线末 bar 陈旧(2026-09-18<2026-09-20)→ insufficient_data"]}])

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertEqual(r["scan"]["verdict"], "insufficient_data")
        self.assertIsNone(r["scan"]["block_reason"])
        self.assertIn("陈旧", r["scan"]["reason"], "无卡点时应由 errors 兜底出可读原因")

    # ── 8) 有卡点时 reason 优先用卡点（更精确）──
    def test_reason_prefers_block_reason(self):
        self._stub_add({"ok": True, "code": "600519"})
        self._stub_scan([{"verdict": "weak", "composite_score": 10,
                          "block_reason": "卡「市场有方向」：regime=range",
                          "errors": ["其他无关错误"]}])

        r = self.api.add_and_scan("600519", "贵州茅台")

        self.assertEqual(r["scan"]["reason"], "卡「市场有方向」：regime=range")

    # ── 9) auto 池标的也能加进人工盘（2026-09-20 owner 拍板：两池互不阻塞）──
    def test_auto_pool_stock_can_be_added(self):
        """auto 池且未持有的标的也能加入人工盘股池（用临时 STATE_DIR，不动真实股池）。"""
        code, name = _pick_auto_unheld()
        if not code:
            self.skipTest("当前无「auto 池且未持有」的标的可测")

        with tempfile.TemporaryDirectory() as td:
            orig = t_gui.STATE_DIR
            t_gui.STATE_DIR = Path(td)
            try:
                r = self.api.add_to_watchlist(code, name)
                entry = json.loads(
                    (Path(td) / "watchlist_buy.json").read_text(encoding="utf-8"))["stocks"][code]
            finally:
                t_gui.STATE_DIR = orig

        self.assertTrue(r["ok"], f"auto 池标的应可加入人工盘，实际 {r}")
        # pool 必须仍写 auto：若写成 manual，引擎 validate_pool_split 会判定
        # manual∩auto 冲突并**拒绝启动**（main.py / gm_main.py 双守卫）
        self.assertEqual(entry.get("pool"), "auto",
                         "auto 池标的的 pool 字段必须写 auto，否则引擎启动守卫会拒绝启动")

    # ── 10) 人工池标的照常可加 ──
    def test_manual_pool_stock_can_be_added(self):
        """600519 不在 AUTO_POOL（用临时 STATE_DIR，不动真实股池）。"""
        with tempfile.TemporaryDirectory() as td:
            orig = t_gui.STATE_DIR
            t_gui.STATE_DIR = Path(td)
            try:
                r = self.api.add_to_watchlist("600519", "贵州茅台")
            finally:
                t_gui.STATE_DIR = orig

        self.assertTrue(r["ok"], f"人工池标的应可添加，实际 {r}")

    # ── 11) 人工盘表可见性：显式加进股池的都显示（含 auto 池标的）──
    def test_visible_shows_auto_pool_watchlist_entries(self):
        """回归那次「添加成功却看不到」：auto 池标的只要在股池里就必须可见。

        用临时 TRACES/STATE_DIR 造一条 auto 池标的的扫描轨迹，断言它出现在建仓表
        （此前 _visible 会把 auto 池+未持有的滤掉）。全程不碰真实股池/轨迹。
        """
        code, name = _pick_auto_unheld()
        if not code:
            self.skipTest("当前无「auto 池且未持有」的标的可测")
        import auto_pool
        self.assertFalse(auto_pool.is_manual(code), f"{code} 应属 auto 池（本用例前提）")

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "traces").mkdir()
            (td / "state").mkdir()
            (td / "state" / "watchlist_buy.json").write_text(
                json.dumps({"stocks": {code: {"name": name,
                                              "status": "monitoring", "pool": "auto"}}}),
                encoding="utf-8")
            (td / "traces" / f"position_builder_{DATE}.jsonl").write_text(
                json.dumps({"code": code, "name": name, "scan_type": "manual",
                            "scan_time": f"{DATE} 09:00:00", "verdict": "weak",
                            "composite_score": 5}) + "\n", encoding="utf-8")
            o_tr, o_sd, o_hl = t_gui.TRACES, t_gui.STATE_DIR, t_gui.HOLDINGS
            t_gui.TRACES, t_gui.STATE_DIR = td / "traces", td / "state"
            t_gui.HOLDINGS = td / "state" / "holdings.json"   # 不存在 → 无持仓
            try:
                agg = self.api._agg_position_builder(DATE)
            finally:
                t_gui.TRACES, t_gui.STATE_DIR, t_gui.HOLDINGS = o_tr, o_sd, o_hl

        codes = [r.get("code") for r in (agg.get("rows") or [])]
        self.assertIn(code, codes,
                      f"auto 池标的 {code} 只要在股池里就该出现在人工盘建仓表（不得按池隐藏）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
