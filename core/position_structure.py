# -*- coding: utf-8 -*-
"""core/position_structure.py — 持仓结构层（30min/10min），供「建仓信号扫描」做仓位管理参考
====================================================================================
定位：给**主观交易**的阶段性持仓提供**结构参考**（止损位/阻力位/阶段/中期方向/补仓档），
      形式与建仓信号扫描现有输出一致（可直接落 trace、可显示为中文列）。

⚠️ 纪律（写死在模块里，勿在消费侧放宽）：
  · 本模块产出的是**结构参考**，**不是买卖信号**。
    实测结论：机械按「阶段2 加仓 / 阶段3 减仓」做仓位择时，相对"恒定同仓位"的
    纯择时贡献为 −1.4 ~ +2.7pp（不显著），而「破前低就清仓」是 −30 ~ −32pp（t=−20，强有害）。
    结构位的价值在于**给人为决策一个非任意的锚点**，不是"预测更准"。
  · 唯一**已验证**的动作是「止损后在**下档支撑**挂限价补仓」：距止损位 −2.3%/−3.0%/−4.3%
    三档相对同窗口随机时点 +0.5 ~ +1.1pp（t=+14~+24，136,756 次破位样本，分半窗 12 格全正）。
    但其绝对期望不变 ⇒ 改善的是**成本结构**，不是收益期望。
  · 全部计算**因果**：摆动点在其后 k 根才确认；所有均线/极值只用到当根及之前。
  · 本模块**无 IO、无网络**（纯函数），与研究目录 `t_io/validation/` 解耦（该目录不入库）。

研究出处（只读引用，不 import）：
  doc/research/2026-10-11_止损后补仓_支撑依据实验.md
  doc/research/2026-10-11_波段做T与仓位择时_多周期实验.md
  doc/research/2026-10-11_多周期阶段法_可行性实验.md
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---- 参数（单一真源；改动须同步研究口径）----
SWING_K = 2                 # 摆动点确认半窗（bar 两侧各 k 根）
RES_WIN_10 = 24             # 10min 短线阻力回看棒数（≈1 个交易日）
MA_FAST_10, MA_SLOW_10 = 20, 50      # 10min 短期/中期均线（书：MA20/MA50）
MA_MID_30 = 65              # 30min 中期均线（≈16 交易日，书注"覆盖 15~20 天"）
MA_FAST_30 = 5              # 30min 短均线（**纯展示项**：已实测无前瞻信息量，勿用于判定）
MA_MID_D = 20               # 日线中期成本线
REFILL_VALID_DAYS = 5       # 补仓窗口有效期（交易日）
FULL_DISCOUNT = 0.043       # −4.3%（前20日最低的历史均距）记满折价

# 三档下档支撑的历史成交率（977 只 × 361 日实测；用于告诉用户"这档大概几成能成交"）
TIER_FILL_RATE = {"T1 30min MA65": 34.6, "T2 日线 MA20": 27.5, "T3 前20日最低": 28.9}

# 纯距离 → 5 日内成交率（同源实测：限价挂在止损位下方 d% 时，5 个交易日内被触及的比例）
# 来源 doc/research/2026-10-11_止损后补仓_支撑依据实验.md §4（FIX 曲线）
_DIST_FILL = [(0.010, 88.5), (0.015, 81.1), (0.020, 74.0), (0.025, 67.1),
              (0.030, 60.6), (0.035, 54.7), (0.040, 49.0), (0.050, 39.6),
              (0.060, 31.7)]


def expected_fill_rate(dist_pct: float) -> float:
    """按"距止损位的百分比"估 5 日内成交概率(%)。超出 6% 按曲线斜率外推并设地板。"""
    d = abs(dist_pct) / 100.0
    xs = [x for x, _ in _DIST_FILL]
    ys = [y for _, y in _DIST_FILL]
    if d <= xs[0]:
        return round(ys[0], 1)
    if d >= xs[-1]:                      # 线性外推，地板 8%
        slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        return round(max(8.0, ys[-1] + slope * (d - xs[-1])), 1)
    return round(float(np.interp(d, xs, ys)), 1)

STAGE_LABEL = {1: "①蓄势", 2: "②抬升", 3: "③派发", 4: "④下跌"}
TREND_LABEL = {"up": "向上", "flat": "走平", "down": "向下"}


# --------------------------------------------------------------------------
# 基础：因果摆动点
# --------------------------------------------------------------------------
def _swing_points(high: np.ndarray, low: np.ndarray, k: int = SWING_K):
    """返回 (sh_idx, sh_val, sl_idx, sl_val)；索引 = **确认时点**（中心 + k），故全因果。"""
    n = len(high)
    sh_i, sh_v, sl_i, sl_v = [], [], [], []
    for c in range(k, n - k):
        conf = c + k
        if conf >= n:
            break
        if high[c] == max(high[c - k:c + k + 1]):
            if not sh_i or conf != sh_i[-1]:
                sh_i.append(conf); sh_v.append(float(high[c]))
        if low[c] == min(low[c - k:c + k + 1]):
            if not sl_i or conf != sl_i[-1]:
                sl_i.append(conf); sl_v.append(float(low[c]))
    return (np.asarray(sh_i, int), np.asarray(sh_v, float),
            np.asarray(sl_i, int), np.asarray(sl_v, float))


def _ff_last_prev(idx: np.ndarray, val: np.ndarray, n: int):
    """按确认时点前向填充「最近 / 再前一个」值（bar i 只看到 idx<=i 的事件）。"""
    nan = np.full(n, np.nan)
    if len(idx) == 0:
        return nan, nan
    j = np.searchsorted(idx, np.arange(n), side="right") - 1
    last = np.where(j >= 0, val[np.clip(j, 0, None)], np.nan)
    prev = np.where(j >= 1, val[np.clip(j - 1, 0, None)], np.nan)
    return last, prev


def _sma(a: np.ndarray, w: int) -> np.ndarray:
    n = len(a)
    out = np.full(n, np.nan)
    if n >= w:
        c = np.cumsum(np.r_[0.0, a])
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def resample_5min_to_10min(df5: pd.DataFrame) -> pd.DataFrame:
    """5min → 10min（按 (symbol, 日) 内两两配对，标签取配对第二根的时间）。
    与生产一致：**剔除 09:30 竞价棒**（量极小、价=开盘价，会污染摆动点）。

    时间列：接受 `dt` 或 `time`（本仓分钟线的生产列名是 `time`，见 `chart_cache._MINUTE_COLS`；
    结构层内部用 `dt`）——两者都收，省掉调用方 rename 的踩坑。
    """
    d = df5.copy()
    if "dt" not in d.columns and "time" in d.columns:
        d = d.rename(columns={"time": "dt"})
    d["dt"] = pd.to_datetime(d["dt"])
    d = d[d["dt"].dt.strftime("%H:%M") != "09:30"]
    d["date"] = d["dt"].dt.normalize()
    key = [c for c in ("symbol",) if c in d.columns]
    d["_seq"] = d.groupby(key + ["date"], sort=False).cumcount()
    d["_g"] = d["_seq"] // 2
    g = d.groupby(key + ["date", "_g"], sort=False)
    # 只聚合**实际存在**的列：生产 tushare 5min 无 amount（research parquet 有）
    agg = {"open": ("open", "first"), "high": ("high", "max"), "low": ("low", "min"),
           "close": ("close", "last"), "dt": ("dt", "last")}
    for opt in ("volume", "amount"):
        if opt in d.columns:
            agg[opt] = (opt, "sum")
    out = g.agg(**agg).reset_index()
    return out.sort_values(key + ["dt"], kind="mergesort").reset_index(drop=True)


# --------------------------------------------------------------------------
# 日线四阶段（**单一真源**；研究脚本 t_io/validation/daily_stage/ 直接 import 本函数）
# --------------------------------------------------------------------------
def stage_daily(daily: pd.DataFrame) -> dict:
    """日线 → 四阶段（蓄势/抬升/派发/下跌），**全因果**（只用当根及之前）。

    定义（判定优先级：④ > ③ > ② > ①）：
      ②抬升 = 多头排列(close>MA20>MA60) 且 MA20 较 5 日前上行
      ③派发 = MA20>MA60（曾在上方）但 close<MA20（丢了短均线）
      ④下跌 = 空头排列(close<MA20<MA60) 且 MA20 较 5 日前下行
      ①蓄势 = 其余（MA20/MA60 缠绕、价格在均线附近）

    ⚠️ 实测（977 只 × 2265 日 / 163 万股票·日）：
      · 分布不退化：①26.9% ②24.8% ③21.2% ④27.1%（对比 10min 版末根 ①83%）。
      · 横截面 IC 上**方向与直觉相反**：②抬升 IC −0.031~−0.042(t=−12~−15)，
        ④下跌 IC +0.020~+0.034(t=+7~+12) ⇒ **"抬升/追高"前瞻更差、"下跌/超跌"前瞻更好**，
        分半窗两段同号。效应量小（IC≈0.04）⇒ 只作**相对强弱排序**，不是独立交易依据。
        见 doc/research/2026-10-11_日线四阶段_得分区分度实验.md
    """
    need = max(MA_MID_D, 60) + 6
    if daily is None or len(daily) < need:
        return {"ok": False, "reason": f"日线不足(<{need})",
                "stage_daily": None, "stage_daily_code": None}
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    c = d["close"].astype(float)
    ma20 = c.rolling(MA_MID_D).mean()
    ma60 = c.rolling(60).mean()
    px = float(c.iloc[-1])
    m20, m60 = float(ma20.iloc[-1]), float(ma60.iloc[-1])
    up20 = m20 > float(ma20.iloc[-6])                # MA20 较 5 日前上行
    dn20 = m20 < float(ma20.iloc[-6])
    code = 1                                         # 默认 ①蓄势
    if px > m20 > m60 and up20:
        code = 2
    if m20 > m60 and px < m20:
        code = 3
    if px < m20 < m60 and dn20:
        code = 4
    return {"ok": True, "stage_daily": STAGE_LABEL[code], "stage_daily_code": code,
            "ma20_daily": round(m20, 3), "ma60_daily": round(m60, 3),
            "ma20_daily_up5": bool(up20)}


# --------------------------------------------------------------------------
# 10min 四阶段
# --------------------------------------------------------------------------
def _stage10(close: np.ndarray, high: np.ndarray, low: np.ndarray) -> dict:
    n = len(close)
    sh_i, sh_v, sl_i, sl_v = _swing_points(high, low)
    res_sw, prev_sh = _ff_last_prev(sh_i, sh_v, n)
    sup, prev_sl = _ff_last_prev(sl_i, sl_v, n)
    # 短线阻力：近 RES_WIN 根最高（不含当根）—— 破它 = 创短线新高
    res = np.full(n, np.nan)
    c = np.full(n + 1, -np.inf)
    np.maximum.accumulate(high, out=c[1:])
    for t in range(1, n):
        lo = max(0, t - RES_WIN_10)
        res[t] = high[lo:t].max() if t > lo else np.nan
    res_prev = np.r_[np.nan, res[:-1]]
    cl_prev = np.r_[np.nan, close[:-1]]
    brk = (close > res) & (cl_prev <= res_prev)
    ma_f, ma_s = _sma(close, MA_FAST_10), _sma(close, MA_SLOW_10)
    ma_cross_dn = ((ma_f < ma_s) & (np.r_[np.nan, ma_f[:-1]] >= np.r_[np.nan, ma_s[:-1]]))
    falling = (prev_sh > res_sw) & (prev_sl > sup)
    rising = (prev_sh < res_sw) & (prev_sl < sup)
    stage = np.ones(n, int)                       # 默认 ①蓄势
    stage = np.where(brk, 2, stage)
    stage = np.where(close < sup, 3, stage)
    stage = np.where(ma_cross_dn, 3, stage)
    stage = np.where(falling, 4, stage)           # ④优先级最高
    return {"stage": stage, "sup": sup, "res_swing": res_sw, "res_win": res,
            "rising": rising, "falling": falling, "ma20": ma_f, "ma50": ma_s}


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def compute_structure(bars10: pd.DataFrame, bars30: pd.DataFrame,
                      daily: pd.DataFrame) -> dict:
    """三分周期结构 → 仓位管理参考。

    bars10 / bars30: 列 [dt, open, high, low, close]（升序，可含 symbol 列但须单只）
    daily:           列 [date, open, high, low, close]（升序，单只）
    返回 dict；数据不足 → {"ok": False, "reason": ...}
    """
    def _prep(df, col):
        d = df.copy()
        if col not in d.columns and "time" in d.columns:   # 生产分钟线用 `time`，本层内部用 `dt`
            d = d.rename(columns={"time": col})
        d[col] = pd.to_datetime(d[col])
        return d.sort_values(col).reset_index(drop=True)

    b10 = _prep(bars10, "dt")
    b30 = _prep(bars30, "dt")
    dl = _prep(daily, "date")
    if len(b10) < 80 or len(b30) < MA_MID_30 + 10 or len(dl) < max(MA_MID_D, 20) + 2:
        return {"ok": False, "reason": f"数据不足(10m={len(b10)},30m={len(b30)},日={len(dl)})"}

    c10 = b10["close"].to_numpy(float)
    s10 = _stage10(c10, b10["high"].to_numpy(float), b10["low"].to_numpy(float))
    px = float(c10[-1])
    stop = float(s10["sup"][-1]) if np.isfinite(s10["sup"][-1]) else np.nan

    # —— 中期（30min）——
    c30 = b30["close"].to_numpy(float)
    ma65 = _sma(c30, MA_MID_30)
    m65 = float(ma65[-1]) if np.isfinite(ma65[-1]) else np.nan
    rising65 = (np.isfinite(ma65[-1]) and np.isfinite(ma65[-9]) and ma65[-1] > ma65[-9])
    if not np.isfinite(m65):
        mid30 = "flat"
    elif c30[-1] > m65 and rising65:
        mid30 = "up"
    elif c30[-1] < m65 and not rising65:
        mid30 = "down"
    else:
        mid30 = "flat"
    _, _, sl30_i, sl30_v = _swing_points(b30["high"].to_numpy(float),
                                         b30["low"].to_numpy(float))
    sh30_i, sh30_v, _, _ = _swing_points(b30["high"].to_numpy(float),
                                         b30["low"].to_numpy(float))
    sup30 = float(sl30_v[-1]) if len(sl30_v) else np.nan     # 已确认，故因果

    # —— 30min MA5 状态（**纯展示项**，owner 要求看盘感）——
    # ⚠️ 实测**无前瞻信息量**：977只×361日，四种口径（状态/跃迁 × 站上/跌破）在
    #    「同日同 bar-of-day」对照下 |diff| ≤ 0.03pp、|t| ≤ 2.4（n=35万~130万）。
    #    （朴素对照会给"−0.343pp t=−25.5"，那是**日内时段伪影**，勿采信。）
    #    ⇒ 只做展示，**不参与任何判定/评分/择时**。见
    #    doc/research/2026-10-11_30min站上MA5_作仓位依据实验.md
    ma5_30 = _sma(c30, MA_FAST_30)
    m5_30 = float(ma5_30[-1]) if np.isfinite(ma5_30[-1]) else np.nan
    above_ma5 = bool(np.isfinite(m5_30) and c30[-1] > m5_30)
    bars_since_cross = None
    _fin = np.isfinite(ma5_30)
    if _fin.any():
        _s = int(np.flatnonzero(_fin)[0])          # MA5 首个有效根（此前全 NaN，不参与"翻转"）
        _ab = c30[_s:] > ma5_30[_s:]
        _flip = np.flatnonzero(_ab[1:] != _ab[:-1])
        bars_since_cross = int(len(_ab) - 1 - int(_flip[-1])) if len(_flip) else None
    res30 = float(sh30_v[-1]) if len(sh30_v) else np.nan

    # —— 日线中期成本线 + 结构低点 ——
    dc = dl["close"].to_numpy(float)
    _sd = stage_daily(dl)
    ma20d = float(_sma(dc, MA_MID_D)[-1])
    low20d = float(dl["low"].tail(20).min())
    prev_close = float(dc[-2]) if len(dc) >= 2 else np.nan
    # 破位后经过的交易日（用于补仓窗口时效）
    days_since_break = 0
    if np.isfinite(stop):
        brk_dn = np.where(c10 < stop)[0]
        if len(brk_dn):
            last_d = b10["dt"].iloc[brk_dn[-1]].normalize()
            days_since_break = int((b10["dt"].iloc[-1].normalize() - last_d).days)

    # —— 折价度（现价 vs 止损位）——
    disc = (px / stop - 1.0) if (np.isfinite(stop) and stop > 0) else np.nan
    disc_score = (round(min(1.0, max(0.0, -disc) / FULL_DISCOUNT) * 100, 1)
                  if np.isfinite(disc) else np.nan)

    # —— 三档下档支撑（补仓）——
    tiers = []
    if np.isfinite(stop):
        for name, lvl in (("T1 30min MA65", m65),
                          ("T2 日线 MA20", ma20d),
                          ("T3 前20日最低", low20d)):
            if not np.isfinite(lvl) or lvl <= 0 or lvl >= stop:
                continue                      # 位于止损位上方的档位自动作废
            dp = round((lvl / stop - 1) * 100, 2)
            tiers.append({"name": name, "price": round(lvl, 3),
                          "dist_from_stop_pct": dp,
                          "hist_fill_rate_pct": TIER_FILL_RATE[name],
                          "exp_fill_rate_pct": expected_fill_rate(dp),
                          "low_prob": expected_fill_rate(dp) < 20.0})

    stage = int(s10["stage"][-1])
    return {
        "ok": True,
        "price": round(px, 3),
        "prev_close": round(prev_close, 3) if np.isfinite(prev_close) else None,
        "stop_level": round(stop, 3) if np.isfinite(stop) else None,
        "stop_basis": "10min 最近已确认摆动低点",
        "resist_10min_swing": (round(float(s10["res_swing"][-1]), 3)
                               if np.isfinite(s10["res_swing"][-1]) else None),
        "resist_10min_win": (round(float(s10["res_win"][-1]), 3)
                             if np.isfinite(s10["res_win"][-1]) else None),
        "resist_30min": round(res30, 3) if np.isfinite(res30) else None,
        "support_30min": round(sup30, 3) if np.isfinite(sup30) else None,
        "stage10": STAGE_LABEL[stage],
        "stage10_code": stage,
        # 日线四阶段（与 10min 版并列；日线版分布不退化，且横截面有区分度——方向见 stage_daily 注释）
        "stage_daily": _sd.get("stage_daily"),
        "stage_daily_code": _sd.get("stage_daily_code"),
        "ma60_daily": _sd.get("ma60_daily"),
        "ma20_daily_up5": _sd.get("ma20_daily_up5"),
        "ma20_10min": round(float(s10["ma20"][-1]), 3) if np.isfinite(s10["ma20"][-1]) else None,
        "ma65_30min": round(m65, 3) if np.isfinite(m65) else None,
        # 纯展示（无前瞻信息量，不参与判定）：30min MA5 状态与"距上次翻转几根"
        "ma5_30min": round(m5_30, 3) if np.isfinite(m5_30) else None,
        "above_ma5_30": above_ma5,
        "bars_since_ma5_cross": bars_since_cross,
        "ma20_daily": round(ma20d, 3),
        "low20_daily": round(low20d, 3),
        "mid_trend_30": TREND_LABEL[mid30],
        "mid_trend_30_code": mid30,
        "broke_stop": bool(np.isfinite(stop) and px < stop),
        "days_since_break": days_since_break,
        "refill_valid": bool(np.isfinite(stop) and px < stop
                             and days_since_break <= REFILL_VALID_DAYS),
        "discount_pct": round(disc * 100, 2) if np.isfinite(disc) else None,
        "discount_score": disc_score,
        "refill_tiers": tiers,
    }


def format_structure(st: dict) -> str:
    """一行摘要 + 明细（供 GUI tooltip / trace 落盘）。"""
    if not st.get("ok"):
        return f"[结构] 不可用：{st.get('reason')}"
    head = (f"[结构] 现价{st['price']}  10min阶段{st['stage10']}  "
            f"日线阶段{st.get('stage_daily') or '—'}  30min中期{st['mid_trend_30']}  "
            f"止损{st['stop_level']}  折价{st['discount_pct']}%（折价分{st['discount_score']}）")
    lines = [head]
    lines.append(f"   阻力：10min摆动高点{st['resist_10min_swing']} / 10min区间高{st['resist_10min_win']}"
                 f" / 30min{st['resist_30min']}")
    lines.append(f"   支撑：30min摆动低点{st['support_30min']}  中期线：30min MA65={st['ma65_30min']}"
                 f"  日线 MA20={st['ma20_daily']}  前20日最低={st['low20_daily']}")
    if st.get("ma5_30min") is not None:
        _n = st.get("bars_since_ma5_cross")
        lines.append(f"   MA5(30min)={st['ma5_30min']}  "
                     f"{'站上' if st.get('above_ma5_30') else '跌破'}"
                     f"{f'（距上次翻转 {_n} 根）' if _n is not None else ''}"
                     f"   ⚠️ 本项**实测无前瞻信息量**（时段匹配对照 |diff|≤0.03pp），仅展示、不参与判定")
    if st["refill_valid"]:
        lines.append(f"   补仓窗口有效（破位后 {st['days_since_break']} 交易日 ≤ {REFILL_VALID_DAYS}）：")
        for t in st["refill_tiers"]:
            flag = "  ⚠️5日内成交概率低" if t.get("low_prob") else ""
            lines.append(f"      {t['name']} 挂{t['price']}（距止损{t['dist_from_stop_pct']:+.2f}%，"
                         f"5日成交率估{t['exp_fill_rate_pct']}%）{flag}")
        lines.append("      纪律：不追市价；5 交易日未成交即作废；"
                     "禁止『站上10min MA20』或『10min重新突破』补仓（历史跑输随机）")
    elif st["broke_stop"]:
        lines.append(f"   已破止损且超 {REFILL_VALID_DAYS} 交易日：本轮补仓作废，等新结构")
    else:
        lines.append("   未破止损：止损位仍有效，仓位管理以持有为主")
    lines.append("   ⚠️ 本层为**结构参考**，非买卖信号；机械按阶段加减仓经实测无正贡献")
    return "\n".join(lines)
