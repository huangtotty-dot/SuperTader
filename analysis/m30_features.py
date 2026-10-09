# -*- coding: utf-8 -*-
"""analysis/m30_features.py — 30分钟线「顶部特征」T1–T4（2026-10-09，**纯计算、无 I/O**）。

来源：`reports/taskB_30min顶部减仓_底部恢复_特征规则手册.md` §1（T1–T4 顶部信号）。

⚠️ **全部未回测验证**（参考·未验证）。只作持仓体检面板展示，用于提示「30min 顶部共性」，
   **不得**表述为买卖信号。仓储既有证据：背离当入场信号在 4.7 年回测中被否
   （`t_io/validation/m60_div_entry/结论_2026-09-12.md`）；30min 顶背离命中≈基线
   （`t_io/validation/w35_divergence/divergence_验证报告.md`）。有效性由
   `t_io/validation/m30_top/` 并行验证。

输入：30min bars `df`（列 `time/open/high/low/close/volume`，时间升序）。
"""
from __future__ import annotations

import bisect

import numpy as np
import pandas as pd

from analysis.divergence import _macd_dif, _local_extrema

MIN_BARS = 60                                   # MACD(26) + 峰谷 + SMA60 预热

FEATURE_LABELS = {"T1": "T1顶背离", "T2": "T2量价背离", "T3": "T3顶分型", "T4": "T4均线压制"}

# T1/T2 摆动门槛（手册 §1）：两个摆动高点间距须在此区间内
MIN_GAP = 4
MAX_GAP = 48


def add_m30_indicators(df, vol_ma_n: int = 8, ma_n=(20, 60)) -> pd.DataFrame:
    """在 30min bars 上追加 T1–T4 需要的列（MACD DIF、SMA、量能均线、影线/实体/振幅）。
    返回**新** df，不改动入参。列不足时返回空 DataFrame。"""
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    need = {"open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    d = df.copy()
    closes = d["close"].astype(float)
    op = d["open"].astype(float)
    hi = d["high"].astype(float)
    lo = d["low"].astype(float)
    d["dif"] = _macd_dif(closes.values)
    for n in ma_n:
        d[f"sma{n}"] = closes.rolling(n).mean()
    d[f"vol_ma{vol_ma_n}"] = d["volume"].astype(float).rolling(vol_ma_n).mean()
    d["body"] = closes - op
    d["amp"] = hi - lo
    d["upper_shadow"] = hi - np.maximum(closes, op)
    return d


def _swing_highs(ind, swing_lr: int = 3):
    """摆动高点索引（复用 divergence._local_extrema，已做≤4根相邻合并）。"""
    return _local_extrema(ind["high"].astype(float).values,
                          ind["low"].astype(float).values, n_bars=swing_lr)[0]


def _arrays(ind, vol_ma_n: int = 8) -> dict:
    """把 30min 指标帧一次性转 numpy 数组（逐根评估共用，避免 O(n²) 重复 `.values`）。"""
    return {
        "close": ind["close"].astype(float).values,
        "high": ind["high"].astype(float).values,
        "low": ind["low"].astype(float).values,
        "vol": ind["volume"].astype(float).values,
        "dif": ind["dif"].values,
        "up_sh": ind["upper_shadow"].values,
        "body": ind["body"].values,
        "amp": ind["amp"].values,
        "vma": ind[f"vol_ma{vol_ma_n}"].values,
        "sma20": ind["sma20"].values if "sma20" in ind.columns else None,
        "sma60": ind["sma60"].values if "sma60" in ind.columns else None,
    }


def _features_at(A, i, peaks, *, price_excess: float = 0.001, shrink_ratio: float = 0.7,
                 max_age_bars: int = 24, ma_touch: float = 0.003,
                 high_zone_n: int = 16) -> dict:
    """在数组视图 `A`（见 `_arrays`）的第 i 根上评估 T1–T4。单根评估 = 唯一真源（detect/scan 共用）。"""
    closes = A["close"]
    highs = A["high"]
    lows = A["low"]
    vols = A["vol"]
    dif = A["dif"]
    up_sh = A["up_sh"]
    body = A["body"]
    amp = A["amp"]
    vma = A["vma"]

    t1 = t2 = t3 = t3_strong = t4 = False
    p1 = p2 = gap = bars_ago = None

    # ── T1/T2：最近两个摆动高点 P1<P2（间距 4–48、价创新高）为共同前提 ──
    pos = bisect.bisect_right(peaks, i)   # peaks 升序 ⇒ O(log) 取「≤ i 的最后两个」
    if pos >= 2:
        a1, a2 = peaks[pos - 2], peaks[pos - 1]   # a1 较早、a2 较新
        g = a2 - a1
        if (MIN_GAP <= g <= MAX_GAP and i - a2 <= max_age_bars
                and closes[a2] > closes[a1] * (1 + price_excess)):
            p1, p2, gap, bars_ago = int(a1), int(a2), int(g), int(i - a2)
            # T1：价创新高 但 DIF 未创新高（顶背离）。⚠️ 手册另要求 dif[P2]<dif[P2-1]（DIF 已回落），
            # 但摆动高点由 _local_extrema 滞后确认（n_bars=3），实测 P2 处 DIF 常仍在**上行**
            # ⇒ 该条会把绝大多数真实顶背离挡掉。改为只用经典定义（与 divergence.detect_divergence_df 同）。
            if dif[a2] < dif[a1]:
                t1 = True
            # T2：价创新高 但量萎缩（且低于自身量均线）
            if (vols[a2] < vols[a1] * shrink_ratio
                    and not np.isnan(vma[a2]) and vols[a2] < vma[a2]):
                t2 = True

    # ── T3：顶分型（K1,K2,K3 三点，中心 K2=i-1）+ 长上影分级 ──
    if i >= 2:
        k1, k2, k3 = i - 2, i - 1, i
        if (highs[k2] > highs[k1] and highs[k2] > highs[k3]
                and lows[k2] > lows[k1] and lows[k2] > lows[k3]):
            t3 = True
            long_sh = (amp[k2] > 0 and up_sh[k2] >= 2 * abs(body[k2]) and up_sh[k2] >= 0.5 * amp[k2])
            if long_sh and closes[k3] < (lows[k1] + highs[k1]) / 2:
                t3_strong = True
    # 独立长上影（贴近近 high_zone_n 根高点）：末根放量冲高回落
    if not t3 and i >= high_zone_n:
        zone_hi = closes[i - high_zone_n + 1:i + 1].max()
        if (amp[i] > 0 and up_sh[i] >= 2 * abs(body[i]) and up_sh[i] >= 0.5 * amp[i]
                and closes[i] >= 0.97 * zone_hi):
            t3 = True

    # ── T4：均线压制（close<SMA20<SMA60，SMA20 下行，且反弹触及 SMA20±0.3%）──
    sma20, sma60 = A["sma20"], A["sma60"]
    if (sma20 is not None and sma60 is not None and i >= 1
            and not np.isnan(sma20[i]) and not np.isnan(sma60[i]) and not np.isnan(sma20[i - 1])
            and closes[i] < sma20[i] < sma60[i] and sma20[i] < sma20[i - 1]):
        _touch_close = abs(closes[i] - sma20[i]) / sma20[i] <= ma_touch
        _touch_hi = any(
            (not np.isnan(sma20[j])) and abs(highs[j] - sma20[j]) / sma20[j] <= ma_touch
            for j in range(max(0, i - 3), i + 1))
        if _touch_close or _touch_hi:
            t4 = True

    fired = [k for k, v in (("T1", t1), ("T2", t2), ("T3", t3), ("T4", t4)) if v]
    return {"t1": t1, "t2": t2, "t3": t3, "t3_strong": t3_strong, "t4": t4,
            "count": int(t1) + int(t2) + int(t3) + int(t4),
            "fired": [FEATURE_LABELS[k] for k in fired],
            "t1_p1": p1, "t1_p2": p2, "t1_swing_gap": gap, "t1_bars_ago": bars_ago}


def _blank(n_bars: int = 0, ok: bool = False, bar_time=None) -> dict:
    return {"ok": ok, "n_bars": n_bars, "bar_time": bar_time,
            "t1": False, "t2": False, "t3": False, "t3_strong": False, "t4": False,
            "count": 0, "fired": [], "t1_p1": None, "t1_p2": None,
            "t1_swing_gap": None, "t1_bars_ago": None}


def detect_top_features(df, **kw) -> dict:
    """末根（最新）bar 的顶部特征。返回含 `ok/n_bars/bar_time` 的 dict（见 `_features_at`）。"""
    ind = add_m30_indicators(df)
    if ind.empty or len(ind) < MIN_BARS:
        return _blank(0 if ind is None or ind.empty else len(ind))
    peaks = _swing_highs(ind)
    feats = _features_at(_arrays(ind), len(ind) - 1, peaks, **kw)
    feats["ok"] = True
    feats["n_bars"] = len(ind)
    feats["bar_time"] = str(ind["time"].iloc[-1]) if "time" in ind.columns else None
    return feats


def scan_top_features(df, **kw) -> list:
    """逐根评估（含 `index`/`bar_time`），供离线验证脚本用（与面板同一套阈值，口径不漂移）。"""
    ind = add_m30_indicators(df)
    if ind.empty:
        return []
    peaks = _swing_highs(ind)
    A = _arrays(ind)                                   # 只转一次（O(n)），逐根评估 O(n) 总量
    times = ind["time"].astype(str).values if "time" in ind.columns else None
    out = []
    for i in range(len(ind)):
        if i + 1 < MIN_BARS:
            continue
        f = _features_at(A, i, peaks, **kw)
        f["index"] = i
        f["bar_time"] = (times[i] if times is not None else None)
        out.append(f)
    return out


def risk_from_features(feats: dict):
    """顶部特征 → (风险等级, 风险提醒文案)。展示口径，**参考·未验证**。"""
    feats = feats or {}
    if not feats.get("ok"):
        return "低", "（30min数据不足）参考·未验证"
    fired = "、".join(feats.get("fired") or []) or "—"
    n = int(feats.get("count") or 0)
    if n >= 3 or (n >= 2 and feats.get("t1")):
        return "高", f"🚨 30min顶部特征×{n}（{fired}）：分档减仓 / 避开追高 · 参考·未验证"
    if n >= 1:
        return "中", f"⚠ 30min顶部特征×{n}（{fired}）：反弹减仓观察 · 参考·未验证"
    return "低", "✓ 无30min顶部特征 · 参考·未验证"


VERDICT_LABELS = {"high": "🔴 减仓/避高", "watch": "🟠 盯紧", "bull": "🟢 偏好",
                  "none": "⚪ 中性", "na": "⚪ 数据不足"}


def verdict_from_features(feats, trend=None, div_type=None, div_bars_ago=None,
                          fresh_bars: int = 16):
    """30min 综合判定 → `(level, label, reason)`。面板「30min 判定」列的唯一口径。

    口径依据 `t_io/validation/m30_top/报告_m30_top.md`（981只×540日，升沿事件 vs 全样本基线）：
      · **T1 顶背离 / T2 量价背离**：前瞻**强**（H=4 跌占比 +29 / +32pp，z≈97 / 88）
        ⇒ 唯一触发「减仓/避高」的信号。
      · **T3 顶分型 / T4 均线压制**：**无区分度**（lift≈0 甚至反向，z<0）⇒ 只作"知情"弱信号，
        不单独报警（单个 → 中性；两者同现 → 盯紧）。
      · **新鲜底背离**且无顶信号 → 偏好。

    与 `risk_from_features`（旧"计数≥2"口径，仅回滚用）不同：**不按计数报警**——那会把 T3/T4
    噪声算进共振，反而稀释真信号（实测「共振≥2」lift 仅 +1.8pp）。"""
    f = feats or {}
    if not f.get("ok"):
        return "na", VERDICT_LABELS["na"], "30min 数据不足"
    t1, t2, t3, t4 = (f.get(k) for k in ("t1", "t2", "t3", "t4"))
    if t1 or t2:
        why = "、".join(x for x, v in (("T1顶背离", t1), ("T2量价背离", t2)) if v)
        return "high", VERDICT_LABELS["high"], f"{why}（已验证强特征）"
    if t3 and t4:
        return "watch", VERDICT_LABELS["watch"], "T3顶分型+T4均线压制（弱信号共振，未验出前瞻）"
    if div_type == "底背离" and div_bars_ago is not None and div_bars_ago <= fresh_bars:
        return "bull", VERDICT_LABELS["bull"], f"无顶信号；新鲜底背离（{int(div_bars_ago)}根前）"
    weak = "、".join(x for x, v in (("T3顶分型", t3), ("T4均线压制", t4)) if v)
    reason = f"仅{weak}（未验出前瞻，不报警）" if weak else "无 30min 顶部特征"
    return "none", VERDICT_LABELS["none"], reason


__all__ = ["add_m30_indicators", "detect_top_features", "scan_top_features",
           "risk_from_features", "verdict_from_features", "VERDICT_LABELS",
           "FEATURE_LABELS", "MIN_BARS"]
