# -*- coding: utf-8 -*-
"""
因子挖掘管线 · IC 评估层（快速初筛层）
=====================================

定位：因子挖掘管线的第一层快速初筛。输入"收盘后可得"的因子长表，
输出按日截面 RankIC / 分组收益 / 蒙特卡洛随机基线，供下游因子包
（GP 臂、LLM 臂、人工因子）统一调用。

纪律（不可妥协）：
- 无未来函数：t 日因子值只配 t+1 起的前瞻收益（buy_open 口径为
  open(t+1+h)/open(t+1)-1，即 T+1 开盘买、持有 h 日后开盘卖）。
- 预注册口径：buy_open（可执行口径）为主，close 口径（close(t+h)/open(t+1)-1）
  为参考，两口径都输出。
- 退市股保留：panel 含退市股属刻意 PIT 设计，不做幸存者过滤。
- 蒙特卡洛随机基线：任何 IC 必须打过零假设分布 95% 分位才算过闸。
- 依赖仅限 pandas / numpy（禁 scipy）；读 parquet 需 pyarrow/fastparquet，
  仅 load_panel 用到。

性能：核心路径（前瞻收益 shift、组内秩、按日 pearson、MC shuffle）
全部基于「面板按 symbol 排序则 symbol 连续、合并后按 date 排序则 date 连续」
的 numpy 连续组向量化实现，9.7M 行全面板单因子三周期评估约 30s 量级。

API 契约（下游两个因子包按此开发）：
    load_panel(panel_dir) -> pd.DataFrame
    evaluate_factor(factor, panel=None, horizons=(1,3,5), min_coverage=30) -> dict
    decile_analysis(factor, panel, horizon=1, n_groups=10) -> dict
    mc_baseline(factor, panel, horizon=1, n=200, seed=42) -> dict

作者：因子挖掘管线任务2（P1-2）  日期：2026-09-18
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_PANEL_DIR = "t_io/validation/xsection/panel"
PANEL_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]


# ---------------------------------------------------------------------------
# numpy 连续组原语（组在数组中必须连续，由排序保证）
# ---------------------------------------------------------------------------

def _shift_contig(arr: np.ndarray, gid: np.ndarray, k: int) -> np.ndarray:
    """组内 shift（等价 pandas groupby.shift(k)），要求组连续。k<0 为前瞻。"""
    out = np.full(len(arr), np.nan)
    if k > 0:
        same = gid[k:] == gid[:-k]
        out[k:][same] = arr[:-k][same]
    elif k < 0:
        kk = -k
        same = gid[:-kk] == gid[kk:]
        out[:-kk][same] = arr[kk:][same]
    else:
        out[:] = arr
    return out


def _group_order(vals: np.ndarray, gid: np.ndarray) -> np.ndarray:
    """返回按 (gid, vals) 排序的 order，要求组连续。

    快路径：复合键 key = gid + frac(vals)（frac∈[0,1) 保序映射）单键 argsort，
    比 lexsort 快约 8 倍（19s -> 2.4s / 9.7M 行）。frac 理论上可能因 float64
    精度损失把两个极近值排反，因此排序后做 O(n) 验证；发现违例（实际几乎
    不会发生）自动回退精确 lexsort —— 正确性不妥协。
    """
    vmin = np.nanmin(vals)
    vmax = np.nanmax(vals)
    use_fast = np.isfinite(vmin) and vmax > vmin
    if use_fast:
        frac = (vals - vmin) / (vmax - vmin)
        frac = np.clip(frac * (1.0 - 2.0 ** -40), 0.0, 1.0 - 2.0 ** -40)
        key = gid.astype(np.float64) + frac
        order = np.argsort(key, kind="quicksort")
        sv = vals[order]
        sg = gid[order]
        same_g = sg[1:] == sg[:-1]
        # 组内必须非降（NaN 比较为 False，会触发回退，也正确）
        if np.all(sv[1:][same_g] >= sv[:-1][same_g]):
            return order
    return np.lexsort((vals, gid))  # 精确回退


def _avg_rank_contig(vals: np.ndarray, gid: np.ndarray) -> np.ndarray:
    """组内平均秩（等价 pandas groupby.rank(method='average')，正确处理 ties），
    要求组连续。返回每行在其 gid 组内的 1 基秩（ties 取平均秩）。"""
    order = _group_order(vals, gid)
    svals = vals[order]
    sgid = gid[order]
    change = np.ones(len(vals), dtype=bool)
    change[1:] = (sgid[1:] != sgid[:-1]) | (svals[1:] != svals[:-1])
    starts = np.flatnonzero(change)
    ends = np.r_[starts[1:], len(vals)]
    # 块 [s,e) 的组内 1 基平均秩 = (s+e+1)/2 - 该组首个块的起始位置
    block_gid = sgid[starts]
    first = np.r_[True, block_gid[1:] != block_gid[:-1]]
    base = np.repeat(starts[first],
                     np.diff(np.r_[np.flatnonzero(first), len(starts)]))
    avg = (starts + ends + 1) / 2.0 - base
    ranks = np.empty(len(vals), dtype=float)
    ranks[order] = np.repeat(avg, ends - starts)
    return ranks


def _bincount_ic(rf: np.ndarray, rr: np.ndarray, gid: np.ndarray,
                 n_days: int) -> np.ndarray:
    """向量化按日 pearson(rf, rr)，rf/rr 均为组内秩。返回长度 n_days 的 IC 数组。"""
    n = np.bincount(gid, minlength=n_days).astype(float)
    sx = np.bincount(gid, weights=rf, minlength=n_days)
    sy = np.bincount(gid, weights=rr, minlength=n_days)
    sxx = np.bincount(gid, weights=rf * rf, minlength=n_days)
    syy = np.bincount(gid, weights=rr * rr, minlength=n_days)
    sxy = np.bincount(gid, weights=rf * rr, minlength=n_days)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy - sx * sy / n
        vx = sxx - sx * sx / n
        vy = syy - sy * sy / n
        denom = np.sqrt(vx * vy)
        ic = np.where(denom > 0, cov / np.where(denom == 0, 1.0, denom), np.nan)
    return ic


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _norm_symbol(s: pd.Series) -> pd.Series:
    """统一 symbol 为纯代码字符串：'SHSE.600519' / '600519.SH' -> '600519'。"""
    s = s.astype(str)
    parts = s.str.split(".")
    return parts.apply(lambda p: p[1] if len(p) == 2 and p[1].isdigit() else p[0])


def _rank_ic_stats(ic: pd.Series, coverage: pd.Series) -> dict:
    """由按日 IC 序列汇总统计量。win_rate = 正 IC 占比（符号一致性）。

    注：契约注释中写「|IC|>0 占比」，字面意义恒≈1、无信息量，
    此处按预注册口径实现为 IC>0 的占比（做多方向胜率），并在报告中声明。
    """
    ic = ic.dropna()
    n = int(len(ic))
    mean = float(ic.mean()) if n else float("nan")
    std = float(ic.std(ddof=1)) if n > 1 else float("nan")
    icir = mean / std if std and np.isfinite(std) and std > 0 else float("nan")
    win = float((ic > 0).mean()) if n else float("nan")
    cov = float(coverage.mean()) if len(coverage) else float("nan")
    return {
        "rank_ic_mean": mean,
        "rank_ic_std": std,
        "icir": icir,
        "win_rate": win,
        "n_days": n,
        "ic_series": ic,
        "coverage_mean": cov,
    }


# ---------------------------------------------------------------------------
# 1. 面板加载
# ---------------------------------------------------------------------------

def load_panel(panel_dir: str = DEFAULT_PANEL_DIR) -> pd.DataFrame:
    """读取全部 parquet 分片，返回长表。

    返回 columns=[symbol, date, open, high, low, close, volume, amount]；
    date 为 pd.Timestamp（由 eob 解析为本地日期，去时区）；
    symbol 为纯代码字符串（600519 形态）；按 (symbol, date) 排序。
    退市股保留（刻意的 PIT 设计）。
    """
    root = Path(panel_dir)
    shard_dir = root / "shards"
    files = sorted(shard_dir.glob("*.parquet")) if shard_dir.is_dir() else []
    files += sorted(p for p in root.glob("*.parquet") if p.name != "universe.parquet")
    if not files:
        raise FileNotFoundError(f"未找到面板分片：{root}（含 shards/ 子目录）")
    frames = [pd.read_parquet(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    # 9.7M 行逐行 str 处理太慢：先 factorize，只归一化 ~5674 个唯一值再映射回去
    codes, uniques = pd.factorize(df["symbol"], sort=False)
    normed = _norm_symbol(pd.Series(uniques)).to_numpy()
    df["symbol"] = normed[codes]
    eob = df["eob"]
    if getattr(eob.dtype, "tz", None) is not None:
        eob = eob.dt.tz_localize(None)
    df["date"] = pd.to_datetime(eob).dt.normalize()
    df = df[PANEL_COLS].drop_duplicates(["symbol", "date"])
    df = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 2. 前瞻收益（无未来函数：t 日因子只配 t+1 起的收益）
# ---------------------------------------------------------------------------

def _forward_returns(panel: pd.DataFrame, horizons: tuple) -> pd.DataFrame:
    """对全面板预计算前瞻收益（连续组 numpy shift，O(n)）。

    buy_open_h = open(t+1+h)/open(t+1)-1  （T+1 开盘买，持有 h 日后开盘卖）
    close_h    = close(t+h)/open(t+1)-1   （T+1 开盘买，h 日后收盘卖）
    返回 columns=[symbol, date, buy_open_h..., close_h...]。
    """
    p = panel[["symbol", "date", "open", "close"]]
    if not p.empty:
        # load_panel 输出已按 (symbol, date) 排序；防御性再排一次保证组连续
        p = p.sort_values(["symbol", "date"], kind="mergesort")
    sid = pd.factorize(p["symbol"], sort=False)[0]
    opn = p["open"].to_numpy(float)
    cls = p["close"].to_numpy(float)
    open_t1 = _shift_contig(opn, sid, -1)
    out = p[["symbol", "date"]].copy()
    for h in horizons:
        out[f"buy_open_{h}"] = _shift_contig(opn, sid, -(1 + h)) / open_t1 - 1.0
        out[f"close_{h}"] = _shift_contig(cls, sid, -h) / open_t1 - 1.0
    return out


def _prepare_factor(factor: pd.DataFrame) -> pd.DataFrame:
    f = factor[["date", "symbol", "value"]].copy()
    codes, uniques = pd.factorize(f["symbol"], sort=False)
    f["symbol"] = _norm_symbol(pd.Series(uniques)).to_numpy()[codes]
    f["date"] = pd.to_datetime(f["date"]).dt.normalize()
    f["value"] = pd.to_numeric(f["value"], errors="coerce")
    return f.dropna(subset=["value"]).drop_duplicates(["date", "symbol"], keep="last")


def _merge_eval_frame(factor: pd.DataFrame, panel: pd.DataFrame,
                      horizons: tuple) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """合并因子与前瞻收益，按 (date, symbol) 排序使 date 组连续。

    返回 (m, gid, date_uniques)。merge 用 int32 股票代码键（比字符串键快数倍）。
    """
    f = _prepare_factor(factor)
    rets = _forward_returns(panel, tuple(sorted(set(horizons))))
    # int 代码映射：panel 侧 factorize 一次，factor 侧 dict map
    sid, suniq = pd.factorize(rets["symbol"], sort=False)
    rets["_sid"] = sid.astype(np.int32)
    mapper = dict(zip(suniq, range(len(suniq))))
    f["_sid"] = f["symbol"].map(mapper)
    f = f.dropna(subset=["_sid"])  # 面板外的股票直接丢弃
    f["_sid"] = f["_sid"].astype(np.int32)
    m = f.drop(columns="symbol").merge(
        rets.drop(columns="symbol"), on=["_sid", "date"], how="inner")
    m = m.sort_values(["date", "_sid"], kind="mergesort").reset_index(drop=True)
    gid, uniques = pd.factorize(m["date"], sort=True)
    return m, gid.astype(np.int64), uniques


def _eval_one_col(m: pd.DataFrame, gid: np.ndarray, uniques: np.ndarray,
                  rf0: np.ndarray, rcol: str, min_coverage: int) -> dict:
    """对单个前瞻收益列算按日 RankIC 统计（numpy 连续组路径）。"""
    v = m[rcol].to_numpy(float)
    valid = ~np.isnan(v)
    idx = np.flatnonzero(valid)
    gid_v = gid[idx]
    n_days = len(uniques)
    cnt = np.bincount(gid_v, minlength=n_days)
    rr = _avg_rank_contig(v[idx], gid_v)
    ic_all = _bincount_ic(rf0[idx], rr, gid_v, n_days)
    mask = cnt >= min_coverage
    ic = pd.Series(ic_all[mask], index=uniques[mask])
    ic[~np.isfinite(ic)] = np.nan
    coverage = pd.Series(cnt[mask].astype(float), index=uniques[mask])
    return _rank_ic_stats(ic, coverage)


# ---------------------------------------------------------------------------
# 3. evaluate_factor —— 按日截面 RankIC
# ---------------------------------------------------------------------------

def evaluate_factor(factor: pd.DataFrame, panel: pd.DataFrame | None = None,
                    horizons: tuple = (1, 3, 5), min_coverage: int = 30) -> dict:
    """按日截面 Spearman RankIC 评估（两前瞻口径都算）。

    factor: 长表 columns=[date, symbol, value]，value 为 t 日收盘后可得
            （调用方保证无未来函数；本层只把 t 日值配 t+1 起的收益）。
    返回 {h: {"buy_open": stats, "close": stats, **buy_open_stats 顶层展开}}
        stats = {"rank_ic_mean","rank_ic_std","icir","win_rate"(IC>0 占比),
                 "n_days","ic_series"(pd.Series 按日),"coverage_mean"}
    """
    if panel is None:
        panel = load_panel()
    hs = tuple(sorted(set(horizons)))
    m, gid, uniques = _merge_eval_frame(factor, panel, hs)
    # 因子秩与收益列无关，只算一次（此前逐列重算是主要性能瓶颈）
    rf0 = _avg_rank_contig(m["value"].to_numpy(float), gid)
    result = {}
    for h in hs:
        bo = _eval_one_col(m, gid, uniques, rf0, f"buy_open_{h}", min_coverage)
        cl = _eval_one_col(m, gid, uniques, rf0, f"close_{h}", min_coverage)
        entry = {"buy_open": bo, "close": cl}
        entry.update(bo)  # 顶层展开 = buy_open 主口径
        result[h] = entry
    return result


# ---------------------------------------------------------------------------
# 4. decile_analysis —— 分组收益与单调性
# ---------------------------------------------------------------------------

def decile_analysis(factor: pd.DataFrame, panel: pd.DataFrame | None = None,
                    horizon: int = 1, n_groups: int = 10) -> dict:
    """按日按因子值分 n_groups 组（组号 1..n_groups，值大者组号大），
    算各组 h 日前瞻收益（buy_open 口径）均值序列。

    返回 {"group_ret": DataFrame(索引=date, 列=组号),
          "long_short": 顶组-底组净值序列 (1+r).cumprod(),
          "monotonicity": 组序(1..n)与组均收益的 Spearman 单调性系数}
    """
    if panel is None:
        panel = load_panel()
    m, gid, uniques = _merge_eval_frame(factor, panel, (horizon,))
    rcol = f"buy_open_{horizon}"
    v = m[rcol].to_numpy(float)
    valid = ~np.isnan(v)
    idx = np.flatnonzero(valid)
    if len(idx) == 0:
        raise ValueError("factor 与 panel 无交集，无法分组")
    gid_v = gid[idx]
    ret = v[idx]
    n_days = len(uniques)
    cnt = np.bincount(gid_v, minlength=n_days).astype(float)
    rf = _avg_rank_contig(m["value"].to_numpy(float)[idx], gid_v)
    pct = (rf - 0.5) / cnt[gid_v]  # 组内百分位 ∈(0,1)
    grp = np.minimum((pct * n_groups).astype(np.int64), n_groups - 1)
    key = gid_v * n_groups + grp
    cs = np.bincount(key, minlength=n_days * n_groups).astype(float)
    ss = np.bincount(key, weights=ret, minlength=n_days * n_groups)
    with np.errstate(invalid="ignore", divide="ignore"):
        mat = np.where(cs > 0, ss / np.where(cs == 0, 1.0, cs), np.nan)
    group_ret = pd.DataFrame(mat.reshape(n_days, n_groups),
                             index=uniques,
                             columns=list(range(1, n_groups + 1)))
    ls_ret = group_ret[n_groups] - group_ret[1]
    long_short = (1.0 + ls_ret.fillna(0.0)).cumprod()
    means = group_ret.mean().to_numpy(float)
    ok = ~np.isnan(means)
    if ok.sum() >= 3:
        # Spearman = pearson(秩(组均收益), 组序)，手写秩避免 scipy 依赖
        r = _avg_rank_contig(means[ok], np.zeros(int(ok.sum()), dtype=np.int64))
        x = group_ret.columns.to_numpy(float)[ok]
        r_c = r - r.mean()
        x_c = x - x.mean()
        denom = float(np.sqrt((r_c ** 2).sum() * (x_c ** 2).sum()))
        mono = float((r_c * x_c).sum() / denom) if denom > 0 else float("nan")
    else:
        mono = float("nan")
    return {"group_ret": group_ret, "long_short": long_short,
            "monotonicity": mono}


# ---------------------------------------------------------------------------
# 5. mc_baseline —— 蒙特卡洛随机基线
# ---------------------------------------------------------------------------

def mc_baseline(factor: pd.DataFrame, panel: pd.DataFrame | None = None,
                horizon: int = 1, n: int = 200, seed: int = 42) -> dict:
    """蒙特卡洛随机基线：保持 (date, symbol) 结构不变，n 次在每日截面内
    shuffle 因子值，重算 rank_ic_mean（buy_open 口径），得零假设分布。

    关键恒等式：组内 shuffle value 后重排名 == 组内 shuffle 秩，
    因此只需预排名一次，每次迭代 shuffle 秩即可（性能优化）。

    返回 {"null_mean","null_std","mc_rank"(真实 IC 在零假设分布中的分位),
          "pass": 真实IC > null 95% 分位}
    注：pass 为做多方向单侧闸门；做空型因子请先对 value 取负。
    """
    if panel is None:
        panel = load_panel()
    real = evaluate_factor(factor, panel, horizons=(horizon,))
    real_ic = real[horizon]["buy_open"]["rank_ic_mean"]

    m, gid, uniques = _merge_eval_frame(factor, panel, (horizon,))
    v = m[f"buy_open_{horizon}"].to_numpy(float)
    valid = ~np.isnan(v)
    idx = np.flatnonzero(valid)
    gid_v = gid[idx]
    n_days = len(uniques)
    cnt = np.bincount(gid_v, minlength=n_days)
    day_mask = cnt >= 30  # 覆盖率口径与 evaluate_factor 默认对齐
    rr = _avg_rank_contig(v[idx], gid_v)
    rf0 = _avg_rank_contig(m["value"].to_numpy(float), gid)[idx]

    rng = np.random.default_rng(seed)
    null = np.empty(n, dtype=float)
    n_rows = len(idx)
    gid_f = gid_v.astype(np.float64)
    for i in range(n):
        # 复合键单键 argsort（组内随机序，保持日期-股票结构）；
        # 随机键碰撞概率可忽略，无需 lexsort（~19s -> ~2.4s / 9.7M 行）
        order = np.argsort(gid_f + rng.random(n_rows), kind="quicksort")
        ics = _bincount_ic(rf0[order], rr, gid_v, n_days)
        null[i] = np.nanmean(ics[day_mask])
    null_mean = float(np.mean(null))
    null_std = float(np.std(null, ddof=1)) if n > 1 else float("nan")
    mc_rank = float((null < real_ic).mean())
    q95 = float(np.percentile(null, 95))
    return {"null_mean": null_mean, "null_std": null_std,
            "mc_rank": mc_rank, "pass": bool(real_ic > q95)}


# ---------------------------------------------------------------------------
# __main__ 自测 + 性能实测
# ---------------------------------------------------------------------------

def _make_synthetic(n_symbols: int = 200, n_days: int = 500,
                    ic_target: float = 0.05, seed: int = 7):
    """合成面板：预埋一个已知 IC≈ic_target 的因子 + 一个纯噪声因子。

    构造：open-to-open 收益 r(d->d+1) = b * z(d-1) + eps，eps~N(0,sigma^2)，
    z 为 d-1 日截面因子，则 corr(z, r) = b/sqrt(b^2+sigma^2)。
    """
    rng = np.random.default_rng(seed)
    sigma = 0.02  # 日 open-to-open 波动 2%，贴近真实 A 股量级
    b = ic_target * sigma / np.sqrt(1.0 - ic_target ** 2)
    syms = [f"{600000 + i}" for i in range(n_symbols)]
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    z = rng.standard_normal((n_days, n_symbols))
    # open-to-open 收益：r[d] 为 d-1 -> d 的收益，依赖 z[d-2]（d-2 日收盘后因子）
    r = sigma * rng.standard_normal((n_days, n_symbols))
    r[2:] += b * z[:-2]
    r[2:] -= b * z[:-2].mean(axis=1, keepdims=True)  # 去截面均值，防整体漂移
    open_px = 10.0 * np.exp(np.cumsum(r, axis=0))
    close_px = open_px * (1.0 + rng.standard_normal((n_days, n_symbols)) * 0.005)
    high = np.maximum(open_px, close_px) * 1.001
    low = np.minimum(open_px, close_px) * 0.999
    rows = []
    for j, s in enumerate(syms):
        rows.append(pd.DataFrame({
            "symbol": s, "date": dates,
            "open": open_px[:, j], "high": high[:, j], "low": low[:, j],
            "close": close_px[:, j],
            "volume": rng.integers(1e5, 1e7, n_days),
            "amount": open_px[:, j] * 1e6,
        }))
    panel = pd.concat(rows, ignore_index=True)
    fz = []
    for j, s in enumerate(syms):
        fz.append(pd.DataFrame({"date": dates, "symbol": s, "value": z[:, j]}))
    known_factor = pd.concat(fz, ignore_index=True)
    noise_factor = known_factor.copy()
    noise_factor["value"] = np.random.default_rng(20260918).standard_normal(
        len(noise_factor))
    return panel, known_factor, noise_factor, ic_target


def _self_test():
    print("== 合成数据自测（200 只 × 500 日）==")
    panel, known, noise, ic_target = _make_synthetic()
    ev = evaluate_factor(known, panel, horizons=(1, 3))
    est = ev[1]["buy_open"]["rank_ic_mean"]
    err = abs(est - ic_target) / ic_target
    print(f"已知因子 h=1 RankIC 回收: {est:.4f} (目标 {ic_target}, 相对误差 {err:.1%})")
    assert err < 0.20, f"IC 回收误差 {err:.1%} >= 20%"
    print(f"  h=3 RankIC: {ev[3]['buy_open']['rank_ic_mean']:.4f}, "
          f"close 口径 h=1: {ev[1]['close']['rank_ic_mean']:.4f}, "
          f"n_days={ev[1]['buy_open']['n_days']}")

    ev_noise = evaluate_factor(noise, panel, horizons=(1,))
    mc_noise = mc_baseline(noise, panel, horizon=1, n=100, seed=42)
    print(f"噪声因子 mc_baseline: real_ic={ev_noise[1]['buy_open']['rank_ic_mean']:.4f}"
          f" null={mc_noise['null_mean']:.4f}±{mc_noise['null_std']:.4f} "
          f"mc_rank={mc_noise['mc_rank']:.2f} pass={mc_noise['pass']}")
    assert not mc_noise["pass"], "纯噪声因子不应过 MC 闸"
    mc_known = mc_baseline(known, panel, horizon=1, n=100, seed=42)
    print(f"已知因子 mc_baseline: null={mc_known['null_mean']:.4f}±"
          f"{mc_known['null_std']:.4f} mc_rank={mc_known['mc_rank']:.2f} "
          f"pass={mc_known['pass']}")
    assert mc_known["pass"], "预埋因子应过 MC 闸"

    dec = decile_analysis(known, panel, horizon=1, n_groups=10)
    print(f"decile 单调性系数: {dec['monotonicity']:.3f} "
          f"(顶组-底组净值终值 {dec['long_short'].iloc[-1]:.3f})")
    assert dec["monotonicity"] > 0.5, "预埋因子分组单调性方向应为正"
    print("== 自测全部通过 ==")


def _perf_test():
    print("== 全面板性能实测（5674 只 × ~2000 日）==")
    t0 = time.perf_counter()
    panel = load_panel()
    t1 = time.perf_counter()
    print(f"load_panel: {t1 - t0:.1f}s, rows={len(panel):,}, "
          f"symbols={panel['symbol'].nunique()}, "
          f"date=[{panel['date'].min().date()}..{panel['date'].max().date()}]")
    # 用面板自造一个 5 日反转因子做 evaluate 计时（非研究结论，仅压测）
    sid = pd.factorize(panel["symbol"], sort=False)[0]
    mom = panel["close"].to_numpy(float) / _shift_contig(
        panel["close"].to_numpy(float), sid, 5) - 1.0
    factor = pd.DataFrame({"date": panel["date"].values,
                           "symbol": panel["symbol"].values,
                           "value": -mom}).dropna()
    t2 = time.perf_counter()
    ev = evaluate_factor(factor, panel, horizons=(1, 3, 5))
    t3 = time.perf_counter()
    print(f"evaluate_factor(3 horizons): {t3 - t2:.1f}s, "
          f"h=1 RankIC={ev[1]['buy_open']['rank_ic_mean']:.4f} "
          f"n_days={ev[1]['buy_open']['n_days']}")
    t4 = time.perf_counter()
    mc = mc_baseline(factor, panel, horizon=1, n=50)
    t5 = time.perf_counter()
    print(f"mc_baseline(n=50): {t5 - t4:.1f}s pass={mc['pass']} "
          f"mc_rank={mc['mc_rank']:.2f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    _self_test()
    if "--perf" in sys.argv:
        _perf_test()
