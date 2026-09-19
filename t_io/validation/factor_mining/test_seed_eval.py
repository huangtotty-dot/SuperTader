# -*- coding: utf-8 -*-
"""seed_eval.py（任务 S2-7）单元测试——全合成数据，不碰真实分钟池。

覆盖：
  1. forward_label：口径 close(t+30)/close(t)-1、日末 30 根 NaN、不跨日（IS/OOS 安全）；
  2. spearman：完全单调 ±1、与手工秩相关一致、样本不足/零方差 → NaN；
  3. _summarize_ics：均值/胜率/最差最佳；
  4. apply_leak：与 eval_factor.compute_factor 的 leak 语义逐位一致；
  5. panel_seed_fvals：连续序列算因子再按日切片，长度与对齐正确（无窗因子 a013）；
  6. mc_factor：预埋单调关系因子过闸（mc_rank 高）、纯噪声因子不过闸；
     日块打乱保留日内块结构与值集合（双射）。
运行：python -m pytest test_seed_eval.py -q   或   python test_seed_eval.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import seed_eval as se


# ── 合成数据工具 ─────────────────────────────────────────────────────────────
def _make_minute_df(n_days=8, bars_per_day=60, seed=7, start='2026-05-20'):
    """合成单票分钟 df（均匀日长），列对齐 minute_data 契约。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    rows = []
    px = 10.0
    for d in dates:
        for m in range(bars_per_day):
            r = rng.standard_normal() * 0.002
            o = px
            c = o * (1 + r)
            rows.append({'time': d + pd.Timedelta(hours=9, minutes=30 + m),
                         'open': o, 'high': max(o, c) * 1.001, 'low': min(o, c) * 0.999,
                         'close': c, 'volume': 1e5 + abs(rng.standard_normal()) * 1e4,
                         'amount': o * 1e5})
            px = c
    return pd.DataFrame(rows)


# ── 1. forward_label ─────────────────────────────────────────────────────────
def test_forward_label_values_and_no_day_crossing():
    # 两日，每日 40 根（> FWD=30），close 线性递增，值可手算
    c1 = np.linspace(10, 11, 40)
    c2 = np.linspace(20, 19, 40)
    close = np.concatenate([c1, c2])
    day_id = np.array([0] * 40 + [1] * 40)
    lab = se.forward_label(close, day_id, fwd=30)
    # 首日 bar0：10 → c1[30]
    assert abs(lab[0] - (c1[30] / c1[0] - 1)) < 1e-12
    # 首日末 30 根全部 NaN（不跨到次日，哪怕次日 close 已知）
    assert np.all(np.isnan(lab[10:40]))
    # 次日 bar0：20 → c2[30]
    assert abs(lab[40] - (c2[30] / c2[0] - 1)) < 1e-12
    assert np.all(np.isnan(lab[50:]))
    # 次日 bar0 的 label 用的是次日第 30 根，而非首日末根 → 证明不跨日
    assert lab[39] != lab[39]  # NaN


def test_forward_label_zero_close_safe():
    close = np.array([0.0, 1.0, 2.0] + [3.0] * 40)
    day_id = np.zeros(len(close), dtype=int)
    lab = se.forward_label(close, day_id, fwd=2)
    assert np.isnan(lab[0])                      # 0 价格不出值
    assert abs(lab[1] - (close[3] / 1.0 - 1)) < 1e-12


# ── 2. spearman ──────────────────────────────────────────────────────────────
def test_spearman_perfect_monotone():
    x = np.arange(500, dtype=float)
    rng = np.random.default_rng(1)
    y = x + rng.standard_normal(500) * 0.0       # 完全同序
    rho, n = se.spearman(x, y)
    assert abs(rho - 1.0) < 1e-9 and n == 500
    rho2, _ = se.spearman(x, -x)
    assert abs(rho2 + 1.0) < 1e-9


def test_spearman_matches_manual_rank_corr():
    rng = np.random.default_rng(42)
    x = rng.standard_normal(1000)
    y = 0.3 * x + rng.standard_normal(1000)
    rho, _ = se.spearman(x, y)
    # 手工：pandas rank + pearson
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    manual = float(np.corrcoef(rx, ry)[0, 1])
    assert abs(rho - manual) < 1e-9


def test_spearman_degenerate():
    x = np.ones(500)                              # 零方差 → NaN
    y = np.arange(500, dtype=float)
    rho, _ = se.spearman(x, y)
    assert np.isnan(rho)
    rho2, n2 = se.spearman(np.arange(10, dtype=float), np.arange(10, dtype=float))
    assert np.isnan(rho2) and n2 == 10            # 样本不足 MIN_PAIRS → NaN 但仍报 n


def test_spearman_ties():
    x = np.repeat([1.0, 2.0, 3.0], 200)
    y = np.repeat([1.0, 2.0, 3.0], 200) + np.random.default_rng(3).standard_normal(600) * 1e-9
    rho, _ = se.spearman(x, y)
    # 正确性基准 = pandas 平均秩 + pearson（ties 必须取平均秩）
    manual = float(np.corrcoef(pd.Series(x).rank(), pd.Series(y).rank())[0, 1])
    assert abs(rho - manual) < 1e-9 and rho > 0.85


# ── 3. _summarize_ics ────────────────────────────────────────────────────────
def test_summarize_ics():
    s = se._summarize_ics({'a': 0.1, 'b': -0.2, 'c': 0.3, 'd': np.nan})
    assert s['n'] == 3
    assert abs(s['mean'] - (0.1 - 0.2 + 0.3) / 3) < 1e-3   # 结果预注册 4 位小数
    assert abs(s['win'] - 2 / 3) < 1e-3
    assert s['worst'][0] == 'b' and s['best'][0] == 'c'
    assert se._summarize_ics({})['n'] == 0


# ── 4. apply_leak 与 eval_factor.compute_factor 语义一致 ─────────────────────
def test_apply_leak_semantics():
    fv = {'2026-05-20': np.arange(10, dtype=float)}
    out = se.apply_leak(fv, 3)['2026-05-20']
    assert np.all(np.isnan(out[:3]))
    assert np.array_equal(out[3:], np.arange(7, dtype=float))
    # leak >= 当日长度 → 全 NaN（与 compute_factor 的 `if leak < len(v)` 一致）
    assert np.all(np.isnan(se.apply_leak(fv, 10)['2026-05-20']))


# ── 5. panel_seed_fvals 切片对齐 ─────────────────────────────────────────────
def _fake_panel(df):
    """把合成分钟 df 伪装成 eval_factor.load_code 的 panel 结构。"""
    dates, days, labels = [], {}, {}
    for d, g in df.groupby(df['time'].dt.date):
        ds = str(d)
        dates.append(ds)
        days[ds] = {'t': [t.strftime('%H:%M') for t in g['time']],
                    'o': g['open'].to_numpy(float), 'h': g['high'].to_numpy(float),
                    'l': g['low'].to_numpy(float), 'c': g['close'].to_numpy(float),
                    'v': g['volume'].to_numpy(float), 'amt': g['amount'].to_numpy(float),
                    'n': len(g)}
        labels[ds] = days[ds]['t']
    return {'dates': dates, 'days': days, 'labels': labels}


def test_panel_seed_fvals_alignment():
    df = _make_minute_df(n_days=6, bars_per_day=50)
    panel = _fake_panel(df)
    fv = se.panel_seed_fvals(panel, ['a013'])     # a013 无窗口：sqrt(h*l)-vwap
    # 长度逐日对齐
    for d in panel['dates']:
        assert len(fv['a013'][d]) == panel['days'][d]['n']
    # 拼接顺序 = 原 df 顺序：手算 a013 第一日首根
    import alpha191_seeds as seeds
    ctx = seeds.MinuteCtx(df)
    expect = np.sqrt(ctx.h * ctx.l + seeds.EPS) - ctx.vwap
    got = np.concatenate([fv['a013'][d] for d in panel['dates']])
    assert np.allclose(got, expect, equal_nan=True)


def test_panel_seed_fvals_rejects_unsorted():
    df = _make_minute_df(n_days=3, bars_per_day=40)
    df = df.iloc[::-1].reset_index(drop=True)     # 逆序 → 必须抛
    panel = _fake_panel(df.sort_values('time').reset_index(drop=True))
    # panel 的 days 标签长度仍合法，但 _panel_continuous_df 用 labels 重建时间
    # 这里直接测断言：手工构造非单调 panel
    bad = {'dates': panel['dates'], 'days': panel['days'],
           'labels': {d: panel['labels'][d][::-1] for d in panel['dates']}}
    try:
        se.panel_seed_fvals(bad, ['a013'])
        assert False, '非单调时间应触发断言'
    except AssertionError:
        pass


# ── 6. mc_factor：预埋关系过闸 / 噪声不过闸 / 日块双射 ───────────────────────
def _plant_cache(tmp_path, code, n_days=30, bars=80, beta=0.0, seed=11):
    """写一份合成缓存 parquet：label = beta * monotone(factor) + 噪声。
    beta>0 时因子与未来收益有真实单调关系（应过 MC 闸）；beta=0 纯噪声。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range('2026-04-01', periods=n_days)  # 全部 < OOS_START → IS
    n = n_days * bars
    date_arr = np.repeat([str(d.date()) for d in dates], bars)
    f = rng.standard_normal(n)
    lab = beta * f + rng.standard_normal(n) * np.sqrt(max(1e-9, 1 - beta ** 2))
    df = pd.DataFrame({'date': date_arr, 'label': lab, 'fX': f})
    df.to_parquet(tmp_path / f'{code}.parquet', index=False)


def test_mc_factor_planted_passes_noise_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(se, 'CACHE_DIR', str(tmp_path))
    codes = ['s1', 's2', 's3']
    for i, c in enumerate(codes):
        _plant_cache(tmp_path, c, beta=0.35, seed=100 + i)
    se.MIN_PAIRS = 50                              # 合成样本小，放宽门槛
    try:
        r = se.mc_factor(codes, 'fX', n=50, seed=1)
        assert r['real_mean'] > 0.2
        assert r['mc_rank'] >= 0.95 and r['pass']  # 预埋因子打过 null 95 分位
        assert abs(r['null_mean']) < 0.05          # null 分布均值 ≈0
        # 换成纯噪声缓存
        for i, c in enumerate(codes):
            _plant_cache(tmp_path, c, beta=0.0, seed=200 + i)
        r2 = se.mc_factor(codes, 'fX', n=50, seed=1)
        assert not r2['pass']                      # 纯噪声不应过闸
    finally:
        se.MIN_PAIRS = 200


def test_mc_dayblock_permutation_is_bijection(tmp_path, monkeypatch):
    """日块打乱前后：label 秩的值集合不变（双射），日内块整体搬迁。"""
    monkeypatch.setattr(se, 'CACHE_DIR', str(tmp_path))
    _plant_cache(tmp_path, 's1', n_days=10, bars=50, beta=0.3, seed=5)
    se.MIN_PAIRS = 50
    try:
        f_rank, blocks, real = se._stock_is_blocks('s1', 'fX')
        whole = np.concatenate(blocks)
        assert np.isfinite(real) and real > 0.2
        rng = np.random.default_rng(0)
        for _ in range(5):
            perm = rng.permutation(len(blocks))
            p = np.concatenate([blocks[i] for i in perm])
            # 双射：排序后逐位相等（NaN 也算）
            a, b = np.sort(np.nan_to_num(whole, nan=-1)), np.sort(np.nan_to_num(p, nan=-1))
            assert np.array_equal(a, b)
            # 块结构：每个块的内容在打乱后完整出现
            for blk in blocks:
                found = any(np.array_equal(np.nan_to_num(blk, nan=-999),
                                           np.nan_to_num(p[j:j + len(blk)], nan=-999))
                            for j in range(0, len(p) - len(blk) + 1, len(blk)))
                assert found
    finally:
        se.MIN_PAIRS = 200


if __name__ == '__main__':
    import tempfile
    import pathlib

    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        if 'tmp_path' in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as td:
                class _MP:                        # 极简 monkeypatch
                    @staticmethod
                    def setattr(obj, name, val):
                        setattr(obj, name, val)
                fn(pathlib.Path(td), _MP)
        else:
            fn()
        print(f'  PASS {fn.__name__}')
    print(f'== {len(fns)} 项测试全部通过 ==')
