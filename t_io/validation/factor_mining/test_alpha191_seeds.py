# -*- coding: utf-8 -*-
"""alpha191_seeds 测试（任务 S2-4）。

覆盖：
1. 本地迷你桩算子正确性（对照 pandas/numpy 手工实现）；
2. gp_ops 注入分发（本地迷你桩 gp_ops → 桩语义优先被采用）；
3. 39 只分钟子集实数据：≥5 个因子非退化分布（非全 NaN / 非恒定）；
4. 前视检测：截断不变性——截掉尾部 40% 重算，重叠段逐位一致（严格因果）。

跑法（managed python）：
  python t_io/validation/factor_mining/test_alpha191_seeds.py
"""
import glob
import os
import sys
import types

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import pandas as pd

import alpha191_seeds as S

CSV_DIR = os.path.abspath(os.path.join(HERE, '..', '..', 'backtest_1year_data'))
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name + (f'  [{detail}]' if detail else ''))


# ── 合成数据：3 日 × 每日 6 根 ─────────────────────────────────────────────
def make_toy(days=3, per_day=6, seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(days):
        d = f'2026-09-{14 + k:02d}'
        px = 10.0
        for m in range(per_day):
            hh = 9 * 60 + 30 + m
            t = f'{d} {hh // 60:02d}:{hh % 60:02d}:00'
            o = px
            c = px * (1 + rng.normal(0, 0.002))
            h, l = max(o, c) * 1.001, min(o, c) * 0.999
            v = float(abs(rng.normal(1e5, 3e4)))
            rows.append(dict(time=t, open=o, high=h, low=l, close=c,
                             volume=v, amount=v * c))
            px = c
    return pd.DataFrame(rows)


# ══ 1. 桩算子正确性 ══════════════════════════════════════════════════════
def test_stub_ops():
    print('== 桩算子对照 ==')
    df = make_toy(days=4, per_day=5)
    ctx = S.MinuteCtx(df)
    ops = S._MinuteOpsStub(ctx)  # 直接钉桩语义（build_ops 在 gp_ops 就位时返适配层）

    c = ctx.c
    # ts_mean(n=2)：窗口 = 最近 2 个交易日的全部 bar → 末 bar 应 = 全部 20 根中
    # 第 3、4 日（10 根）的均值（warmup 阈值 0.5×2×5=5 已满足）
    m2 = ops.ts_mean(c, 2)
    expect = c[10:20].mean()
    check('ts_mean 日窗=2日×bar', abs(m2[-1] - expect) < 1e-9,
          f'{m2[-1]:.6f} vs {expect:.6f}')
    # warmup：当日之前须满 n=2 个完整交易日 → 第 1、2 日（day_id 0/1）全 NaN
    check('ts_mean warmup 满 2 日出值', np.isnan(m2[:10]).all() and np.isfinite(m2[10:]).all())
    # 第 3 日首根（i=10）：窗长 = 之前 2 日（10 根）→ 尾窗 c[1:11]
    check('ts_mean 窗口按日对齐', abs(m2[10] - c[1:11].mean()) < 1e-9,
          f'{m2[10]:.6f} vs {c[1:11].mean():.6f}')

    # ts_delay(n=1)：同时刻对齐 → 第 k 日第 m 根 = 第 k−1 日第 m 根
    dl = ops.ts_delay(c, 1)
    check('ts_delay 同时刻对齐', np.isnan(dl[0]) and abs(dl[5] - c[0]) < 1e-12
          and abs(dl[7] - c[2]) < 1e-12)

    # ts_rank 值域 (0,1]
    r = ops.ts_rank(c, 3)
    rv = r[np.isfinite(r)]
    check('ts_rank 值域 (0,1]', len(rv) and rv.min() > 0 and rv.max() <= 1 + 1e-12)

    # ts_corr 与 numpy 逐窗对照（窗口=2 日=10 bar，末 bar）
    co = ops.ts_corr(ctx.h, ctx.v, 2)
    ref = np.corrcoef(ctx.h[10:20], ctx.v[10:20])[0, 1]
    check('ts_corr 对照 np.corrcoef', abs(co[-1] - ref) < 1e-9,
          f'{co[-1]:.6f} vs {ref:.6f}')

    # ts_std 对照 numpy（ddof=1，与 gp_ops/factor_ops 口径一致）
    sd = ops.ts_std(c, 2)
    check('ts_std 对照 np.std(ddof=1)', abs(sd[-1] - np.std(c[10:20], ddof=1)) < 1e-9)

    # ts_max：窗口 2 日 = 第 3、4 日全部 bar 的最大值
    mx = ops.ts_max(c, 2)
    check('ts_max 跨日取极值', abs(mx[-1] - c[10:20].max()) < 1e-12)

    # decay_linear：窗口内线性权重（末 bar 权重最大）
    dc = ops.decay_linear(c, 2)
    w = np.arange(1, 11, dtype=float)
    ref = float(np.dot(c[10:20], w) / w.sum())
    check('decay_linear 线性加权', abs(dc[-1] - ref) < 1e-9)

    # sma(n=2,m=1) 递推 = ewm(alpha=0.5)
    sm = ops.sma(c, 2, 1)
    y = c[0]
    for i in range(1, len(c)):
        y = (c[i] + y) / 2
    check('sma 递推口径', abs(sm[-1] - y) < 1e-9)


# ══ 2. gp_ops 注入分发（本地迷你桩 gp_ops）═══════════════════════════════
def test_gp_ops_dispatch():
    print('== gp_ops 契约分发（本地迷你桩注入）==')
    calls = []
    import factor_ops as fo

    fake = types.ModuleType('gp_ops')
    def _mk(name):
        def fn(x, *a):
            calls.append((name, a[-1] if a else None))
            return getattr(fo, name.replace('ts_', 'ts_'))(np.asarray(x, float), *a)
        return fn
    for nm in ('ts_mean', 'ts_std', 'ts_max', 'ts_min', 'ts_corr',
               'ts_delay', 'ts_delta', 'decay_linear'):
        setattr(fake, nm, _mk(nm))
    fake.ts_delay = lambda x, n: (calls.append(('ts_delay', n)), fo.delay(np.asarray(x, float), n))[1]
    fake.ts_delta = lambda x, n: (calls.append(('ts_delta', n)), fo.delta(np.asarray(x, float), n))[1]
    fake.decay_linear = lambda x, n: (calls.append(('decay_linear', n)), fo.decay_linear(np.asarray(x, float), n))[1]
    sys.modules['gp_ops'] = fake
    try:
        import importlib
        importlib.reload(S)
        # 玩具数据 4 日 × 5 根：日长均匀 → 适配层应把 10 日折算为 10×5=50 bar 调 gp_ops
        ctx = S.MinuteCtx(make_toy(days=4, per_day=5))
        ops = S.build_ops(ctx)
        check('gp_ops 就位时走适配层', isinstance(ops, S._GpOpsDayAdapter))
        S.alpha_139(ctx)
        check('alpha_139 经 gp_ops.ts_corr 求值（窗长折 bar）',
              ('ts_corr', 50) in calls, str(calls))
    finally:
        # 屏蔽 gp_ops（sys.modules 置 None → import 抛 ImportError），验证桩回退
        sys.modules['gp_ops'] = None
        import importlib
        importlib.reload(S)
        ops = S.build_ops(S.MinuteCtx(make_toy()))
        check('屏蔽 gp_ops 后回退本地桩', type(ops) is S._MinuteOpsStub)
        del sys.modules['gp_ops']
        importlib.reload(S)


# ══ 2b. gp_ops 真实模块 parity：适配层 vs 桩（均匀日长 + 小规模）══════════
def test_gp_ops_parity():
    print('== gp_ops parity（真实 S2-3 模块，双方均有限处逐位对照）==')
    if S._gp is None:
        print('   gp_ops 未安装，跳过')
        return
    df = make_toy(days=8, per_day=120, seed=11)   # 均匀日长，960 bar < GP_MAX_BARS
    ctx = S.MinuteCtx(df)
    ad, st = S._GpOpsDayAdapter(ctx), S._MinuteOpsStub(ctx)
    check('parity 前提：日长均匀', ad._uniform and ad._day_bars == 120)
    pairs = [('ts_mean', (ctx.c, 3)), ('ts_std', (ctx.c, 3)),
             ('ts_max', (ctx.h, 2)), ('ts_min', (ctx.l, 2)),
             ('ts_corr', (ctx.h, ctx.v, 3)), ('ts_delay', (ctx.c, 2)),
             ('ts_delta', (ctx.c, 2)), ('decay_linear', (ctx.c, 3))]
    for name, args in pairs:
        n = args[-1]
        a = getattr(ad, name)(*args)
        b = getattr(st, name)(*args)
        both = np.isfinite(a) & np.isfinite(b)
        diff = np.abs(a[both] - b[both]).max() if both.any() else 0.0
        # 有限性错位只允许出现在 warmup 边界（i < n×B：gp 在第 N−1 日末根先出值）
        mism = np.where(np.isfinite(a) != np.isfinite(b))[0]
        warm_ok = len(mism) == 0 or int(mism.max()) < n * 120
        check(f'parity {name}', warm_ok and diff < 1e-8,
              f'对照点{both.sum()} max|Δ|={diff:.2e} 错位{len(mism)}处(warmup内={warm_ok})')


# ══ 3. 39 只子集非退化分布 ════════════════════════════════════════════════
REALTIME_FACTORS = ['a139', 'a002', 'a013', 'a028', 'a043', 'a063', 'a070', 'a085']


def test_nondegenerate_39():
    print('== 39 只分钟子集 · 非退化分布（8 因子）==')
    files = sorted(glob.glob(os.path.join(CSV_DIR, '*1min.csv')))
    check('39 只 CSV 就位', len(files) == 39, f'{len(files)} 个')
    stats = {f: dict(nan=0, tot=0, vals=[]) for f in REALTIME_FACTORS}
    per_stock_fail = 0
    for fp in files:
        df = S.load_minute_csv(fp)
        ctx = S.MinuteCtx(df)
        vals_map = {f: S.SEEDS[f](ctx).to_numpy() for f in REALTIME_FACTORS}
        for f, v in vals_map.items():
            st = stats[f]
            st['tot'] += len(v)
            st['nan'] += int(np.isnan(v).sum())
            st['vals'].append(v[np.isfinite(v)][::97])  # 抽样汇聚，控内存
        # 每股粗检：至少 6/8 因子有非 NaN 值
        ok = sum(1 for v in vals_map.values() if np.isfinite(v).mean() > 0.1)
        if ok < 6:
            per_stock_fail += 1
    check('逐票出值（≥6/8 因子非 NaN>10%）', per_stock_fail == 0,
          f'{per_stock_fail} 票失败')
    n_pass = 0
    for f in REALTIME_FACTORS:
        st = stats[f]
        vals = np.concatenate(st['vals'])
        ratio = 1 - st['nan'] / max(st['tot'], 1)
        nondegenerate = (len(vals) > 1000 and np.nanstd(vals) > 1e-12
                         and len(np.unique(vals)) > 100 and ratio > 0.3)
        n_pass += bool(nondegenerate)
        check(f'{f} 非退化', nondegenerate,
              f'有效率{ratio:.1%} std={np.nanstd(vals):.4g} 唯一值{len(np.unique(vals))}')
    check('≥5 因子非退化（任务门槛）', n_pass >= 5, f'{n_pass}/8')


# ══ 4. 前视检测：截断不变性 ═══════════════════════════════════════════════
def test_no_lookahead():
    print('== 前视检测：截断不变性 ==')
    files = sorted(glob.glob(os.path.join(CSV_DIR, '*1min.csv')))[:3]
    worst = 0.0
    for fp in files:
        df = S.load_minute_csv(fp)
        cut = int(len(df) * 0.6)
        # 截在日中任意位置（非日界），最严苛
        for f in REALTIME_FACTORS:
            full = S.SEEDS[f](df).to_numpy()
            part = S.SEEDS[f](df.iloc[:cut].copy()).to_numpy()
            a, b = full[:cut], part
            both = np.isfinite(a) & np.isfinite(b)
            if both.sum() == 0:
                continue
            diff = np.abs(a[both] - b[both]).max()
            # NaN 位置也必须一致（预热区不得因尾部数据而变化）
            nan_mismatch = int((np.isnan(a) != np.isnan(b)).sum())
            worst = max(worst, diff)
            check(f'{os.path.basename(fp)[:6]} {f} 截断不变',
                  diff < 1e-9 and nan_mismatch == 0,
                  f'max|Δ|={diff:.2e} nan错位{nan_mismatch}')
    print(f'   worst max|Δ| = {worst:.2e}')


if __name__ == '__main__':
    test_stub_ops()
    test_gp_ops_dispatch()
    test_gp_ops_parity()
    test_nondegenerate_39()
    test_no_lookahead()
    print(f'\n== 汇总：{len(PASS)} 过 / {len(FAIL)} 败 ==')
    if FAIL:
        print('失败项：' + ', '.join(FAIL))
        sys.exit(1)
