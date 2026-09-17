# -*- coding: utf-8 -*-
"""日内因子算子层（DSL 求值器）—— 严格因果，bar i 只用 ≤ i 的数据。

## 为什么自己写而不引 qlib

qlib 的算子与会话里其它结论的数据层/成本口径不兼容，接进来等于重建一套对照，
反而让结果无法与本会话既有实验（`t0_schemes/run_experiment.py`）对齐。
此处沿用 Alpha101/qlib 式的**算子命名**，但落在我们自己的 1min 面板上。

## 口径（必须钉死，否则因子值不可比）

**VWAP = 当日累计 Σamt / Σv**，与 `t0_schemes/run_experiment.py:646` 一致。
⚠️ 仓库另有 4 处 VWAP 实现且单位口径不一（`analysis/indicators.py:34` 是 ×100 的元/股），
**本模块一律用上面这个口径**，不要混用。

## 因果性

所有 `ts_*` 算子只用**窗口内已发生的 bar**（右对齐窗口，含当前 bar，不含未来）。
`eval_factor.py` 另有一条前视自检：把因子整体后移 5 根 bar，指标应显著劣化。
"""
from __future__ import annotations

import numpy as np

EPS = 1e-12


# ── 基础工具 ──────────────────────────────────────────────────────────────
def _arr(day, k):
    return np.asarray([b[k] for b in day], float)


def day_ctx(day, prev_close=0.0, daily_atr=0.0):
    """把一天的 bar list 拆成向量 + 常用派生量。day 为 bar dict 列表（字段 t,o,h,l,c,v,amt）。"""
    o, h, l, c = _arr(day, 'o'), _arr(day, 'h'), _arr(day, 'l'), _arr(day, 'c')
    v = _arr(day, 'v')
    amt = _arr(day, 'amt') if 'amt' in day[0] else c * v
    cum_v = np.cumsum(v)
    with np.errstate(divide='ignore', invalid='ignore'):
        vwap = np.where(cum_v > 0, np.cumsum(amt) / np.where(cum_v > 0, cum_v, 1.0), c)
    ret = np.zeros_like(c)
    ret[1:] = c[1:] / np.where(c[:-1] > 0, c[:-1], np.nan) - 1
    hi_so_far = np.maximum.accumulate(h)
    lo_so_far = np.minimum.accumulate(l)
    return {'t': [b['t'] for b in day], 'o': o, 'h': h, 'l': l, 'c': c, 'v': v, 'amt': amt,
            'vwap': vwap, 'ret': ret, 'hi_so_far': hi_so_far, 'lo_so_far': lo_so_far,
            'prev_close': float(prev_close or 0.0), 'daily_atr': float(daily_atr or 0.0),
            'n': len(c)}


# ── 逐元素 ────────────────────────────────────────────────────────────────
def abs_(x):
    return np.abs(x)


def neg(x):
    return -x


def log_(x):
    return np.log(np.abs(x) + EPS)


def sqrt_(x):
    return np.sqrt(np.abs(x))


def sign_(x):
    return np.sign(x)


def add(x, y):
    return x + y


def sub(x, y):
    return x - y


def mul(x, y):
    return x * y


def div(x, y):
    return x / np.where(np.abs(y) > EPS, y, np.nan)


def where_(cond, a, b):
    return np.where(cond, a, b)


# ── 时序（全部因果：窗口右对齐，含当前 bar）────────────────────────────────
def _roll(x, n, fn):
    """因果滚动：out[i] = fn(x[max(0,i-n+1) : i+1])；窗口不足 n 时为 NaN。"""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        w = x[i - n + 1:i + 1]
        if np.all(np.isfinite(w)):
            out[i] = fn(w)
    return out


def delay(x, n):
    """向右移 n 根：out[i] = x[i-n]。"""
    out = np.full(len(x), np.nan)
    if n < len(x):
        out[n:] = x[:len(x) - n]
    return out


def delta(x, n):
    return x - delay(x, n)


def ts_mean(x, n):
    return _roll(x, n, np.mean)


def ts_std(x, n):
    return _roll(x, n, lambda w: np.std(w, ddof=1) if len(w) > 1 else np.nan)


def ts_sum(x, n):
    return _roll(x, n, np.sum)


def ts_max(x, n):
    return _roll(x, n, np.max)


def ts_min(x, n):
    return _roll(x, n, np.min)


def ts_corr(x, y, n):
    """因果滚动 Pearson 相关（CPV 用）。"""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        a, b = x[i - n + 1:i + 1], y[i - n + 1:i + 1]
        if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
            continue
        if np.std(a) < EPS or np.std(b) < EPS:
            continue
        out[i] = float(np.corrcoef(a, b)[0, 1])
    return out


def ts_rank(x, n):
    """当前值在过去 n 根内的分位（0~1，因果）。"""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        if i + 1 < n:
            continue
        w = x[i - n + 1:i + 1]
        if np.all(np.isfinite(w)):
            out[i] = float((w <= x[i]).mean())
    return out


def decay_linear(x, n):
    """线性衰减加权均值（近端权重高）。"""
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()

    def _f(a):
        return float(np.dot(a, w))
    return _roll(x, n, _f)


def ema(x, n):
    """因果 EMA（α=2/(n+1)）；前 n 根为 NaN 避免未收敛值污染。"""
    a = 2.0 / (n + 1.0)
    out = np.full(len(x), np.nan)
    prev = np.nan
    for i, val in enumerate(x):
        if not np.isfinite(val):
            continue
        prev = val if not np.isfinite(prev) else a * val + (1 - a) * prev
        if i + 1 >= n:
            out[i] = prev
    return out


# ── 日内结构量 ────────────────────────────────────────────────────────────
def range_pos(ctx):
    """当日**已发生**区间内的价格位置 (c−low)/(high−low)，因果。"""
    hi, lo, c = ctx['hi_so_far'], ctx['lo_so_far'], ctx['c']
    rng = hi - lo
    return np.where(rng > EPS, (c - lo) / np.where(rng > EPS, rng, 1.0), 0.5)


def dist_high(ctx):
    """距当日**迄今**最高点的距离（≤0）。"""
    return ctx['c'] / np.where(ctx['hi_so_far'] > EPS, ctx['hi_so_far'], np.nan) - 1


def dist_low(ctx):
    """距当日**迄今**最低点的距离（≥0）。"""
    return ctx['c'] / np.where(ctx['lo_so_far'] > EPS, ctx['lo_so_far'], np.nan) - 1


def vwap_dev(ctx):
    """价格对当日累计 VWAP 的偏离（正=在均价上方）。"""
    return ctx['c'] / np.where(ctx['vwap'] > EPS, ctx['vwap'], np.nan) - 1


def vol_ratio(ctx, n=30):
    """当前分钟量 / 过去 n 分钟**因果**均量（不含当前，避免自比）。"""
    m = delay(ts_mean(ctx['v'], n), 1)
    return ctx['v'] / np.where(m > EPS, m, np.nan) - 1


def downvol_share(ctx, n=60):
    """下行波动占比 = Σr²·I(r<0) / Σr²（海通口径），因果滚动。"""
    out = np.full(ctx['n'], np.nan)
    r = ctx['ret']
    for i in range(len(r)):
        if i + 1 < n:
            continue
        w = r[i - n + 1:i + 1]
        if not np.all(np.isfinite(w)):
            continue
        s2 = np.sum(w ** 2)
        out[i] = float(np.sum(w[w < 0] ** 2) / s2) if s2 > EPS else np.nan
    return out


def realized_skew(ctx, n=60):
    """已实现偏度（分钟收益的三阶标准化矩）。"""
    def _sk(w):
        s = np.std(w, ddof=1)
        if s < EPS:
            return np.nan
        return float(np.mean(((w - np.mean(w)) / s) ** 3))
    return _roll(ctx['ret'], n, _sk)


def volume_quantile(ctx):
    """当前分钟量在当日**迄今**分钟量中的分位（因果）。"""
    v = ctx['v']
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        w = v[:i + 1]
        out[i] = float((w <= v[i]).mean())
    return out


def volume_concentration(ctx):
    """成交量集中度 HHI（迄今累积，归一化到 [1/k, 1]）；越大越集中。"""
    v = ctx['v']
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        w = v[:i + 1]
        s = w.sum()
        if s > EPS and i >= 20:
            p = w / s
            out[i] = float(np.sum(p ** 2))
    return out


def tail_vol_share(ctx, tail_from='14:30'):
    """尾盘（>=tail_from）成交量占当日**迄今**总量比；尾盘前该值无意义（NaN）。"""
    ts = ctx['t']
    v = ctx['v']
    idx = [i for i, x in enumerate(ts) if x >= tail_from]
    out = np.full(len(v), np.nan)
    if not idx:
        return out
    start = idx[0]
    for i in range(start, len(v)):
        tot = v[:i + 1].sum()
        out[i] = float(v[start:i + 1].sum() / tot) if tot > EPS else np.nan
    return out


def smart_money_dev(ctx, top_frac=0.2, beta=0.5, n=120):
    """聪明钱 VWAP 偏离（开源/方正口径，因果滚动版）。

    S_t = |R_t| / V_t^β；取过去 n 根中 S 最大的 top_frac（按成交量累积占比）为"聪明钱"分钟，
    VWAP_smart = 这些分钟的成交量加权均价；返回 c/VWAP_smart − 1（<0 = 价在聪明钱成本之下）。
    """
    c, v, r = ctx['c'], ctx['v'], ctx['ret']
    out = np.full(len(c), np.nan)
    for i in range(len(c)):
        if i + 1 < n:
            continue
        cs, vs, rs = c[i - n + 1:i + 1], v[i - n + 1:i + 1], r[i - n + 1:i + 1]
        if not (np.all(np.isfinite(cs)) and np.all(np.isfinite(vs))):
            continue
        S = np.abs(np.nan_to_num(rs)) / np.power(np.maximum(vs, 1.0), beta)
        order = np.argsort(-S)
        cum = np.cumsum(vs[order])
        tot = cum[-1]
        if tot <= EPS:
            continue
        k = int(np.searchsorted(cum, tot * top_frac) + 1)
        sel = order[:max(k, 1)]
        w = vs[sel]
        if w.sum() <= EPS:
            continue
        vw_smart = float(np.dot(cs[sel], w) / w.sum())
        if vw_smart > EPS:
            out[i] = c[i] / vw_smart - 1
    return out


def tod_bias(day, hist_same_minute_mean, op=None):
    """相对"过去 N 日同一 HH:MM 收盘均值"的偏离（时段效应）。hist 由 eval_factor 预计算。"""
    c = np.asarray([b['c'] for b in day], float)
    base = np.asarray([hist_same_minute_mean.get(b['t'], np.nan) for b in day], float)
    return c / np.where(base > EPS, base, np.nan) - 1
