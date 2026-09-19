# -*- coding: utf-8 -*-
"""背离新鲜度口径 离线单测（2026-09-19）。

## 为什么要有这个测试

修正前 `detect_minute_divergence_detail` 取 `events[-1]` 且**不检查事件年龄**，
把数周前的背离当成当前信号展示（实测 300153 的 30min 底背离距今 24 天/189 根）。
本测试把「过期即不返回」这条口径钉死，防止回退。

## 覆盖

- `bars_ago` 计算正确（= 最后一根 bar 索引 − 事件索引）
- `_latest_fresh` 的边界：等于阈值保留、超阈值剔除
- `detect_daily_divergence` 的 `date`→`time` 兼容与窗口过滤
- `detect_minute_divergence_detail` 端到端走新鲜度（monkeypatch 数据层，**不联网**）
- `consec` 字段形态不回归

## 怎么造出确定的背离

`_gen(tail)` 用锯齿价格构造：抬升 → 陡拉到峰A(120) → 回落 → **缓推**到峰B(124) →
尾部下跌。峰B 价更高但动能更弱 ⇒ 确定性地产生**顶背离**，且
`bars_ago = tail - 1`（`_local_extrema` 的 n_bars=3 决定尾部至少留 3 根）。

运行：python tests/stage0_2/test_divergence_freshness.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from analysis import divergence as dv  # noqa: E402


def _frame(c, lo=None, hi=None) -> pd.DataFrame:
    c = np.asarray(c, float)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=len(c), freq="D").astype(str),
        "open": c,
        "high": c * 1.001 if hi is None else np.asarray(hi, float),
        "low": c * 0.999 if lo is None else np.asarray(lo, float),
        "close": c,
    })


def _gen(tail: int) -> pd.DataFrame:
    """锯齿价格 → 确定性顶背离，bars_ago = tail - 1。tail>=4 才有事件。"""
    pts = []
    pts += list(np.linspace(100, 108, 15))[1:]
    pts += list(np.linspace(108, 120, 8))[1:]      # 陡拉到峰A
    pts += list(np.linspace(120, 110, 10))[1:]     # 回落
    pts += list(np.linspace(110, 124, 14))[1:]     # 缓推到峰B（价更高、动能更弱）
    pts += list(np.linspace(124, 112, tail))[1:]   # 尾部下跌
    return _frame(pts)


def _gen_top(peak_b: float, tail: int = 5) -> pd.DataFrame:
    """同 _gen，但峰B 价位可控 —— 用于测"创新高幅度门槛"。

    峰A 固定 120；峰B=120.2 只高出 0.17% ⇒ 应被 0.3% 门槛挡掉。"""
    pts = []
    pts += list(np.linspace(100, 108, 15))[1:]
    pts += list(np.linspace(108, 120, 8))[1:]
    pts += list(np.linspace(120, 110, 10))[1:]
    pts += list(np.linspace(110, peak_b, 14))[1:]
    pts += list(np.linspace(peak_b, 112, tail))[1:]
    return _frame(pts)


def _gen_tiny_bounce() -> pd.DataFrame:
    """两个谷相隔 8 根、中间只反弹 0.05% —— 属于**同一个底**，不是两个低点。

    复刻 owner 抽查的 300475(香农芯创)：谷 160.85(09-15) 与 谷 159.20(09-16) 仅隔 5 根、
    中间只反弹 2.26%，被误判底背离；而行情软件把 09-14~09-16 视为同一个底（158.34）。
    注意 bar 要够宽（这里 ±0.3% ⇒ 中位振幅 0.6%、门槛 1.5%）才能体现"摆动不足"。"""
    pts = []
    pts += list(np.linspace(104, 100, 25))[1:]
    pts += list(np.linspace(100, 98.9, 5))[1:]      # 谷1
    pts += list(np.linspace(98.9, 98.95, 7))[1:]    # 微弱反弹（6 根 > merge 窗口）
    pts += list(np.linspace(98.95, 98.5, 3))[1:]    # 谷2
    pts += list(np.linspace(98.5, 102, 12))[1:]
    c = np.array(pts)
    return _frame(c, lo=c * 0.997, hi=c * 1.003)


def _gen_top_mid_swing(dip_low: float = 99.3) -> pd.DataFrame:
    """顶背离，两峰之间的回撤深度**可控** —— 用于验证顶/底用了不同摆动门槛。

    dip_low=99.3 时：回撤落在 2.5x 与 4.0x 门槛之间 ⇒
    旧口径（顶也 2.5x）保留，新口径（顶 4.0x）剔除。"""
    pts = []
    pts += list(np.linspace(90, 100, 25))[1:]      # 涨到峰1
    pts += list(np.linspace(100, dip_low, 8))[1:]  # 单一下探
    pts += list(np.linspace(dip_low, 101, 12))[1:]  # 涨到峰2（更高）
    pts += list(np.linspace(101, 97, 6))[1:]
    c = np.array(pts)
    return _frame(c, lo=c * 0.997, hi=c * 1.003)


def _gen_wick() -> pd.DataFrame:
    """稳步上行中两根"长下影" → 后一根 low 更低但 DIF 更高（双正）。

    复刻真实误报机制：DIF 由**收盘价**算，而谷由 **low** 找 ⇒ 下影线可以在
    上升趋势里造出"更低的低点 + 更高的 DIF"。这是 002202 那类
    「DIF 深负区小反弹被标成顶背离」的镜像（此处是正区被标成底背离）。"""
    n = 60
    c = np.linspace(100, 130, n)
    lo = c * 0.998
    lo[32] = c[32] * 0.94          # 长下影 1
    lo[46] = lo[32] - 1.5          # 长下影 2（更低）
    return _frame(c, lo=lo, hi=c * 1.002)


class TestFreshness(unittest.TestCase):

    def test_01_bars_ago_arithmetic(self):
        """bars_ago = 最后一根索引 − 事件索引；且尾部越短事件越新。"""
        ages = {}
        for tail in (4, 5, 8):
            df = _gen(tail)
            ev = dv.detect_divergence_events(df)
            tops = [e for e in ev if e["type"] == "顶"]
            self.assertTrue(tops, f"tail={tail} 应产生顶背离（生成器失效则本测试作废）")
            e = tops[-1]
            ages[tail] = e["bars_ago"]
            self.assertEqual(e["bars_ago"], len(df) - 1 - e["index"])
        self.assertEqual(ages[4], 3)
        self.assertLess(ages[4], ages[8], "尾部越短，事件应越新")

    def test_02_latest_fresh_boundary(self):
        """_latest_fresh：bars_ago == 阈值保留，> 阈值剔除。"""
        ev = dv.detect_divergence_events(_gen(8))       # bars_ago = 7
        age = ev[-1]["bars_ago"]
        self.assertEqual(age, 7)
        self.assertIsNone(dv._latest_fresh(ev, age - 1), "超阈值应剔除")
        self.assertEqual(dv._latest_fresh(ev, age)["bars_ago"], age, "等于阈值应保留")
        self.assertEqual(dv._latest_fresh([], 100), None, "空列表应返回 None")

    def test_03_daily_window_and_date_compat(self):
        """detect_daily_divergence：接受 date 列；过期返回 {}，窗口内返回事件。"""
        df = _gen(5)                                     # bars_ago = 4
        df_date = df.rename(columns={"time": "date"})    # provider 真实列名
        self.assertEqual(dv.detect_daily_divergence(df_date, max_age_bars=3), {},
                         "4 根前的日线事件在 3 根窗口外 → 应不返回")
        r = dv.detect_daily_divergence(df_date, max_age_bars=4)
        self.assertEqual(r["type"], "顶背离")
        self.assertEqual(r["bars_ago"], 4)
        self.assertIn("time", r)
        self.assertIn("consec", r)

    def test_04_daily_rejects_bad_input(self):
        self.assertEqual(dv.detect_daily_divergence(None), {})
        self.assertEqual(dv.detect_daily_divergence(pd.DataFrame()), {})
        # 列不全（缺 high/low）→ 不抛异常，返回 {}
        self.assertEqual(dv.detect_daily_divergence(pd.DataFrame({"time": ["2026-01-01"], "close": [1.0]})), {})

    def test_05_minute_detail_applies_freshness(self):
        """端到端：过期事件不被 detail 返回（monkeypatch 数据层，不联网）。"""
        df = _gen(8)                                     # bars_ago = 7
        orig = dv.fetch_freq_kline
        dv.fetch_freq_kline = lambda code, freq="60min", days=None: df
        try:
            # 阈值 2 → 7 根前属过期，两个周期都应被剔除
            self.assertEqual(dv.detect_minute_divergence_detail("000000", {"30min": 2, "60min": 2}), {})
            # 阈值放宽 → 两个周期都应返回，且带 bars_ago
            got = dv.detect_minute_divergence_detail("000000", {"30min": 50, "60min": 50})
            self.assertEqual(set(got), {"m30", "m60"})
            for k, v in got.items():
                self.assertEqual(v["type"], "顶背离")
                self.assertEqual(v["bars_ago"], 7)
                self.assertIsInstance(v["consec"], bool)
        finally:
            dv.fetch_freq_kline = orig

    def test_07_price_excess_blocks_marginal_new_high(self):
        """幅度门槛：只高 0.17% 的"新高"不算新高（owner 报的 002202 误报根因）。"""
        marginal = _gen_top(120.2)          # 高出 0.17%
        self.assertEqual(dv.detect_divergence_events(marginal), [],
                         "0.17% 的微新高应被 0.3% 门槛挡掉")
        self.assertEqual(len(dv.detect_divergence_events(marginal, price_excess=0.0)), 1,
                         "关掉门槛后同一序列应能检出 —— 证明上面的空结果是门槛造成的")
        real = _gen_top(120.5)              # 高出 0.42%
        self.assertEqual(len(dv.detect_divergence_events(real)), 1,
                         "0.42% 的新高超过门槛，应保留")

    def test_08_dif_zone_requires_correct_side(self):
        """价区门槛：顶背离须 DIF>0、底背离须 DIF<0（对齐同花顺/通达信口径）。"""
        df = _gen_wick()
        off = dv.detect_divergence_events(df, price_excess=0.0, require_dif_zone=False)
        wrong = [e for e in off
                 if (e["type"] == "顶" and e["dif"] <= 0) or (e["type"] == "底" and e["dif"] >= 0)]
        self.assertTrue(wrong, "样本应能产出'价区不符'的事件，否则本测试是空转的")
        self.assertEqual(dv.detect_divergence_events(df), [],
                         "价区不符的事件在默认口径下必须被剔除")

        # 不变式：默认口径下，所有事件的 DIF 都在正确一侧，且数量不多于关闭门槛时
        for frame in (_gen_wick(), _gen(5), _gen_top(124.0)):
            on = dv.detect_divergence_events(frame)
            off2 = dv.detect_divergence_events(frame, require_dif_zone=False)
            self.assertLessEqual(len(on), len(off2))
            for e in on:
                if e["type"] == "底":
                    self.assertLess(e["dif"], 0, "底背离的 DIF 应在零轴下方")
                else:
                    self.assertGreater(e["dif"], 0, "顶背离的 DIF 应在零轴上方")

    def test_09_swing_gate_rejects_same_bottom(self):
        """摆动门槛：两个谷之间若没有一次真实反弹，它们属于同一个底，不算两个低点。

        owner 抽查 300475 的根因（见 _gen_tiny_bounce docstring）。"""
        df = _gen_tiny_bounce()
        off = dv.detect_divergence_events(df, price_excess=0.0,
                                          require_dif_zone=False, swing_mult=0.0)
        self.assertEqual(len(off), 1, "关掉摆动门槛时该序列应判出 1 个底背离（否则测试空转）")
        self.assertEqual(dv.detect_divergence_events(df, price_excess=0.0,
                                                     require_dif_zone=False), [],
                         "默认摆动门槛下，同一个底里的两个小谷不应构成底背离")

    def test_11_top_uses_stricter_swing_gate_than_bottom(self):
        """顶背离用更严的摆动门槛（SWING_MULT_TOP=4.0），底背离维持 2.5。

        依据：974 只 × 540 天扩池扫描显示顶背离提升在整条阈值曲线上都显著，
        而 60min 底背离 train +10pp → test ≈0（过拟合），故只收紧顶背离。"""
        df = _gen_top_mid_swing(99.3)
        kw = dict(price_excess=0.0, require_dif_zone=False)
        self.assertEqual(len(dv.detect_divergence_events(
            df, swing_mult=0.0, swing_mult_top=0.0, **kw)), 1,
            "夹具本身应能判出 1 个顶背离（否则测试空转）")
        self.assertEqual(len(dv.detect_divergence_events(
            df, swing_mult_top=2.5, **kw)), 1,
            "旧口径（顶也用 2.5x）应保留该事件")
        self.assertEqual(len(dv.detect_divergence_events(df, **kw)), 0,
            "新口径（顶用 4.0x）应剔除该不足摆动的顶背离")
        # 底背离不受影响
        self.assertEqual(dv.SWING_MULT, 2.5)
        self.assertGreater(dv.SWING_MULT_TOP, dv.SWING_MULT)

    def test_10_swing_mult_scales_with_volatility(self):
        """门槛必须是**相对**量：同样的价格形态，bar 越宽越容易被判"非独立摆动"。

        这是为了不把低波票一票否决（固定 3% 曾让 515180/600900/601318 等 6 只信号归零）。"""
        x = np.arange(60)
        base = 100 + 2 * np.sin(x / 6.0)
        narrow = _frame(base, lo=base * 0.999, hi=base * 1.001)
        wide = _frame(base, lo=base * 0.985, hi=base * 1.015)
        med_n = float(np.median((narrow["high"] - narrow["low"]) / narrow["close"]))
        med_w = float(np.median((wide["high"] - wide["low"]) / wide["close"]))
        self.assertLess(med_n, med_w)
        self.assertLessEqual(len(dv.detect_divergence_events(wide)),
                             len(dv.detect_divergence_events(narrow)),
                             "同样的形态，宽 bar（高波）下判定应不更宽松")
        self.assertGreater(dv.SWING_MULT, 0)

    def test_06_default_thresholds_are_one_week(self):
        """默认阈值 = 30min/60min 约 1 周；日线提醒窗口单独（4 根）。"""
        self.assertEqual(dv.MAX_AGE_BARS, {"30min": 32, "60min": 20})
        self.assertEqual(dv.DAILY_ALERT_MAX_AGE_BARS, 4)
        self.assertGreater(dv.DAILY_DISPLAY_MAX_AGE_BARS, dv.DAILY_ALERT_MAX_AGE_BARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
