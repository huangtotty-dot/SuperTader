# -*- coding: utf-8 -*-
"""猎手「当日 bar 回写共享缓存」的回归守卫（2026-10-12）。

**这是一类已经复发 3 次的缺陷**：凡用"最新一根"算均线，都要先问**这根真的是今天的吗**。
  · 600362 误报破 5 日线（payload 停滞用旧序列）
  · 600362 误报破 5 日线（补 forming bar 把昨收重复计入 MA5）
  · **000681 视觉中国**（本次）：猎手技术标签报"破5日线"，同花顺显示**站上**。

**本次根因**：`fetch_one` 里那段"当日回写共享缓存"被**一个性能优化悄悄削掉**了 ——
提交 a13a9b5「取数改走本地缓存」引入了 `_bulk_from_cache`（"来自本地缓存的码 → 值未变，
省去回写 merge"），但它是**无条件** add；而 `chart_cache.load_daily_display` 的新鲜度闸
放到 **12 个自然日** ⇒ "末根停在上一交易日"的缓存也被当成"值未变" ⇒ **跳过回写**
⇒ 共享缓存永远补不上当日 bar ⇒ 零网络的消费端（技术标签 / 建仓符合度）一直按陈旧序列算。

实测（000681）：缓存末根 10-09 close=14.98 ⇒ MA5=15.48 ⇒ **误报破位**；
当日实时 16.69 ⇒ MA5=15.70 ⇒ 实为**站上**。

修法两处（本文件同时钉住）：
  ① `_bulk_from_cache.add` 只在**缓存末根 == 目标日**时才加；
  ② 回写条件加"手上确实有当日 bar"（避免无谓 I/O）。

⚠️ 这两条在 `_fetch_historical_tencent` 内部（要真实网络才能跑通），故用**源码守卫**而非行为测试
—— 与本仓 `TestT6GmMainStatics` 同一做法。

运行：python -m unittest tests.phase3.test_hunter_daily_cache_writeback -v
"""
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "stock_hunter" / "modules" / "market_data.py"


class TestDailyCacheWriteback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _SRC.read_text(encoding="utf-8")

    def test_stale_cache_not_marked_unchanged(self):
        """① `_bulk_from_cache` 不能无条件 add —— 否则陈旧缓存永远不回写。"""
        i = self.src.index("_bulk_from_cache.add")
        window = self.src[max(0, i - 500):i]
        self.assertIn('str(target_date_str)[:10]', window,
                      "add 之前必须先判『缓存末根 == 目标日』，否则 12 天新鲜度闸内的陈旧缓存"
                      "会被当成『值未变』⇒ 共享缓存永远补不上当日 bar ⇒ 误报破5日线")

    def test_writeback_requires_today_bar(self):
        """② 回写处要确认手上真有当日 bar（否则是白写 I/O）。

        ⚠️ 针串不得带右括号：调用点带 `forming_date=` 实参（2026-10-21 加），
        写 `merge_daily_cache(code, df)` 会 ValueError 让本守卫**整个死掉**（曾经漂移过一次）。
        窗口 500→700：forming 注释两行把日期闸推到了 500 之外。"""
        i = self.src.index("merge_daily_cache(code, df,")
        window = self.src[max(0, i - 700):i]
        self.assertIn('str(df["date"].iloc[-1])[:10]', window,
                      "回写前应确认末根是目标日")

    def test_writeback_still_guarded_by_cache_membership(self):
        """别把优化整个删掉：来自缓存**且缓存已含当日**的码仍应跳过回写（省 I/O）。"""
        i = self.src.index("merge_daily_cache(code, df,")
        window = self.src[max(0, i - 500):i]
        self.assertIn("_bulk_from_cache", window, "回写仍应受 _bulk_from_cache 豁免")


class TestFreshnessGapGuardsTheSameClass(unittest.TestCase):
    """记录"为什么巡检没先抓到"：新鲜度巡检的日线阈值是**12 个自然日**（对齐展示口径），
    而 000681 那种"停在上一个交易日"只有 3 天 ⇒ 判为新鲜 ⇒ 不报。

    ⇒ 对"要用最新一根算均线"的消费端，判据应是**『是不是今天』**，不是"多久没更新"。
    本测试只是把这个认知固定在测试里，防止日后有人以为巡检已经覆盖了这类问题。
    """

    def test_daily_threshold_is_lax_for_ma_consumers(self):
        import core.chart_cache as cc
        self.assertGreaterEqual(cc.MAX_DISPLAY_GAP_DAYS, 7,
                                "展示口径允许长假 ⇒ 天然覆盖不了『今天缺 bar』这类缺陷")


if __name__ == "__main__":
    unittest.main(verbosity=2)
