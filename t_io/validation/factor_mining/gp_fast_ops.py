# -*- coding: utf-8 -*-
"""gp_ops 的向量化加速层（任务 S2-6 配套，2026-09-19）。

## 为什么存在

`gp_ops.py` / `factor_ops.py` 的滚动算子是逐 bar Python 循环的**参考实现**
（正确性优先）：ts_corr_w20 在 167 日 × 241 根面板上实测 17.3s/表达式，
GP 搜索 4 万次评估不可行。本模块提供**语义逐一对齐**的 numpy 向量化版本
（axis=-1 滚动，1D/2D 通吃），不改任何只读文件。

## 对齐纪律

- 每个算子与 gp_ops 同名同参同语义：窗口右对齐含当前 bar、不足窗口 / 窗内
  含非有限值 → NaN（smart_money_dev 例外：其参考实现只要求 c/v 有限，
  ret 的 NaN 走 nan_to_num——本模块精确复刻）；
- 测试 `test_gp_miner.py::test_fast_ops_match_gp_ops` 对全部窗口算子做
  随机数据逐点对照（equal_nan，atol=1e-12，smart_money_dev 因并列秩
  可能选集不同放宽到 1e-9）；
- 2D 输入沿最后轴滚动：配合 `gp_miner.ExprEvaluator.eval_matrix` 的
  (n_days, L) 逐日矩阵，天然不跨日（§6.3 风险项）。

性能基准（167 日 × 241 根，托管 python 3.12 / numpy 2.4）：
  ts_mean_w10  2212ms → ~2ms；ts_corr_w20  17300ms → ~15ms。
"""
from __future__ import annotations

import numpy as np

EPS = 1e-12


# ── 滚动原语（axis=-1）─────────────────────────────────────────────────────
def _view(x, w):
    """(…, n) → (…, n-w+1, w) 滑动窗视图（不含 NaN 前缀）。"""
    return np.lib.stride_tricks.sliding_window_view(x, w, axis=-1)


def _pad(y, w):
    """滚动结果前补 w-1 个 NaN，对齐输入长度。"""
    return np.concatenate([np.full(y.shape[:-1] + (w - 1,), np.nan), y], axis=-1)


def _sums(v):
    """窗内和 / 平方和 / 全有限掩码。"""
    fin = np.isfinite(v)
    ok = fin.all(axis=-1)
    s1 = np.where(fin, v, 0.0).sum(axis=-1)
    s2 = np.where(fin, v * v, 0.0).sum(axis=-1)
    return s1, s2, ok


def _ret(c):
    """分钟收益（首根 NaN；前收 ≤EPS → NaN），与 gp_ops 各算子内部口径一致。"""
    prev = np.where(c[..., :-1] > EPS, c[..., :-1], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = c[..., 1:] / prev - 1.0
    return np.concatenate([np.full(c.shape[:-1] + (1,), np.nan), r], axis=-1)


# ── 组① 时序统计 ──────────────────────────────────────────────────────────
def ts_mean(x, n):
    s1, _s2, ok = _sums(_view(x, n))
    return _pad(np.where(ok, s1 / n, np.nan), n)


def ts_std(x, n):
    s1, s2, ok = _sums(_view(x, n))
    m = s1 / n
    var = (s2 - n * m * m) / (n - 1)
    return _pad(np.where(ok, np.sqrt(np.maximum(var, 0.0)), np.nan), n)


def ts_max(x, n):
    v = _view(x, n)
    ok = np.isfinite(v).all(axis=-1)
    out = np.where(np.isfinite(v), v, -np.inf).max(axis=-1)
    return _pad(np.where(ok, out, np.nan), n)


def ts_min(x, n):
    v = _view(x, n)
    ok = np.isfinite(v).all(axis=-1)
    out = np.where(np.isfinite(v), v, np.inf).min(axis=-1)
    return _pad(np.where(ok, out, np.nan), n)


def ts_rank(x, n):
    v = _view(x, n)
    ok = np.isfinite(v).all(axis=-1)
    cur = x[..., n - 1:]
    rank = (v <= cur[..., None]).mean(axis=-1)
    return _pad(np.where(ok & np.isfinite(cur), rank, np.nan), n)


def ts_delay(x, n):
    if n >= x.shape[-1]:
        return np.full_like(x, np.nan)
    pad = np.full(x.shape[:-1] + (n,), np.nan)
    return np.concatenate([pad, x[..., : x.shape[-1] - n]], axis=-1)


def ts_delta(x, n):
    return x - ts_delay(x, n)


def ts_argmax(x, n):
    """窗口内最大值距今 bar 数（0=当前；并列取最旧，同 np.argmax 语义）。"""
    v = _view(x, n)
    ok = np.isfinite(v).all(axis=-1)
    out = n - 1 - np.argmax(np.where(np.isfinite(v), v, -np.inf), axis=-1)
    return _pad(np.where(ok, out.astype(float), np.nan), n)


def decay_linear(x, n):
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()
    v = _view(x, n)
    ok = np.isfinite(v).all(axis=-1)
    out = (np.where(np.isfinite(v), v, 0.0) * w).sum(axis=-1)
    return _pad(np.where(ok, out, np.nan), n)


# ── 组② 量价关系 ──────────────────────────────────────────────────────────
def ts_corr(p, v_, n):
    """滚动 Pearson（总体矩，ddof 在相关系数中约掉；零方差 → NaN，同参考实现）。"""
    vp, vv = _view(p, n), _view(v_, n)
    sp1, sp2, okp = _sums(vp)
    sv1, sv2, okv = _sums(vv)
    sxy = np.where(np.isfinite(vp) & np.isfinite(vv), vp * vv, 0.0).sum(axis=-1)
    ok = okp & okv
    mp, mv = sp1 / n, sv1 / n
    cov = sxy / n - mp * mv
    varp = np.maximum(sp2 / n - mp * mp, 0.0)
    varv = np.maximum(sv2 / n - mv * mv, 0.0)
    denom = np.sqrt(varp * varv)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(denom > EPS * EPS, cov / np.where(denom > 0, denom, 1.0), np.nan)
    return _pad(np.where(ok, out, np.nan), n)


def ts_cov(p, v_, n):
    """滚动协方差（ddof=1；零方差时协方差=0 合法，同参考实现）。"""
    vp, vv = _view(p, n), _view(v_, n)
    sp1, _sp2, okp = _sums(vp)
    sv1, _sv2, okv = _sums(vv)
    sxy = np.where(np.isfinite(vp) & np.isfinite(vv), vp * vv, 0.0).sum(axis=-1)
    ok = okp & okv
    cov = (sxy - sp1 * sv1 / n) / (n - 1)
    return _pad(np.where(ok, cov, np.nan), n)


def vwap_dev(c, amt, v):
    """c / 累计 VWAP − 1（沿 axis=-1 从行首累计；2D 即按日）。"""
    cum_v = np.cumsum(v, axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(cum_v > EPS,
                        np.cumsum(amt, axis=-1) / np.where(cum_v > EPS, cum_v, 1.0), c)
        return c / np.where(vwap > EPS, vwap, np.nan) - 1.0


def smart_money_dev(c, v, n=120):
    """聪明钱 VWAP 偏离：窗口内按 S=|r|/v^0.5 降序，取累计成交量达 20% 的分钟。

    精确复刻 factor_ops.smart_money_dev（top_frac=0.2, beta=0.5）：
    只要求窗口内 c/v 全有限（ret 的 NaN 经 nan_to_num 置 0 参与排序）；
    并列 S 的排序差异理论上可能改变选集，随机实数数据概率为零。
    """
    r = np.nan_to_num(_ret(c), nan=0.0)
    vc, vv, vr = _view(c, n), _view(v, n), _view(r, n)
    ok = np.isfinite(vc).all(axis=-1) & np.isfinite(vv).all(axis=-1)
    S = np.abs(np.nan_to_num(vr, nan=0.0, posinf=0.0, neginf=0.0)) \
        / np.power(np.maximum(vv, 1.0), 0.5)
    order = np.argsort(-S, axis=-1, kind="stable")
    cs = np.take_along_axis(vc, order, axis=-1)
    vs = np.take_along_axis(vv, order, axis=-1)
    cum = np.cumsum(vs, axis=-1)
    tot = cum[..., -1:]
    # k = searchsorted(cum, tot*0.2) + 1（cum 单调非降 → 等价于计数）
    k = (cum < tot * 0.2).sum(axis=-1, keepdims=True) + 1
    sel = np.arange(n) < np.maximum(k, 1)
    w = np.where(sel, vs, 0.0)
    wsum = w.sum(axis=-1)
    vw = np.where(wsum > EPS, (cs * w).sum(axis=-1) / np.where(wsum > 0, wsum, 1.0),
                  np.nan)
    cur = c[..., n - 1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where((vw > EPS) & (tot[..., 0] > EPS),
                       cur / np.where(vw > 0, vw, 1.0) - 1.0, np.nan)
    return _pad(np.where(ok, out, np.nan), n)


def amount_ratio(amt, n):
    """amt / mean(amt 过去 n 根，不含当前) − 1。"""
    m = ts_delay(ts_mean(amt, n), 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return amt / np.where(m > EPS, m, np.nan) - 1.0


# ── 组③ 日内结构 ──────────────────────────────────────────────────────────
def day_vwap(amt, v):
    cum_v = np.cumsum(v, axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.cumsum(amt, axis=-1) / np.where(cum_v > EPS, cum_v, np.nan)


def open_ret(c, o):
    """c / o[行首] − 1；o[行首] 非有限或 ≤EPS → 全行 NaN。"""
    o0 = o[..., :1]
    bad = (~np.isfinite(o0)) | (o0 <= EPS)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = c / np.where(bad, np.nan, o0) - 1.0
    return np.where(np.broadcast_to(bad, c.shape), np.nan, out)


def tail30_ret(c):
    base = ts_delay(c, 30)
    with np.errstate(divide="ignore", invalid="ignore"):
        return c / np.where(base > EPS, base, np.nan) - 1.0


def amihud(c, amt, n):
    r = _ret(c)
    with np.errstate(divide="ignore", invalid="ignore"):
        illiq = np.abs(r) / np.where(amt > EPS, amt, np.nan)
    return ts_mean(illiq, n)


def realized_skew(c, n):
    """已实现偏度：mean(((r−mean)/std_ddof1)³)，窗内零波动 → NaN。"""
    v = _view(_ret(c), n)
    s1, s2, ok = _sums(v)
    s3 = np.where(np.isfinite(v), v ** 3, 0.0).sum(axis=-1)
    m = s1 / n
    central3 = s3 / n - 3.0 * m * (s2 / n) + 2.0 * m ** 3
    var1 = np.maximum((s2 - n * m * m) / (n - 1), 0.0)   # ddof=1
    s = np.sqrt(var1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(s > EPS, central3 / np.where(s > 0, s, 1.0) ** 3, np.nan)
    return _pad(np.where(ok, out, np.nan), n)


# ── namespace 构建（与 gp_ops.build_function_set 同名同 arity）────────────
def build_fast_namespace(windows=(5, 10, 20, 30, 60), include_csrank=False):
    """返回 {算子名: 向量化函数}，命名与 gp_ops.build_function_set 完全对齐。"""
    ns = {}
    for w in windows:
        w = int(w)
        ns[f"ts_mean_w{w}"] = (lambda _w: lambda x: ts_mean(x, _w))(w)
        ns[f"ts_std_w{w}"] = (lambda _w: lambda x: ts_std(x, _w))(w)
        ns[f"ts_max_w{w}"] = (lambda _w: lambda x: ts_max(x, _w))(w)
        ns[f"ts_min_w{w}"] = (lambda _w: lambda x: ts_min(x, _w))(w)
        ns[f"ts_rank_w{w}"] = (lambda _w: lambda x: ts_rank(x, _w))(w)
        ns[f"ts_delta_w{w}"] = (lambda _w: lambda x: ts_delta(x, _w))(w)
        ns[f"ts_delay_w{w}"] = (lambda _w: lambda x: ts_delay(x, _w))(w)
        ns[f"ts_argmax_w{w}"] = (lambda _w: lambda x: ts_argmax(x, _w))(w)
        ns[f"decay_linear_w{w}"] = (lambda _w: lambda x: decay_linear(x, _w))(w)
        ns[f"ts_corr_w{w}"] = (lambda _w: lambda x, y: ts_corr(x, y, _w))(w)
        ns[f"ts_cov_w{w}"] = (lambda _w: lambda x, y: ts_cov(x, y, _w))(w)
        ns[f"amount_ratio_w{w}"] = (lambda _w: lambda x: amount_ratio(x, _w))(w)
        ns[f"amihud_w{w}"] = (lambda _w: lambda x, y: amihud(x, y, _w))(w)
        ns[f"realized_skew_w{w}"] = (lambda _w: lambda x: realized_skew(x, _w))(w)
        ns[f"smart_money_dev_w{w}"] = (lambda _w:
                                       lambda x, y: smart_money_dev(x, y, _w))(w)
    ns["vwap_dev"] = vwap_dev
    ns["day_vwap"] = day_vwap
    ns["open_ret"] = open_ret
    ns["tail30_ret"] = tail30_ret
    if include_csrank:
        from t_io.validation.factor_mining import gp_ops
        ns["cs_rank"] = gp_ops.cs_rank        # 截面算子无加速需求，直接用参考实现
    return ns
