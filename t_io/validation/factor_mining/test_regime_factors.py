# -*- coding: utf-8 -*-
"""regime_factors 测试。纯 assert 脚本（仓库风格，无 pytest 依赖）：

  python test_regime_factors.py

覆盖：
1. ADX 手算对照（3 点 Wilder 递推小样例）+ 趋势/震荡合成数据的定性 sanity
2. BBW = 2k·σ/MA 的数值正确性；bbw_pct120 ∈ (0,1]
3. ma_bull_layers / range_days / trend_up_confirm / dist_high20 语义
4. **无未来函数**：全序列 vs 截断序列，重叠区间因子值必须逐位一致
5. regime 打标规则：构造五类合成行各命中目标标签
6. IC 层契约：内置桩（ic_layer 不存在时）驱动 factor_health_check 跑通
7. scenario_value 在玩具数据上的方向正确性

测试数据全部本地合成，不碰生产 t_io/state/。
"""
import os
import sys
import types

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import regime_factors as rf  # noqa: E402

PASS = []


def ok(name, cond):
    assert cond, f'FAIL: {name}'
    PASS.append(name)
    print(f'  ✓ {name}')


def mk_df(close, high=None, low=None, open_=None, symbol='T.001'):
    """合成单 symbol 面板行。"""
    n = len(close)
    close = np.asarray(close, float)
    high = np.asarray(high, float) if high is not None else close * 1.01
    low = np.asarray(low, float) if low is not None else close * 0.99
    open_ = np.asarray(open_, float) if open_ is not None else close
    return pd.DataFrame({'symbol': symbol, 'open': open_, 'high': high, 'low': low,
                         'close': close, 'volume': 1000, 'amount': 1e6,
                         'eob': pd.date_range('2025-01-01', periods=n, freq='B',
                                              tz='Asia/Shanghai')})


# ── 1. ADX ──────────────────────────────────────────────────────────────────
def test_adx():
    # 手算对照：3 根 bar，n=2（ewm alpha=0.5 adjust=False 的显式递推）
    h = [10.0, 11.0, 12.0]
    l = [9.0, 9.5, 10.5]
    c = [9.5, 10.5, 11.5]
    adx, pdi, mdi, atr = rf.wilder_adx(h, l, c, n=2)
    # TR: [1.0, max(1.5, 1.5, 0.5)=1.5, max(1.5,1.5,0.5)=1.5]
    tr = [1.0, 1.5, 1.5]
    sm0, sm1 = tr[0], 0.5 * tr[0] + 0.5 * tr[1]
    sm2 = 0.5 * sm1 + 0.5 * tr[2]
    assert abs(atr.iloc[2] - sm2) < 1e-9, 'Wilder ATR 递推'
    # 单调上涨：+DM 主导 → plus_di >> minus_di（应为 0）
    ok('ADX 单边上涨: +DI 主导且 -DI≈0', pdi.iloc[2] > 0 and mdi.iloc[2] < 1e-9)

    # 定性：趋势合成数据 ADX 高，锯齿震荡 ADX 低
    n = 300
    trend = 10 + np.cumsum(np.full(n, 0.05))            # 每天 +0.05 匀速
    chop = 10 + np.sin(np.arange(n) * 2 * np.pi / 6)    # 6 日周期正弦
    df_t = rf.compute_symbol_factors(mk_df(trend))
    df_c = rf.compute_symbol_factors(mk_df(chop, high=chop + 0.3, low=chop - 0.3))
    ok('ADX 趋势>震荡', df_t['adx14'].iloc[-1] > 40 > df_c['adx14'].iloc[-1])
    ok('ADX 非负', (df_t['adx14'].dropna() >= 0).all())


# ── 2. BBW ──────────────────────────────────────────────────────────────────
def test_bbw():
    rng = np.random.RandomState(7)
    c = 20 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    bbw, mid = rf.bollinger_bbw(c)
    cs = pd.Series(c)
    exp = 4 * cs.rolling(20).std(ddof=0) / cs.rolling(20).mean()
    ok('BBW=4σ/MA', np.allclose(bbw.dropna(), exp.dropna(), atol=1e-12))
    pct = rf.rolling_pct_rank(bbw, 120)
    ok('BBW 分位 ∈(0,1]', ((pct.dropna() > 0) & (pct.dropna() <= 1)).all())
    ok('BBW 分位前 138 行 NaN、第 139 行起有效', pct.iloc[:138].isna().all() and pct.iloc[138:].notna().all())


# ── 3. 其余因子语义 ─────────────────────────────────────────────────────────
def test_misc_factors():
    # ma_bull_layers：完全多头排列
    n = 60
    c = 10 + np.cumsum(np.full(n, 0.1))
    df = rf.compute_symbol_factors(mk_df(c))
    ok('多头排列满层', df['ma_bull_layers'].iloc[-1] == 3)
    # 完全空头
    c2 = 100 - np.cumsum(np.full(n, 0.1))
    df2 = rf.compute_symbol_factors(mk_df(c2))
    ok('空头排列 0 层', df2['ma_bull_layers'].iloc[-1] == 0)

    # range_days：手动造 ADX<20 段的长度语义（用常数序列直接测 streak_below）
    s = pd.Series([30, 10, 10, 25, 15, 10, 5, 30])
    st = rf.streak_below(s, 20)
    ok('range_days 连续计数', list(st) == [0, 1, 2, 0, 1, 2, 3, 0])

    # trend_up_confirm：穿越当根为 1，其余为 0
    adx = pd.Series([20, 24, 26, 28, 22])
    pdi = pd.Series([30, 30, 30, 30, 30])
    mdi = pd.Series([10, 10, 10, 10, 40])
    tuc = rf.trend_up_confirm(adx, pdi, mdi)
    ok('上穿确认事件', list(tuc) == [0, 0, 1, 0, 0])

    # dist_high20 ∈ [-1,0]：默认 mk_df 里 high=close*1.01，故收盘新高日 = 1/1.01-1
    d_last = df['dist_high20'].iloc[-1]
    ok('dist_high20 新高日=close/high-1', abs(d_last - (1 / 1.01 - 1)) < 1e-9)
    ok('dist_high20 ≤0 且有界', (df['dist_high20'].dropna() <= 0).all()
       and (df['dist_high20'].dropna() >= -1).all())


# ── 4. 无未来函数 ────────────────────────────────────────────────────────────
def test_no_lookahead():
    rng = np.random.RandomState(42)
    n = 400
    c = 30 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    h = c * (1 + np.abs(rng.normal(0, 0.01, n)))
    l = c * (1 - np.abs(rng.normal(0, 0.01, n)))
    full = rf.compute_symbol_factors(mk_df(c, h, l))
    cut = rf.compute_symbol_factors(mk_df(c[:250], h[:250], l[:250]))
    for col in rf.FACTOR_COLS + ['plus_di', 'minus_di', 'atr14']:
        a, b = full[col].iloc[:250], cut[col]
        both = a.notna() & b.notna()
        assert np.allclose(a[both], b[both], atol=1e-10), f'{col} 前视泄漏!'
    ok('全部因子: 截断不变性（无未来函数）', True)
    lab_full = rf.label_regimes(full)
    lab_cut = rf.label_regimes(cut)
    both = lab_full.iloc[:250].notna() & lab_cut.notna()
    ok('regime 标签: 截断不变性', (lab_full.iloc[:250][both] == lab_cut[both]).all())


# ── 5. regime 打标 ───────────────────────────────────────────────────────────
def _row(**kw):
    base = dict(n_hist=200, adx14=15.0, plus_di=20.0, minus_di=20.0,
                ma_bull_layers=1.0, close=10.0, ma20=10.0, bbw_pct120=0.5)
    base.update(kw)
    return base


def test_labels():
    rows = pd.DataFrame([
        _row(adx14=30, plus_di=30, minus_di=10, ma_bull_layers=3),      # 单边上涨
        _row(adx14=30, plus_di=10, minus_di=30, close=9.0, ma20=10.0),  # 单边下跌
        _row(adx14=15, bbw_pct120=0.10),                                # 震荡末期
        _row(adx14=15, bbw_pct120=0.60),                                # 震荡
        _row(adx14=22),                                                 # 混沌(过渡带)
        _row(adx14=30, plus_di=30, minus_di=10, ma_bull_layers=1),      # 混沌(ADX高但均线不配合)
        _row(n_hist=50),                                                # 历史不足 → NaN
    ])
    lab = rf.label_regimes(rows)
    exp = [rf.REGIME_UP, rf.REGIME_DOWN, rf.REGIME_SQUEEZE, rf.REGIME_RANGE,
           rf.REGIME_CHAOS, rf.REGIME_CHAOS, None]
    got = [None if pd.isna(v) else v for v in lab]
    ok(f'regime 五分类规则 {got}', got == exp)


# ── 6. IC 层契约（内置桩）───────────────────────────────────────────────────
def _install_ic_stub():
    """任务2 真实契约的桩：evaluate_factor/decile_analysis/mc_baseline。
    签名：(factor 长表[date,symbol,value], panel, ...)。仅测试用，不提交 ic_layer.py。"""
    mod = types.ModuleType('ic_layer')
    def evaluate_factor(factor, panel, horizons=(1, 3, 5), min_coverage=30):
        # 桩：对合成玩具面板，factor 的 value 与 panel 按 (date,symbol) 对齐后
        # 用 close/open-1 当日收益占位，只验证「契约形状」而非数值正确性
        m = factor.merge(panel[['symbol', 'date', 'open', 'close']],
                         on=['symbol', 'date'], how='inner')
        m = m[m['open'] > 0]
        m = m.assign(r=m['close'] / m['open'] - 1.0)
        res = {}
        for h in horizons:
            vals = {}
            for d, g in m.groupby('date'):
                if len(g) >= 5:
                    ic = rf._spearman(g['value'].to_numpy(), g['r'].to_numpy())
                    if np.isfinite(ic):
                        vals[d] = ic
            ics = pd.Series(vals, dtype=float)
            st = {'rank_ic_mean': float(ics.mean()), 'rank_ic_std': float(ics.std(ddof=1)),
                  'icir': float(ics.mean() / ics.std(ddof=1)),
                  'win_rate': float((ics > 0).mean()), 'n_days': int(len(ics))}
            res[h] = {'buy_open': dict(st), 'close': dict(st), **st}
        return res
    def decile_analysis(factor, panel, horizon=1, n_groups=10):
        m = factor.merge(panel[['symbol', 'date', 'open', 'close']],
                         on=['symbol', 'date'], how='inner')
        m = m.assign(r=m['close'] / m['open'] - 1.0)
        m['q'] = m.groupby('date')['value'].transform(
            lambda s: pd.qcut(s.rank(method='first'), n_groups, labels=False))
        gr = m.groupby(['date', 'q'])['r'].mean().unstack('q')
        means = gr.mean()
        mono = rf._spearman(means.to_numpy(), means.index.to_numpy(float))
        return {'group_ret': gr, 'long_short': (1 + gr.iloc[:, -1] - gr.iloc[:, 0]).cumprod(),
                'monotonicity': float(mono)}
    def mc_baseline(factor, panel, horizon=1, n=200, seed=42):
        return {'null_mean': 0.0, 'null_std': 0.01, 'mc_rank': 0.5, 'pass': False}
    mod.evaluate_factor = evaluate_factor
    mod.decile_analysis = decile_analysis
    mod.mc_baseline = mc_baseline
    sys.modules['ic_layer'] = mod
    return mod


def test_health_check_with_stub():
    _install_ic_stub()
    rng = np.random.RandomState(1)
    nd, ns = 30, 40
    dates = pd.date_range('2026-01-01', periods=nd)
    rows = []
    for d in dates:
        for s in range(ns):
            r = rng.normal(0, 0.02)
            rows.append({'eob': pd.Timestamp(d, tz='Asia/Shanghai'),
                         'symbol': f'T.{s:03d}',
                         'open': 10.0, 'high': 10.5, 'low': 9.5,
                         'close': 10 * (1 + r), 'volume': 1000, 'amount': 1e4,
                         'adx14': 20 + 10 * r, 'bbw': abs(r), 'bbw_pct120': rng.rand(),
                         'ma_bull_layers': float(rng.randint(0, 4)), 'ma_bull_cont': r,
                         'range_days': float(rng.randint(0, 20)),
                         'trend_up_confirm': float(rng.rand() < 0.05),
                         'dist_high20': -abs(r)})
    fp = pd.DataFrame(rows)
    res = rf.factor_health_check(fp, factors=['adx14', 'dist_high20'], mc_perm=5)
    ok('体检走 ic_layer 桩后端', res['ic_backend'] == 'ic_layer')
    ok('体检三件套齐全', all(k in res['factors']['adx14']
                           for k in ('evaluate_factor', 'decile', 'mc')))
    ic1 = res['factors']['adx14']['evaluate_factor'][1]['rank_ic_mean']
    ok('IC 数值有界', -1 <= ic1 <= 1)
    ok('decile 输出含单调性', 'monotonicity' in res['factors']['adx14']['decile'])
    del sys.modules['ic_layer']
    sys.modules['ic_layer'] = None          # 屏蔽真实 ic_layer（任务2 已落盘），强制走回退
    res2 = rf.factor_health_check(fp, factors=['adx14'], mc_perm=3)
    ok('无 ic_layer 时回退 stub 后端', res2['ic_backend'] == 'fallback_stub')
    del sys.modules['ic_layer']


# ── 7. scenario_value 玩具数据 ───────────────────────────────────────────────
def test_scenario_value():
    rng = np.random.RandomState(3)
    rows = []
    dates = pd.date_range('2026-01-01', periods=60)
    for d in dates:
        for s in range(30):
            tgt = rng.rand() < 0.3
            lab = rf.REGIME_UP if tgt else rf.REGIME_RANGE
            amp = rng.gamma(4, 0.01) if tgt else rng.gamma(2, 0.005)  # 目标组明显更大
            rows.append({'eob': pd.Timestamp(d, tz='Asia/Shanghai'),
                         'symbol': f'T.{s:03d}', 'regime': lab,
                         'next_amp': amp, 'next_theo': amp * 1.05,
                         'next_real': rng.normal(0.001, 0.005)})
    sv = rf.scenario_value(pd.DataFrame(rows), n_perm=50)
    ok('玩具数据: 目标组 amp 更大', sv['mann_whitney']['amp']['diff'] > 0)
    ok('玩具数据: MW 显著', sv['mann_whitney']['amp']['U_p_one_sided'] < 0.05)
    ok('玩具数据: 结论=证实', sv['verdict'] == '证实')
    ok('分组统计齐全', set(sv['group_stats']) >= {rf.REGIME_UP, rf.REGIME_RANGE})


if __name__ == '__main__':
    print('[test] regime_factors')
    test_adx()
    test_bbw()
    test_misc_factors()
    test_no_lookahead()
    test_labels()
    test_health_check_with_stub()
    test_scenario_value()
    print(f'\n[test] 全部通过 ({len(PASS)} 项)')
