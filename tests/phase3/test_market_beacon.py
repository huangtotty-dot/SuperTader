# -*- coding: utf-8 -*-
"""市场风向标（`t_gui.load_market_beacon`）+ 它的两个地基件 的单测（2026-10-13）。

## 为什么要有这个文件

风向标 = 「人气概念板块 × 代表的龙头/中军」，列沿用建仓信号扫描。它踩在三块**别处已经存在**
的东西上，任一块漂移都会让面板**静默变错**（不报错、只是显示成别的意思）：

1. **`_jiuyan_board()` 是「主板块」口径的第二份实现。** 真源是策略包的
   `oversold_gap_confirm.prescreen.sym2board_map()`，但 t_gui **不该 import 策略包**
   （`execution/auto/` 是实盘宿主目录、且策略包只存在于某一个部署里）。⇒ 刻意复制一份，
   由本文件**逐码比对**守卫 —— 这是 2026-10-13 那次「背离判定副本漂移、同一个 bug 发作第二次」
   的教训：**副本可以存在，但必须有机械闸**。
2. **`daily_summary.json` 的板块名必须能 join 上韭研表的主板块名**。join 不上时
   `load_market_beacon` 会「该板块一只成分股都没有」⇒ 面板**静默少几行**，看起来像"今天没热度"。
   实测 20/20 全中，但它是**跨两个子系统的隐含契约**，必须钉住。
3. **`_tier_sector_stocks` 的入参字段名**是 `change_pct`(百分数) / `amount`。传错名字不会报错，
   只会让 `ok` 列表为空 ⇒ 全板块都不分层 ⇒ 面板空白。

运行：python -m unittest tests.phase3.test_market_beacon -v
"""
import json
import sys
import time as _time
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "execution" / "auto") not in sys.path:
    sys.path.insert(0, str(_ROOT / "execution" / "auto"))

import t_gui                                              # noqa: E402


class TestBoardMapSingleSource(unittest.TestCase):
    """`_jiuyan_board()` 必须与策略包的 `sym2board_map()` **逐码一致**。

    两者都在「韭研主板块」这一个概念上，**改任一侧都要同步另一侧**。
    """

    @classmethod
    def setUpClass(cls):
        from oversold_gap_confirm import prescreen
        cls.ref = prescreen.sym2board_map()
        cls.mine = t_gui._jiuyan_board()

    def test_same_codes(self):
        self.assertEqual(set(self.ref), set(self.mine),
                         "两份实现的代码集合不一致（有码漏了或有码多了）")

    def test_same_boards(self):
        diff = {c: (self.ref.get(c), self.mine.get(c))
                for c in set(self.ref) | set(self.mine)
                if self.ref.get(c) != self.mine.get(c)}
        self.assertFalse(diff, f"主板块口径漂移（前 5 个）：{list(diff.items())[:5]}")

    def test_nonempty_boards_exist(self):
        n = sum(1 for v in self.mine.values() if v)
        self.assertGreater(n, 1000, f"有板块归属的码只有 {n} 个 —— 韭研表可能没读到")
        self.assertIn("医药", set(self.mine.values()),
                      "MERGES 生效时应有「医药」（创新药/中药 合并而来）")


class TestSectorJoin(unittest.TestCase):
    """`daily_summary.json` 的板块名 ⇄ 韭研主板块名 —— 面板能不能出行全靠这个 join。"""

    def test_top_sectors_all_joinable(self):
        fp = _ROOT / "stock_hunter" / "history" / "daily_summary.json"
        if not fp.exists():
            self.skipTest("无 daily_summary.json")
        s = json.loads(fp.read_text(encoding="utf-8"))
        days = sorted(k for k in s if s.get(k))
        self.assertTrue(days, "daily_summary 里没有非空日")
        bmap = t_gui._jiuyan_board()
        from collections import Counter
        cnt = Counter(v for v in bmap.values() if v)
        missing = [x.get("板块") for x in (s[days[-1]] or [])[:20]
                   if not cnt.get(str(x.get("板块") or ""))]
        self.assertFalse(missing,
                         f"最新日(={days[-1]}) 前 20 板块里有 join 不上的：{missing} —— "
                         "面板会静默少行（看起来像『今天没热度』）")


class TestLoadMarketBeacon(unittest.TestCase):
    """端到端：真跑（**只读缓存，零网络**）。

    ⚠️ 它跑的是**两级 SWR 契约**（2026-10-13 改）：**骨架**（成分/涨幅/分档 ≈6s）与
    **重列**（建仓判定/得分/标签 ≈18s）**都在后台算**，主线程只做缓存读。

    最初版本是**同步**补列的（88 只 × `_hunter_build_conformance` ≈5s，全在 pywebview
    主线程上）—— 那正是本仓每一次 GUI 冻死的老形状。所以下面把「首帧不得阻塞」作为
    **性能契约**钉住：两级都要能在冷缓存下断言到 pending。
    """

    @classmethod
    def setUpClass(cls):
        cls.api = t_gui.Api()
        # 冷缓存：这样才能证明首帧**不在主线程同步算**（否则会命中上一轮的缓存）
        t_gui._BEACON_SKELETON.update({"key": None, "ts": 0.0, "val": None, "running": False})
        t_gui._BEACON_ENRICH.update({"key": None, "ts": 0.0, "rows": None, "running": False})
        cls.first = cls.api.load_market_beacon()          # 冷：骨架都还没算
        cls.r = cls._poll(lambda rr: not rr.get("pending"), 60)   # 等骨架落地

    @classmethod
    def _poll(cls, done, timeout, top_n=None):
        rr = None
        t0 = _time.time()
        while _time.time() - t0 < timeout:
            rr = cls.api.load_market_beacon(top_n=top_n)
            if done(rr):
                return rr
            _time.sleep(2)
        return rr

    def test_first_call_is_swr_not_sync(self):
        """**性能契约**：冷缓存下首帧必须 `pending=True`（连骨架都还没算）。

        挂了就说明有人把某级改回**主线程同步算**了 —— 骨架 ≈6s / 重列 ≈18s，
        而 `refreshLive` 每 10s 还会重渲染一次。
        """
        self.assertTrue(self.first.get("pending"),
                        "首帧就把骨架算完了 ⇒ 又变回主线程同步算（GUI 冻死的老形状）")

    def test_available_and_has_sectors(self):
        self.assertTrue(self.r.get("available"), f"未返回可用数据：{self.r.get('error')}")
        self.assertTrue(self.r.get("sectors"), "一个板块都没有")
        self.assertFalse(self.r.get("pending"), "骨架应在超时内落地")

    def test_all_sectors_not_topn(self):
        """owner 2026-10-13：**显示所有板块**（不再只取前 N）。"""
        fp = _ROOT / "stock_hunter" / "history" / "daily_summary.json"
        if not fp.exists():
            self.skipTest("无 daily_summary.json")
        day = self.r.get("date")
        raw = json.loads(fp.read_text(encoding="utf-8")).get(str(day)) or []
        # 有分层的板块数 ≤ 当日板块总数，且**不应被截断成一个常数**
        self.assertLessEqual(len(self.r["sectors"]), len(raw))
        self.assertGreater(len(self.r["sectors"]), 8,
                           f"只有 {len(self.r['sectors'])} 个板块 —— 像是又被 top_n 截断了")
        self.assertEqual(self.r.get("top_n"), 0, "top_n=None 应回传 0（=全部）")

    def test_light_columns_present_on_first_skeleton_call(self):
        """轻列（缓存读）骨架落地时就该有，否则列会白着。"""
        m = self.r["sectors"][0]["rows"][0]
        for k in ("code", "name", "tier", "change_pct", "amount", "structure",
                  "m30_verdict", "tags"):
            self.assertIn(k, m, f"行里缺轻列 {k} —— 对应的列会显示成 —")
        self.assertIn("level", m["m30_verdict"] or {}, "30min 判定应为 {level,label,reason}")

    def test_heavy_columns_arrive_after_enrich(self):
        """重列由后台线程补齐（实测 t+8s 出判定、t+12s 出标签）⇒ 轮询到落地再验字段齐备。

        ⚠️ **只取 top_n=3**（约 46 只）：全板块是 332 只、后台补列 ~18s+，再叠上同批其它
        用例与在跑的 GUI/策略抢 CPU，90s 超时**会偶发不够**（实测遇到过一次 FAILED 但复跑即绿）。
        测试不该是时间敏感的 —— 小样本同样能验"重列会落地"，且把等待压到几秒。
        """
        rr = self._poll(lambda x: not x.get("enrich_pending"), 120, top_n=3)
        self.assertFalse((rr or {}).get("enrich_pending"), "后台补列 90s 仍未落地")
        m = rr["sectors"][0]["rows"][0]
        for k in ("build_verdict", "build_score", "build_met", "build_conds",
                  "build_reason", "build_above_ma5"):
            self.assertIn(k, m, f"重列落地后行里仍缺 {k}")

    def test_rows_are_lead_and_mid_only(self):
        """**只出 龙头/中军**，不得混入杂毛（owner 口径：代表公司）。"""
        bad = [(s["name"], m["code"], m["tier"])
               for s in self.r["sectors"] for m in s["rows"]
               if m.get("tier") not in ("龙头", "中军")]
        self.assertFalse(bad, f"混入了非代表档：{bad[:5]}")

    def test_no_scan_only_fields(self):
        """**刻意不含**「池」与「扫描」：本面板不是扫描产物。

        若哪天后端开始返回 pool/scan_time，说明有人把它接到了扫描线上 —— 那时前端那两列
        的取舍要重新定，别默认它还是"没有"。
        """
        m = self.r["sectors"][0]["rows"][0]
        self.assertNotIn("pool", m)
        self.assertNotIn("scan_time", m)

if __name__ == "__main__":
    unittest.main(verbosity=2)
