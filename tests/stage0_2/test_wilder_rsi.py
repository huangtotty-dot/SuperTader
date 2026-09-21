# -*- coding: utf-8 -*-
"""RSI 口径单测 —— Wilder 平滑（2026-09-21）。

## 为什么要有这个测试

owner 报「指数5分钟超卖预警 与同花顺不吻合」：预警称科创50 5分钟RSI=4.6（超卖），
同花顺同刻显示 RSI6=38.14。排查出两个叠加根因，本测试钉住其中两个可单测的点：

1. **公式**：原实现用 `rolling(N).mean()`（简单均值）。简单均值下**一根大阴线进出
   N 根窗口**就把 RSI 打到个位数——实测 10:35 那根：简单均值 6.24 / 同花顺同刻 26 左右。
   标准 RSI（通达信/同花顺 `SMA(X,N,1)`）是 Wilder 递归平滑。
2. **坐标**：原 `rsi` 列用的是**单日**序列，而它是递推指标 → 种子主导结果
   （单日 RSI6 得 3.6，多日 796 根 Wilder 得 36.7）。这一条靠数据源侧修
   （main.py 指数预警改用掘金多日 300s），不在本文件的单测范围内，但下面的
   「大阴线不塌缩」用例正是它暴露出来的症状。

## 参照实现

`_ref_wilder` 是教科书写法（前 N 根简单均值播种后递推，TA-Lib 口径）。
两种写法只在**预热期**不同，长序列尾部应逐位相等——`test_matches_classical_wilder_tail`
把这条钉死，防止有人把平滑改回去。

运行：python tests/stage0_2/test_wilder_rsi.py
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

from analysis.indicators import add_5min_indicators, wilder_rsi  # noqa: E402


def _ref_wilder(close: pd.Series, period: int) -> pd.Series:
    """教科书 Wilder：前 period 根简单均值播种，之后递归。"""
    d = close.diff().values
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = np.full(len(d), np.nan)
    ad = np.full(len(d), np.nan)
    if len(d) > period:
        au[period] = up[1:period + 1].mean()
        ad[period] = dn[1:period + 1].mean()
        for i in range(period + 1, len(d)):
            au[i] = (au[i - 1] * (period - 1) + up[i]) / period
            ad[i] = (ad[i - 1] * (period - 1) + dn[i]) / period
    return pd.Series([
        100 - 100 / (1 + au[i] / ad[i]) if (i >= period and ad[i]) else np.nan
        for i in range(len(d))
    ])


def _sma_rsi(close: pd.Series, period: int) -> pd.Series:
    """旧实现（简单均值）—— 仅供对照，确认它确实会塌缩。"""
    d = close.diff()
    g = d.clip(lower=0).rolling(period, min_periods=1).mean()
    l = (-d.clip(upper=0)).rolling(period, min_periods=1).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


class TestWilderRsi(unittest.TestCase):
    def test_matches_classical_wilder_tail(self):
        """长序列尾部须与教科书 Wilder 逐位相等（差异只允许出现在预热期）。"""
        np.random.seed(7)
        c = pd.Series(100 + np.random.randn(400).cumsum())
        for n in (6, 14):
            a, b = wilder_rsi(c, n), _ref_wilder(c, n)
            self.assertAlmostEqual(float((a - b).abs().iloc[-30:].max()), 0.0, places=6,
                                   msg=f"RSI{n} 尾部与经典 Wilder 不一致——平滑被改回去了？")

    def test_single_big_bar_does_not_collapse(self):
        """关键回归：一根大阴线进出 6 根窗口时，Wilder 不得塌到个位数（旧实现会）。

        数据复刻实测情形（科创50 5分钟：一根 -8.46 的下跌）。
        """
        c = pd.Series([1665.0, 1665.7, 1657.2, 1657.4, 1656.5, 1654.5, 1653.2])
        old = float(_sma_rsi(c, 6).iloc[-1])
        new = float(wilder_rsi(c, 6).iloc[-1])

        self.assertLess(old, 10, "本用例前提：旧口径应塌到个位数")
        self.assertGreater(new, 15, f"Wilder 不应塌缩，实际 {new:.2f}（旧口径 {old:.2f}）")
        self.assertGreater(new, old, "Wilder 必须比简单均值更抗单根极值")

    def test_flat_window_is_neutral_50(self):
        """0/0 钉平窗语义保留：一段完全横盘 → 50 中性（而非 NaN）。"""
        c = pd.Series([100.0] * 10)
        self.assertAlmostEqual(float(wilder_rsi(c, 6).iloc[-1]), 50.0, places=9)

    def test_leading_warmup_is_nan(self):
        """预热期保持 NaN（首个 diff 为 NaN → 递推无有效种子）。"""
        c = pd.Series([100.0, 101.0, 102.0, 101.5, 103.0])
        self.assertTrue(pd.isna(wilder_rsi(c, 6).iloc[0]))

    def test_pure_uptrend_stays_nan(self):
        """纯上涨窗（loss==0 且 gain>0）**刻意保持 NaN**，不填 100。

        这是 V1.1.2 的既有语义（"与现网一致"），不是新引入的：消费端（预警/共振闸门）
        遇到 NaN 会跳过/判不过，属保守安全侧。本用例把这条语义钉住，
        避免有人"顺手"改成 100 而改变闸门行为。
        """
        c = pd.Series([1.0 * i for i in range(1, 21)])
        self.assertTrue(pd.isna(wilder_rsi(c, 6).iloc[-1]), "纯上涨窗应保持 NaN（V1.1.2 语义）")

    def test_indicators_columns_use_wilder(self):
        """接线回归：add_5min_indicators 产出的三列必须等于 wilder_rsi 的结果。

        防止有人只改 wilder_rsi 而把列公式改回简单均值（或反之）。
        """
        np.random.seed(11)
        df = pd.DataFrame({
            "time": pd.date_range("2026-09-21 09:35", periods=120, freq="5min"),
            "open": 100 + np.random.randn(120).cumsum(),
            "high": 101 + np.random.randn(120).cumsum(),
            "low": 99 + np.random.randn(120).cumsum(),
            "close": 100 + np.random.randn(120).cumsum(),
            "volume": np.full(120, 1000.0),
        })
        out = add_5min_indicators(df)
        # 逐列与 helper 对齐（同输入同周期须完全一致）
        from config import PARAMS
        close = out["close"]
        exp6 = wilder_rsi(close, int(PARAMS.get("rsi_period_5m_swing", 6)))
        exp14 = wilder_rsi(close, int(PARAMS.get("rsi_period_5m", 14)))
        self.assertAlmostEqual(float(out["rsi_5m_p6"].iloc[-1]), float(exp6.iloc[-1]), places=9)
        self.assertAlmostEqual(float(out["rsi_5m"].iloc[-1]), float(exp14.iloc[-1]), places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
