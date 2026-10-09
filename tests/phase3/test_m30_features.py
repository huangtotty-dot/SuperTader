# -*- coding: utf-8 -*-
"""30 分钟线「顶部特征」T1–T4 离线单测（2026-10-09）。**全离线、纯计算**。

覆盖 `analysis/m30_features.py`：
  - `add_m30_indicators` 列齐（dif/sma20/sma60/vol_ma8/upper_shadow/body/amp）
  - T1（MACD 顶背离）/ T2（量价背离）/ T3（顶分型+长上影）/ T4（均线压制）各自确定性触发
  - `count` 与 `fired` 一致；数据不足 → ok=False/count=0
  - `detect_top_features(df)` 与 `scan_top_features(df)[-1]` 同口径（不漂移）
  - `risk_from_features` 映射（0/1/2 无 T1/2 含 T1/3）

运行：python tests/phase3/test_m30_features.py
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

from analysis import m30_features as mf  # noqa: E402


def _bars(closes, highs=None, lows=None, opens=None, volumes=None):
    """由收盘序列构造 30min bars（缺省影线 ±0.1%、量能 1000）。time 升序。"""
    c = np.asarray(closes, float)
    n = len(c)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-05 09:30", periods=n, freq="30min").astype(str),
        "open": c if opens is None else np.asarray(opens, float),
        "high": c * 1.001 if highs is None else np.asarray(highs, float),
        "low": c * 0.999 if lows is None else np.asarray(lows, float),
        "close": c,
        "volume": np.full(n, 1000.0) if volumes is None else np.asarray(volumes, float),
    })


def _saw_dual_peak(vol_at_p1=1000.0, vol_at_p2=1000.0):
    """锯齿：抬升→陡拉到峰A→回落→**缓推**到峰B(价更高,动能更弱)→尾部下跌。
    确定性产生 T1（DIF 顶背离）；两个摆动高点间距 ~13 根（在 4–48 内）。
    P1≈峰A、P2≈峰B；峰B 量可由 `vol_at_p2` 单独压低以触发 T2。"""
    pts = []
    pts += list(np.linspace(100, 108, 20))[1:]
    pts += list(np.linspace(108, 120, 10))[1:]     # 陡拉到峰A
    pts += list(np.linspace(120, 110, 12))[1:]     # 回落
    pts += list(np.linspace(110, 124, 16))[1:]     # 缓推到峰B
    pts += list(np.linspace(124, 114, 8))[1:]      # 尾部下跌（留 3 根确认峰B）
    c = np.array(pts)
    vol = np.full(len(c), 1000.0)
    # 峰A / 峰B 索引 = argmax 分段（用局部极值近似），单独调量
    hi = c
    p2 = int(np.argmax(hi[30:])) + 30              # 峰B（后半段最高）
    vol[p2] = vol_at_p2
    p1 = int(np.argmax(hi[18:30])) + 18            # 峰A
    vol[p1] = vol_at_p1
    return _bars(c, volumes=vol)


def _top_fractal():
    """末三根构成缠论顶分型（中间 high/low 均高于两侧），中间长上影、末根收得低。"""
    c = list(np.linspace(10.0, 10.4, 66))          # 平稳上行到 10.4
    o = list(c)
    hi = [x * 1.001 for x in c]
    lo = [x * 0.999 for x in c]
    # 倒数第 3/2/1 根：k1, k2, k3（k2 为分型中心 i-1）
    c[-3], c[-2], c[-1] = 10.30, 10.36, 10.10
    o[-3], o[-2], o[-1] = 10.29, 10.34, 10.32
    hi[-3], hi[-2], hi[-1] = 10.34, 10.56, 10.33     # k2 high 最高
    lo[-3], lo[-2], lo[-1] = 10.28, 10.31, 10.05     # k2 low 亦高于两侧
    return _bars(c, highs=hi, lows=lo, opens=o)


def _ma_suppressed():
    """极缓下行（d/bar≈0.003）：close < SMA20 < SMA60 且 SMA20 下行、贴近 SMA20 ⇒ T4。"""
    c = np.linspace(11.0, 11.0 - 0.003 * 69, 70)
    return _bars(c)


def _monotone_up():
    return _bars(np.linspace(10.0, 11.4, 70))


class TestM30Features(unittest.TestCase):

    def test_01_indicators_columns(self):
        ind = mf.add_m30_indicators(_monotone_up())
        for col in ("dif", "sma20", "sma60", "vol_ma8", "upper_shadow", "body", "amp"):
            self.assertIn(col, ind.columns, f"缺列 {col}")
        self.assertEqual(len(ind), 70)

    def test_02_t1_macd_top_divergence(self):
        f = mf.detect_top_features(_saw_dual_peak())
        self.assertTrue(f["ok"])
        self.assertTrue(f["t1"], f"应检出 T1 顶背离；fired={f['fired']}")

    def test_03_t2_volume_divergence(self):
        # 峰B 量缩到峰A 的 25% ⇒ T2；T1 也仍在
        f = mf.detect_top_features(_saw_dual_peak(vol_at_p1=1000.0, vol_at_p2=250.0))
        self.assertTrue(f["t2"], f"应检出 T2 量价背离；fired={f['fired']}")

    def test_04_t3_top_fractal(self):
        f = mf.detect_top_features(_top_fractal())
        self.assertTrue(f["t3"], f"应检出 T3 顶分型；fired={f['fired']}")

    def test_05_t4_ma_suppression(self):
        f = mf.detect_top_features(_ma_suppressed())
        self.assertTrue(f["t4"], f"应检出 T4 均线压制；fired={f['fired']}")

    def test_06_count_and_fired_consistent(self):
        f = mf.detect_top_features(_saw_dual_peak(vol_at_p2=250.0))
        self.assertEqual(f["count"], int(f["t1"]) + int(f["t2"]) + int(f["t3"]) + int(f["t4"]))
        self.assertEqual(len(f["fired"]), f["count"])

    def test_07_insufficient(self):
        f = mf.detect_top_features(_monotone_up().head(30))
        self.assertFalse(f["ok"])
        self.assertEqual(f["count"], 0)

    def test_08_no_features_on_monotone_up(self):
        f = mf.detect_top_features(_monotone_up())
        self.assertTrue(f["ok"])
        self.assertEqual(f["count"], 0, f"单调上行不应有顶部特征；fired={f['fired']}")

    def test_09_detect_equals_scan_last(self):
        df = _saw_dual_peak(vol_at_p2=250.0)
        det = mf.detect_top_features(df)
        sc = mf.scan_top_features(df)
        self.assertTrue(sc)
        last = {k: v for k, v in sc[-1].items() if k != "index"}
        for k in ("t1", "t2", "t3", "t4", "count", "fired"):
            self.assertEqual(det[k], last[k], f"detect 与 scan 在 {k} 上口径漂移")

    def test_10_risk_mapping(self):
        self.assertEqual(mf.risk_from_features(None)[0], "低")
        self.assertEqual(mf.risk_from_features({"ok": False})[0], "低")
        self.assertEqual(mf.risk_from_features({"ok": True, "count": 0, "fired": []})[0], "低")
        self.assertEqual(mf.risk_from_features({"ok": True, "count": 1, "fired": ["T4均线压制"]})[0], "中")
        # 2 项但无 T1 → 中；含 T1 → 高
        self.assertEqual(mf.risk_from_features({"ok": True, "count": 2, "t1": False, "fired": ["T3顶分型", "T4均线压制"]})[0], "中")
        self.assertEqual(mf.risk_from_features({"ok": True, "count": 2, "t1": True, "fired": ["T1顶背离", "T2量价背离"]})[0], "高")
        self.assertEqual(mf.risk_from_features({"ok": True, "count": 3, "fired": ["T1顶背离", "T2量价背离", "T4均线压制"]})[0], "高")


if __name__ == "__main__":
    unittest.main(verbosity=2)
