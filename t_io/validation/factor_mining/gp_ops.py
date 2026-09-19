# -*- coding: utf-8 -*-
"""GP 算子库（任务 S2-3）—— 分钟级 GP 因子挖掘的安全算子集。

## 定位

为 AlphaGen/gplearn 式 GP 搜索提供「安全算子集」：每个算子是
`(array, ...) -> array` 的向量化/逐序列函数，签名兼容 gplearn
`make_function(function, arity)` 约定（一元/二元/三元，参数全部为同长 1D
序列，窗口等超参数通过 `build_function_set()` 工厂烘焙成闭包）。

**gplearn 不是本模块的运行依赖**：本模块只保证签名约定兼容，
`build_function_set()` 返回 `(name, func, arity)` 三元组，GP 层拿到后自行
`gplearn.functions.make_function(...)` 包装即可。

## 纪律（不可妥协）

1. **纯因果**：所有时序算子在 bar i 的输出只用 ≤ i 的数据
   （窗口右对齐、含当前 bar、不含未来）。`cs_rank` 是横截面算子，
   按行（同一时刻）秩化，不涉及时序因果。
2. **除零 / NaN 安全**：分母 ≤ EPS → NaN；窗口内含非有限值 → NaN；
   出口统一 `inf → NaN`，由调用方（GP 适应度层）drop。
3. **复用优先**：`factor_ops.py` 已有的严格因果算子（ts_mean/ts_std/
   ts_max/ts_min/ts_rank/ts_corr/decay_linear/delay/delta/vwap_dev/
   realized_skew/smart_money_dev 等）一律包装复用，不重造；
   新增算子在测试中必须对照 factor_ops 参考实现或手算值。

## 口径

- VWAP = 序列起点起累计 Σamt/Σv（与 factor_ops.day_ctx 一致）；
  调用方必须按日切片传入，跨日累计语义由调用方负责。
- 尾盘 30min 涨幅 = c[i]/c[i-30] − 1（1min bar 上 30 根位移收益），
  与 B7 标定口径 tail30 = close(15:00)/close(14:30)−1 在收盘 bar 处一致
  （见 t_io/validation/t0_schemes/run_b7_overnight.py:8）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

try:
    from t_io.validation.factor_mining import factor_ops as fo
except ModuleNotFoundError:  # 直接 python 运行本文件时，补仓库根路径
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from t_io.validation.factor_mining import factor_ops as fo

EPS = fo.EPS


# ── 输入归一与出口净化 ────────────────────────────────────────────────────
def _to_arr(x):
    """归一输入：ndarray / list / pd.Series / 单列 DataFrame -> 1D float array。

    DataFrame 仅当恰有一列时压平（GP 层逐特征传入，不会喂多列）；
    多列 DataFrame 直接报错，避免静默取错列。
    """
    if hasattr(x, "values"):
        import pandas as pd
        if isinstance(x, pd.DataFrame):
            if x.shape[1] != 1:
                raise ValueError("DataFrame 输入必须恰为一列")
            x = x.iloc[:, 0]
        x = x.values
    return np.asarray(x, dtype=float).ravel()


def _sanitize(out):
    """出口净化：inf -> NaN（NaN 原样保留），返回 float 1D array。"""
    out = np.asarray(out, dtype=float)
    return np.where(np.isfinite(out), out, np.nan)


# ══════════════════════════════════════════════════════════════════════════
# 组① 时序统计（窗口右对齐，含当前 bar，不足窗口 / 窗内有 NaN -> NaN）
# ══════════════════════════════════════════════════════════════════════════
def ts_mean(x, n):
    """滚动均值。窗口语义：过去 n 根（含当前）均值；前 n-1 根为 NaN。复用 factor_ops。"""
    return _sanitize(fo.ts_mean(_to_arr(x), int(n)))


def ts_std(x, n):
    """滚动标准差（ddof=1）。窗口语义同 ts_mean。复用 factor_ops。"""
    return _sanitize(fo.ts_std(_to_arr(x), int(n)))


def ts_max(x, n):
    """滚动最大值。窗口语义同 ts_mean。复用 factor_ops。"""
    return _sanitize(fo.ts_max(_to_arr(x), int(n)))


def ts_min(x, n):
    """滚动最小值。窗口语义同 ts_mean。复用 factor_ops。"""
    return _sanitize(fo.ts_min(_to_arr(x), int(n)))


def ts_rank(x, n):
    """时序分位：当前值在过去 n 根（含当前）中的分位 (w<=x[i]).mean() ∈ (0,1]。
    窗口语义同 ts_mean。复用 factor_ops。"""
    return _sanitize(fo.ts_rank(_to_arr(x), int(n)))


def ts_delta(x, n):
    """差分：x[i] − x[i−n]；前 n 根为 NaN。复用 factor_ops.delta。"""
    return _sanitize(fo.delta(_to_arr(x), int(n)))


def ts_delay(x, n):
    """右移 n 根：out[i] = x[i−n]；前 n 根为 NaN。复用 factor_ops.delay。"""
    return _sanitize(fo.delay(_to_arr(x), int(n)))


def ts_argmax(x, n):
    """窗口内最大值距今的 bar 数（0 = 当前 bar 就是最大值）。

    窗口语义：过去 n 根（含当前）；前 n-1 根 / 窗内有 NaN -> NaN。
    **新增**（factor_ops 无此算子），实现沿用 fo._roll 同款因果骨架。
    """
    x = _to_arr(x)

    def _f(w):
        return float(n - 1 - int(np.argmax(w)))  # argmax 取最旧的最大值

    return _sanitize(fo._roll(x, int(n), _f))


def decay_linear(x, n):
    """线性衰减加权均值（近端权重高，权重 1..n 归一化）。
    窗口语义同 ts_mean。复用 factor_ops。"""
    return _sanitize(fo.decay_linear(_to_arr(x), int(n)))


# ══════════════════════════════════════════════════════════════════════════
# 组② 量价关系
# ══════════════════════════════════════════════════════════════════════════
def ts_corr(p, v, n):
    """量价滚动 Pearson 相关。窗口语义：过去 n 根（含当前）；
    任一序列窗内零方差 -> NaN。复用 factor_ops.ts_corr。"""
    return _sanitize(fo.ts_corr(_to_arr(p), _to_arr(v), int(n)))


def ts_cov(p, v, n):
    """量价滚动协方差（ddof=1）。窗口语义同 ts_corr。

    **新增**（factor_ops 只有 ts_corr）。协方差不要求非零方差，
    只要求窗内全部有限；零方差时协方差=0（合法值，不置 NaN）。
    """
    p, v = _to_arr(p), _to_arr(v)
    n = int(n)
    out = np.full(len(p), np.nan)
    for i in range(len(p)):
        if i + 1 < n:
            continue
        a, b = p[i - n + 1:i + 1], v[i - n + 1:i + 1]
        if np.all(np.isfinite(a)) and np.all(np.isfinite(b)):
            out[i] = float(np.cov(a, b, ddof=1)[0, 1])
    return _sanitize(out)


def vwap_dev(c, amt, v):
    """价格对累计 VWAP 的偏离 c/VWAP − 1。

    窗口语义：从传入序列起点起累计（调用方按日切片）。复用
    factor_ops.vwap_dev（口径 VWAP=Σamt/Σv 与 run_experiment.py:646 一致）。
    """
    c, amt, v = _to_arr(c), _to_arr(amt), _to_arr(v)
    cum_v = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(cum_v > EPS, np.cumsum(amt) / np.where(cum_v > EPS, cum_v, 1.0), c)
    ctx = {"c": c, "vwap": vwap}
    return _sanitize(fo.vwap_dev(ctx))


def smart_money_dev(c, v, n=120):
    """聪明钱 VWAP 偏离（方正/开源口径，因果滚动版）：c/VWAP_smart − 1。

    窗口语义：过去 n 根（含当前）内按 S=|r|/v^β 选聪明钱分钟；
    内部收益 r 由 c 自算（首根 r=NaN，不参与排序）。复用
    factor_ops.smart_money_dev（top_frac=0.2, beta=0.5 默认）。
    """
    c, v = _to_arr(c), _to_arr(v)
    ret = np.full(len(c), np.nan)
    prev = np.where(c[:-1] > EPS, c[:-1], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret[1:] = c[1:] / prev - 1
    ctx = {"c": c, "v": v, "ret": ret, "n": len(c)}
    return _sanitize(fo.smart_money_dev(ctx, n=int(n)))


def amount_ratio(amt, n):
    """成交额相对过去 n 根均额的偏离：amt / mean(amt[i−n..i−1]) − 1。

    窗口语义：均额**不含当前根**（delay(·,1)，与 factor_ops.vol_ratio
    同款自比规避）；前 n 根为 NaN。新增（由 fo.ts_mean + fo.delay 组合）。
    """
    amt = _to_arr(amt)
    m = fo.delay(fo.ts_mean(amt, int(n)), 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return _sanitize(amt / np.where(m > EPS, m, np.nan) - 1)


# ══════════════════════════════════════════════════════════════════════════
# 组③ 日内结构（调用方按日切片传入；累计量从序列起点起算）
# ══════════════════════════════════════════════════════════════════════════
def day_vwap(amt, v):
    """当日累计 VWAP = Σamt/Σv（序列起点起累计，口径同 factor_ops.day_ctx）。

    累计量 ≤0 时回退为 NaN（GP 层不应拿 VWAP 当价格用）。**新增**，
    测试对照 fo.day_ctx 的 vwap 字段。
    """
    amt, v = _to_arr(amt), _to_arr(v)
    cum_v = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.cumsum(amt) / np.where(cum_v > EPS, cum_v, np.nan)
    return _sanitize(out)


def open_ret(c, o):
    """开盘至今涨跌 = c / o[0] − 1（o[0] 为序列首根开盘价）。

    窗口语义：从序列起点起（调用方按日切片）；o[0] ≤ EPS -> 全列 NaN。
    **新增**。
    """
    c, o = _to_arr(c), _to_arr(o)
    if len(o) == 0 or not np.isfinite(o[0]) or o[0] <= EPS:
        return np.full(len(c), np.nan)
    return _sanitize(c / o[0] - 1)


def tail30_ret(c):
    """尾盘 30min 涨幅 = c[i]/c[i−30] − 1（1min bar 上 30 根位移收益）。

    B7 同款口径：标定 tail30 = close(15:00)/close(14:30)−1
    （t_io/validation/t0_schemes/run_b7_overnight.py:8），在收盘 bar 处
    与本算子输出一致；盘中 bar 处是「过去 30 分钟收益」的滚动推广。
    前 30 根为 NaN；基准价 ≤ EPS -> NaN。**新增**（fo.delay 组合）。
    """
    c = _to_arr(c)
    base = fo.delay(c, 30)
    with np.errstate(divide="ignore", invalid="ignore"):
        return _sanitize(c / np.where(base > EPS, base, np.nan) - 1)


def amihud(c, amt, n):
    """Amihud 非流动性 = ts_mean(|r| / amt, n)（未缩放）。

    窗口语义：过去 n 根（含当前）的 |收益|/成交额 均值；首根收益 NaN，
    amt ≤ EPS -> 该根 illiq 为 NaN（窗内含 NaN 则输出 NaN）。
    量级极小属正常（IC 对单调变换不变，GP 层自行缩放）。**新增**。
    """
    c, amt = _to_arr(c), _to_arr(amt)
    ret = np.full(len(c), np.nan)
    prev = np.where(c[:-1] > EPS, c[:-1], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret[1:] = c[1:] / prev - 1
    with np.errstate(divide="ignore", invalid="ignore"):
        illiq = np.abs(ret) / np.where(amt > EPS, amt, np.nan)
    return _sanitize(fo.ts_mean(illiq, int(n)))


def realized_skew(c, n):
    """已实现偏度（分钟收益三阶标准化矩）。

    窗口语义：过去 n 根收益（含当前），收益由 c 自算（首根 NaN）；
    窗内零波动 -> NaN。复用 factor_ops.realized_skew。
    """
    c = _to_arr(c)
    ret = np.full(len(c), np.nan)
    prev = np.where(c[:-1] > EPS, c[:-1], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret[1:] = c[1:] / prev - 1
    ctx = {"ret": ret, "n": len(c)}
    return _sanitize(fo.realized_skew(ctx, n=int(n)))


# ══════════════════════════════════════════════════════════════════════════
# 组④ 截面（横截面算子，非时序；调用方传同刻面板）
# ══════════════════════════════════════════════════════════════════════════
def cs_rank(x):
    """横截面分位（平均秩 / 有效样本数 ∈ (0,1]，并列取平均秩）。

    输入约定：
    - 1D array：同一时刻多股票的一行横截面 -> 逐元素分位；
    - 2D array（T × S）：逐行秩化（每行 = 一个时刻的横截面），返回同形 2D。
    NaN 保持 NaN，分位只在当行有限值内计算。本算子无「窗口」概念，
    不参与前视 shift 测试（时序不变性对它无意义）。**新增**。
    """
    a = np.asarray(x, dtype=float)
    if a.ndim == 1:
        return _sanitize(_cs_rank_1d(a))
    if a.ndim == 2:
        out = np.full_like(a, np.nan)
        for i in range(a.shape[0]):
            out[i] = _cs_rank_1d(a[i])
        return _sanitize(out)
    raise ValueError("cs_rank 只接受 1D 或 2D 输入")


def _cs_rank_1d(row):
    """单行平均秩分位：rank ∈ 1..m（m=有限值数），并列取平均秩，输出 rank/m。"""
    row = np.asarray(row, dtype=float)
    out = np.full(len(row), np.nan)
    mask = np.isfinite(row)
    m = int(mask.sum())
    if m == 0:
        return out
    vals = row[mask]
    order = np.argsort(vals, kind="mergesort")  # 稳定排序，便于并列分组
    sv = vals[order]
    ranks = np.empty(m, dtype=float)
    i = 0
    while i < m:
        j = i + 1
        while j < m and sv[j] == sv[i]:
            j += 1
        ranks[i:j] = (i + 1 + j) / 2.0  # 平均秩（1-based）
        i = j
    out[np.flatnonzero(mask)[order]] = ranks / m
    return out


# ══════════════════════════════════════════════════════════════════════════
# gplearn function set 兼容层（窗口烘焙成闭包，返回 (name, func, arity)）
# ══════════════════════════════════════════════════════════════════════════
DEFAULT_WINDOWS = (5, 10, 20, 30, 60)


def build_function_set(windows=DEFAULT_WINDOWS):
    """返回 [(name, func, arity), ...]，供 GP 层 make_function 包装。

    窗口参数在此烘焙：每个窗口化算子对每个 w ∈ windows 生成一个闭包，
    名字带后缀（如 ts_mean_w10）。无窗口算子（cs_rank/open_ret/tail30_ret
    /vwap_dev/day_vwap）只注册一份。
    """
    fns = []

    def _add(name, func, arity):
        fns.append((name, func, arity))

    for w in windows:
        w = int(w)
        _add(f"ts_mean_w{w}", (lambda _w: lambda x: ts_mean(x, _w))(w), 1)
        _add(f"ts_std_w{w}", (lambda _w: lambda x: ts_std(x, _w))(w), 1)
        _add(f"ts_max_w{w}", (lambda _w: lambda x: ts_max(x, _w))(w), 1)
        _add(f"ts_min_w{w}", (lambda _w: lambda x: ts_min(x, _w))(w), 1)
        _add(f"ts_rank_w{w}", (lambda _w: lambda x: ts_rank(x, _w))(w), 1)
        _add(f"ts_delta_w{w}", (lambda _w: lambda x: ts_delta(x, _w))(w), 1)
        _add(f"ts_delay_w{w}", (lambda _w: lambda x: ts_delay(x, _w))(w), 1)
        _add(f"ts_argmax_w{w}", (lambda _w: lambda x: ts_argmax(x, _w))(w), 1)
        _add(f"decay_linear_w{w}", (lambda _w: lambda x: decay_linear(x, _w))(w), 1)
        _add(f"ts_corr_w{w}", (lambda _w: lambda x, y: ts_corr(x, y, _w))(w), 2)
        _add(f"ts_cov_w{w}", (lambda _w: lambda x, y: ts_cov(x, y, _w))(w), 2)
        _add(f"amount_ratio_w{w}", (lambda _w: lambda x: amount_ratio(x, _w))(w), 1)
        _add(f"amihud_w{w}", (lambda _w: lambda x, y: amihud(x, y, _w))(w), 2)
        _add(f"realized_skew_w{w}", (lambda _w: lambda x: realized_skew(x, _w))(w), 1)
        _add(f"smart_money_dev_w{w}",
             (lambda _w: lambda x, y: smart_money_dev(x, y, _w))(w), 2)
    _add("vwap_dev", vwap_dev, 3)
    _add("day_vwap", day_vwap, 2)
    _add("open_ret", open_ret, 2)
    _add("tail30_ret", tail30_ret, 1)
    _add("cs_rank", cs_rank, 1)
    return fns


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    fns = build_function_set()
    print(f"GP 算子集：{len(fns)} 个算子（窗口 {DEFAULT_WINDOWS} 展开）")
    for name, _f, arity in fns:
        print(f"  {name:<24s} arity={arity}")
