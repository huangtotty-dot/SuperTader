# -*- coding: utf-8 -*-
"""`tencent_provider.qt_vwap` —— 成交量**单位坑**的回归（2026-10-16）。**全离线**。

## 背景

owner 2026-10-16 拍板 A：把 `main.py::_refresh_spot_vwap_cache` 的数据源由
**akshare 全市场快照**（实测 97.7s + import 11.2s ≈ **109s/次**）换成**腾讯逐只快照**
（48ms/10 只）。动机：该函数在**逐只持仓循环里**被调，节流阈值 60s < 109s ⇒
**节流形同虚设、每只重拉一次**（7 只 ≈ 763s），正是主循环一轮 ~15 分钟的来源。

## 换源时踩到的坑（本文件钉的就是它）

腾讯 `f[6]`（成交量）**单位按板块不同，差 100 倍**：

| 板块 | `f[6]` 单位 | 公式 | 实测 |
|---|---|---|---|
| 主板 / 创业板 / ETF | **手** | 成交额 ÷ (f[6] × 100) | `600362`→39.438（akshare 39.44 ✅） |
| **科创板 `688xxx`** | **股** | 成交额 ÷ f[6] | `688008`→**169.229**（akshare 169.23 ✅） |

→ 首版按"手"一刀切 ⇒ **科创板整体偏小 100 倍**（1.690 vs 169.23）。
判据改用**不变量**：真 VWAP 是当日价的加权平均 ⇒ 必落在当日 `[low, high]` 内；
两个候选差 100 倍 ⇒ 至多一个在区间内。

运行：python -m unittest tests.phase3.test_qt_vwap_units -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.market_data import tencent_provider as TP  # noqa: E402


class TestVwapUnits(unittest.TestCase):
    """两个板块口径都要对 —— 只测一种就会漏掉 100 倍的那个。"""

    def test_lots_unit_main_board(self):
        """主板：f[6] 是「手」⇒ 成交额 ÷ (f[6]×100)。100 万元 / 1000 手 = 10 元/股。"""
        self.assertEqual(TP.qt_vwap({"vol_hand": 1000, "amount_wan": 100,
                                     "low": 0, "high": 0}), 10.0)

    def test_shares_unit_star_market_real_shape(self):
        """科创板：**真实观测形状**（688008 当日，2026-10-16）—— f[6] 是「股」。

        vol_hand=35,886,794（其实是**股**）/ amount_wan=607,308 / low=165.66 / high=173.38
        ⇒ 真值 169.229（akshare 侧同值 169.23）。若按"手"算会得 1.692 —— **偏小 100 倍**。
        """
        q = {"vol_hand": 35886794, "amount_wan": 607308.0, "low": 165.66, "high": 173.38}
        self.assertEqual(TP.qt_vwap(q), 169.23)

    def test_star_market_second_sample(self):
        """688111 当日形状：3,961,583（股）/ 91,231 万元 / [226.26, 233.55] ⇒ 230.289。"""
        q = {"vol_hand": 3961583, "amount_wan": 91231.0, "low": 226.26, "high": 233.55}
        self.assertEqual(TP.qt_vwap(q), 230.29)

    def test_interval_decides_when_both_plausible_shapes(self):
        """判据是**区间**不是前缀：造两组数，一组只有"手"口径落区间、一组只有"股"口径落。"""
        # vh=1000 手 → 1e5 股；100 万元 ⇒ 手口径 10.0、股口径 1000.0。区间 [9.5,10.5] 只容得下"手"
        self.assertEqual(TP.qt_vwap({"vol_hand": 1000, "amount_wan": 100,
                                     "low": 9.5, "high": 10.5}), 10.0)
        # 10 万元 ⇒ 手口径 1.0、股口径 100.0。区间 [95,105] 只容得下"股"
        self.assertEqual(TP.qt_vwap({"vol_hand": 1000, "amount_wan": 10,
                                     "low": 95.0, "high": 105.0}), 100.0)

    def test_no_interval_falls_back_to_lots(self):
        """low/high 不可用 ⇒ 退回「手」口径（多数板块），不抛。"""
        self.assertEqual(TP.qt_vwap({"vol_hand": 1000, "amount_wan": 100,
                                     "low": 0, "high": 0}), 10.0)

    def test_both_out_of_interval_returns_none(self):
        """两个候选都越界 ⇒ **宁缺勿假**，返回 None（调用方据此不写缓存）。"""
        self.assertIsNone(TP.qt_vwap({"vol_hand": 1000, "amount_wan": 100,
                                      "low": 5.0, "high": 6.0}))

    def test_zero_or_negative_volume_amount(self):
        """停牌/无成交：旧 akshare 实现用 `mask=(vol>0)&(amt>0)` 排除 ⇒ 这里同语义。"""
        for q in ({"vol_hand": 0, "amount_wan": 100, "low": 1, "high": 2},
                  {"vol_hand": 100, "amount_wan": 0, "low": 1, "high": 2},
                  {"vol_hand": -5, "amount_wan": 100, "low": 1, "high": 2},
                  {"vol_hand": 100, "amount_wan": -1, "low": 1, "high": 2}):
            self.assertIsNone(TP.qt_vwap(q), q)

    def test_missing_fields_never_raises(self):
        for q in ({}, {"low": 0, "high": 0}, {"vol_hand": None, "amount_wan": None},
                  {"vol_hand": "x", "amount_wan": "y"}, {"vol_hand": 1}):
            self.assertIsNone(TP.qt_vwap(q), q)


class TestMainUsesTencentNotAkshare(unittest.TestCase):
    """钉住换源本身 —— 退回 akshare 就会把主循环重新拖回 15 分钟。"""

    _SRC = (_ROOT / "main.py").read_text(encoding="utf-8")

    def _fn_src(self, code_only: bool = False):
        a = self._SRC.index("def _refresh_spot_vwap_cache")
        b = self._SRC.index("\n_IR_GATE_ADVICE_CN", a)
        src = self._SRC[a:b]
        if code_only:
            # ⚠️ 必须剥掉 docstring 与 `#` 注释再判 —— 判据看**代码**，不看说明文字。
            #    首版没剥，被 docstring 里"为什么从 akshare 换走"的说明判成了失败。
            src = re.sub(r'"""[\s\S]*?"""', " ", src)
            src = re.sub(r"(?m)#.*$", " ", src)
        return src

    def test_no_akshare_in_the_refresh_path(self):
        self.assertNotIn("akshare", self._fn_src(code_only=True),
                         "VWAP 刷新路径又用回 akshare 了（109s/次 × 逐只持仓 = 主循环 15 分钟）")
        self.assertNotIn("import ak", self._fn_src(code_only=True))

    def test_uses_tencent_and_the_unit_helper(self):
        src = self._fn_src()
        self.assertIn("snapshot_auction_many", src)
        self.assertIn("qt_vwap", src, "单位口径必须走 qt_vwap，别在 main.py 里自己算")

    def test_keeps_old_cache_when_fetch_fails(self):
        """整批取不到 ⇒ 保留旧缓存（与旧实现 `spot is None/empty → return` 同语义）。"""
        self.assertIn("if not snaps", self._fn_src())

    def test_throttle_still_60s(self):
        self.assertIn("_LAST_SPOT_VWAP_REFRESH < 60", self._fn_src())


if __name__ == "__main__":
    unittest.main(verbosity=2)
