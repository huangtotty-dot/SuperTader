# -*- coding: utf-8 -*-
"""
D1 后半段衰减归因诊断
回答: 反转 alpha 为什么 2025-06 后走弱?
产物: t_io/validation/factor_mining/results/decay_diag/
用法: python decay_diagnosis.py A  (口径/IC/拐点/环境, 落盘中间件)
      python decay_diagnosis.py B  (分层/门控)
"""
import json
import os
import sys
import numpy as np
import pandas as pd

PART = sys.argv[1] if len(sys.argv) > 1 else 'A'

BASE = 't_io/validation/factor_mining'
RES = os.path.join(BASE, 'results')
FH = os.path.join(RES, 'fullhist_robustness_2026-10-09')
OUT = os.path.join(RES, 'decay_diag')
os.makedirs(OUT, exist_ok=True)
TMP = os.path.join(OUT, '_tmp')
os.makedirs(TMP, exist_ok=True)

FACTORS = ['REV10', 'GAP', 'AMOUNT_CHG_inv', 'PRICE_POS60_inv']
MAIN_START = '2024-03-05'
MAIN_END = '2026-09-17'


if PART == 'A':
    # ---------------------------------------------------------------- 1. 面板
    print('[1] load panel ...', flush=True)
    panel = pd.read_parquet(os.path.join(FH, 'panel_processed.parquet'))
    panel = panel[panel['date'] >= '2006-01-01'].copy()
    print('  rows:', len(panel), 'symbols:', panel['symbol'].nunique(),
          'range:', panel['date'].min().date(), '->', panel['date'].max().date(), flush=True)

    close = panel.pivot(index='date', columns='symbol', values='close').sort_index()
    amount = panel.pivot(index='date', columns='symbol', values='amount').reindex(close.index)
    del panel

    # ---------------------------------------------------------------- 2. 卫生宇宙掩码
    print('[2] hygiene mask ...', flush=True)
    roll_max60 = close.rolling(60, min_periods=20).max()
    dd60 = close / roll_max60 - 1.0                     # 60日回撤
    mask = (close >= 2.0) & (dd60 > -0.60)              # <2元 + 60日回撤>60% 剔除
    del roll_max60, dd60

    # ---------------------------------------------------------------- 3. REV10 日IC重算(验证口径)
    print('[3] recompute REV10 daily IC (h5) ...', flush=True)
    ret1 = close / close.shift(1) - 1.0
    rev10 = -(close / close.shift(10) - 1.0)            # 反转: 过去10日跌幅大者因子值高
    fwd5 = close.shift(-5) / close - 1.0

    valid = mask & rev10.notna() & fwd5.notna()

    def fast_spearman_by_day(fac, fwd, valid, layer=None, min_n=30):
        """按日(可选按层)快速 Spearman: 先组内 rank 再用求和公式算 Pearson。"""
        f = fac.where(valid)
        r = fwd.where(valid)
        fr = f.rank(axis=1).values.ravel()
        rr = r.rank(axis=1).values.ravel()
        ok = ~np.isnan(fr) & ~np.isnan(rr)
        d = np.repeat(f.index.values, f.shape[1])
        if layer is not None:
            lv = layer.where(valid).values.ravel()
            ok2 = ok & ~pd.isna(lv)
            df = pd.DataFrame({'d': d[ok2], 'l': lv[ok2].astype(object),
                               'fr': fr[ok2].astype(np.float32),
                               'rr': rr[ok2].astype(np.float32)})
            gkeys = ['d', 'l']
        else:
            df = pd.DataFrame({'d': d[ok], 'fr': fr[ok].astype(np.float32),
                               'rr': rr[ok].astype(np.float32)})
            gkeys = ['d']
        df['frrr'] = df['fr'] * df['rr']
        df['fr2'] = df['fr'] * df['fr']
        df['rr2'] = df['rr'] * df['rr']
        agg = df.groupby(gkeys)[['fr', 'rr', 'frrr', 'fr2', 'rr2']].sum()
        agg['n'] = df.groupby(gkeys).size()
        agg = agg[agg['n'] >= min_n]
        n = agg['n']
        cov = agg['frrr'] - agg['fr'] * agg['rr'] / n
        vf = agg['fr2'] - agg['fr'] ** 2 / n
        vr = agg['rr2'] - agg['rr'] ** 2 / n
        ic = cov / np.sqrt(vf * vr)
        ic.name = 'ic'
        return ic

    ic_rev10 = fast_spearman_by_day(rev10, fwd5, valid)
    ic_rev10.index = pd.to_datetime(ic_rev10.index)
    print('  recomputed days:', len(ic_rev10), flush=True)

    # 与官方 CSV 校验
    csv_ic = pd.read_csv(os.path.join(FH, 'ic_daily_REV10_h5.csv'),
                         index_col=0, parse_dates=True)['ic']
    ov = csv_ic.index.intersection(ic_rev10.index)
    corr_chk = csv_ic.loc[ov].corr(ic_rev10.loc[ov])
    bias = (ic_rev10.loc[ov] - csv_ic.loc[ov]).mean()
    print(f'  validation vs csv: overlap={len(ov)}d corr={corr_chk:.4f} bias={bias:+.4f}', flush=True)

    # ---------------------------------------------------------------- 4. 四因子滚动IC与拐点
    print('[4] rolling IC + breakpoint scan ...', flush=True)
    ics = {}
    for fct in FACTORS:
        s = pd.read_csv(os.path.join(FH, f'ic_daily_{fct}_h5.csv'),
                        index_col=0, parse_dates=True)['ic']
        ics[fct] = s
    ic_df = pd.DataFrame(ics)
    roll60 = ic_df.rolling(60, min_periods=40).mean()
    roll60.to_csv(os.path.join(OUT, 'rolling60_ic_4factors.csv'))

    def breakpoint_scan(s, cand_start='2024-01-01', cand_end='2026-03-31',
                        min_post=100, pre_start=None):
        """在候选区间扫分割点, 使 post 均值相对 pre 均值的 t 统计量最负。"""
        s = s.dropna()
        best = None
        cand = s.loc[cand_start:cand_end].index
        for d in cand:
            pre = s.loc[:d].iloc[:-1] if pre_start is None else s.loc[pre_start:d].iloc[:-1]
            post = s.loc[d:]
            if len(post) < min_post or len(pre) < 120:
                continue
            t = (post.mean() - pre.mean()) / np.sqrt(post.var() / len(post) + pre.var() / len(pre) + 1e-12)
            if best is None or t < best[1]:
                best = (d, t, pre.mean(), post.mean())
        return best

    bp_table = {}
    for fct in FACTORS:
        d, t, mpre, mpost = breakpoint_scan(ics[fct])
        d2, t2, mpre2, mpost2 = breakpoint_scan(ics[fct], pre_start=MAIN_START, min_post=80)
        bp_table[fct] = {'breakpoint_fullhist': str(d.date()), 't': round(float(t), 2),
                         'ic_pre': round(float(mpre), 4), 'ic_post': round(float(mpost), 4),
                         'breakpoint_mainwin': str(d2.date()), 't_mw': round(float(t2), 2),
                         'ic_pre_mw': round(float(mpre2), 4), 'ic_post_mw': round(float(mpost2), 4)}
        print(f'  {fct}: bp(full)={d.date()} t={t:.1f} pre={mpre:+.4f} post={mpost:+.4f} | '
              f'bp(mw)={d2.date()} t={t2:.1f} pre={mpre2:+.4f} post={mpost2:+.4f}', flush=True)

    BP = pd.Timestamp(bp_table['REV10']['breakpoint_mainwin'])
    # 滚动均值首次持续翻负(拐点后)
    r = roll60['REV10'].loc['2024-01-01':]
    neg = r[r < 0]
    first_sustained_neg = None
    if len(neg):
        for d0 in neg.index:
            seg = r.loc[d0:].iloc[:20]
            if (seg < 0).mean() >= 0.9:
                first_sustained_neg = d0
                break
    print('  REV10 roll60 first sustained <0:', first_sustained_neg, flush=True)
    # 滚动均值时代快照: 2024H2 / 2025H1 / 2025H2 / 2026
    epochs = {}
    for name, (a, b) in {'2024H2': ('2024-07-01', '2024-12-31'),
                         '2025H1': ('2025-01-01', '2025-06-30'),
                         '2025H2': ('2025-07-01', '2025-12-31'),
                         '2026YTD': ('2026-01-01', '2026-09-17')}.items():
        for fct in FACTORS:
            seg = roll60[fct].loc[a:b].dropna()
            epochs[f'{fct}@{name}'] = round(float(seg.mean()), 4) if len(seg) else None
    print('  roll60 epoch means:', epochs, flush=True)

    # 主窗内前后半 (固定 2025-06-01 与自适应拐点两套)
    def prepost(s, split):
        pre = s.loc[MAIN_START:split].iloc[:-1].dropna()
        post = s.loc[split:MAIN_END].dropna()
        t = (post.mean() - pre.mean()) / np.sqrt(post.var() / len(post) + pre.var() / len(pre) + 1e-12)
        return {'pre_mean': round(float(pre.mean()), 4), 'post_mean': round(float(post.mean()), 4),
                'pre_icir': round(float(pre.mean() / pre.std()), 3),
                'post_icir': round(float(post.mean() / post.std()), 3),
                't': round(float(t), 2), 'n_pre': len(pre), 'n_post': len(post)}

    pp_fixed = {f: prepost(ics[f], '2025-06-01') for f in FACTORS}
    pp_adapt = {f: prepost(ics[f], BP) for f in FACTORS}
    json.dump({'breakpoint_scan': bp_table,
               'roll60_first_sustained_neg_REV10': str(first_sustained_neg),
               'roll60_epoch_means': epochs,
               'prepost_fixed_2025-06-01': pp_fixed,
               'prepost_adaptive_bp': pp_adapt,
               'ic_recompute_check': {'overlap_days': int(len(ov)),
                                      'corr': round(float(corr_chk), 4),
                                      'bias': round(float(bias), 4)}},
              open(os.path.join(OUT, 'breakpoint_and_prepost.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    # ---------------------------------------------------------------- 5. 环境共变
    print('[5] environment covariates ...', flush=True)
    ret1m = ret1.where(mask)
    amt_m = amount.where(mask)

    env = pd.DataFrame(index=close.index)
    env['mkt_amount20'] = amt_m.sum(axis=1).rolling(20).mean()          # 全市场成交额20日均
    env['xsec_disp20'] = ret1m.std(axis=1).rolling(20).mean()           # 截面收益离散度20日均
    env['adv_ratio20'] = (ret1m > 0).sum(axis=1).div(ret1m.count(axis=1)).rolling(20).mean()  # 上涨家数占比20日均
    env['amount20_med'] = amt_m.rolling(20).mean().median(axis=1)       # AMOUNT20宇宙中位数

    idx_raw = json.load(open('t_io/cache/daily_kline/index_sh000001.json', encoding='utf-8'))
    idx = pd.DataFrame(idx_raw['rows'])
    idx['date'] = pd.to_datetime(idx['date'])
    idx = idx.set_index('date').sort_index()
    idxc = idx['close'].astype(float)
    env['sse_mom20'] = idxc / idxc.shift(20) - 1
    env['sse_mom60'] = idxc / idxc.shift(60) - 1
    env['sse_vol20'] = idxc.pct_change().rolling(20).std()
    env = env.dropna(how='all')
    env.to_csv(os.path.join(OUT, 'env_vars_daily.csv'))

    icr = roll60['REV10']
    env_s = env.rolling(60, min_periods=40).mean()
    rows = []
    for c in env_s.columns:
        al = pd.concat([icr, env_s[c]], axis=1, keys=['ic', 'x']).dropna()
        al3 = al.loc['2023-01-01':]
        dic = al.diff(20).dropna()  # 变化量相关, 避免水平伪相关
        dic3 = dic.loc['2023-01-01':]
        rows.append({'var': c,
                     'corr_lvl_full': round(float(al['ic'].corr(al['x'])), 3),
                     'corr_lvl_2023+': round(float(al3['ic'].corr(al3['x'])), 3),
                     'corr_d20_full': round(float(dic['ic'].corr(dic['x'])), 3),
                     'corr_d20_2023+': round(float(dic3['ic'].corr(dic3['x'])), 3)})
    env_corr = pd.DataFrame(rows).sort_values('corr_d20_2023+', key=abs, ascending=False)
    env_corr.to_csv(os.path.join(OUT, 'env_corr.csv'), index=False)
    print(env_corr.to_string(index=False), flush=True)

    # ---------------- part A 中间件落盘 (win 切片) ----------------
    print('[A] save intermediates ...', flush=True)
    win = close.loc[MAIN_START:MAIN_END].index
    rev10.loc[win].astype('float32').to_parquet(os.path.join(TMP, 'rev10_w.parquet'))
    fwd5.loc[win].astype('float32').to_parquet(os.path.join(TMP, 'fwd5_w.parquet'))
    valid.loc[win].astype('uint8').to_parquet(os.path.join(TMP, 'valid_w.parquet'))
    close.loc[win].astype('float32').to_parquet(os.path.join(TMP, 'close_w.parquet'))
    amt20 = amount.rolling(20).mean()
    amt20.loc[win].astype('float32').to_parquet(os.path.join(TMP, 'amt20_w.parquet'))
    ic_rev10.to_csv(os.path.join(TMP, 'ic_rev10_recomputed.csv'))
    print('[A done]', flush=True)

    sys.exit(0)

# ---------------- part B: 载入中间件 ----------------
print('[B] load intermediates ...', flush=True)
rev10_w = pd.read_parquet(os.path.join(TMP, 'rev10_w.parquet'))
fwd5_w = pd.read_parquet(os.path.join(TMP, 'fwd5_w.parquet'))
valid_w = pd.read_parquet(os.path.join(TMP, 'valid_w.parquet')).astype(bool)
close_w = pd.read_parquet(os.path.join(TMP, 'close_w.parquet'))
amt20_w = pd.read_parquet(os.path.join(TMP, 'amt20_w.parquet'))
ic_rev10 = pd.read_csv(os.path.join(TMP, 'ic_rev10_recomputed.csv'), index_col=0, parse_dates=True)['ic']
env = pd.read_csv(os.path.join(OUT, 'env_vars_daily.csv'), index_col=0, parse_dates=True)
env_corr = pd.read_csv(os.path.join(OUT, 'env_corr.csv'))
_bp = json.load(open(os.path.join(OUT, 'breakpoint_and_prepost.json'), encoding='utf-8'))
BP = pd.Timestamp(_bp['breakpoint_scan']['REV10']['breakpoint_mainwin'])
win = rev10_w.index
print('  BP =', BP.date(), flush=True)

def fast_spearman_by_day(fac, fwd, valid, layer=None, min_n=30):
    """按日(可选按层)快速 Spearman: 先组内 rank 再用求和公式算 Pearson。"""
    f = fac.where(valid)
    r = fwd.where(valid)
    fr = f.rank(axis=1).values.ravel()
    rr = r.rank(axis=1).values.ravel()
    ok = ~np.isnan(fr) & ~np.isnan(rr)
    d = np.repeat(f.index.values, f.shape[1])
    if layer is not None:
        lv = layer.where(valid).values.ravel()
        ok2 = ok & ~pd.isna(lv)
        df = pd.DataFrame({'d': d[ok2], 'l': lv[ok2].astype(object),
                           'fr': fr[ok2].astype(np.float32),
                           'rr': rr[ok2].astype(np.float32)})
        gkeys = ['d', 'l']
    else:
        df = pd.DataFrame({'d': d[ok], 'fr': fr[ok].astype(np.float32),
                           'rr': rr[ok].astype(np.float32)})
        gkeys = ['d']
    df['frrr'] = df['fr'] * df['rr']
    df['fr2'] = df['fr'] * df['fr']
    df['rr2'] = df['rr'] * df['rr']
    agg = df.groupby(gkeys)[['fr', 'rr', 'frrr', 'fr2', 'rr2']].sum()
    agg['n'] = df.groupby(gkeys).size()
    agg = agg[agg['n'] >= min_n]
    n = agg['n']
    cov = agg['frrr'] - agg['fr'] * agg['rr'] / n
    vf = agg['fr2'] - agg['fr'] ** 2 / n
    vr = agg['rr2'] - agg['rr'] ** 2 / n
    ic = cov / np.sqrt(vf * vr)
    ic.name = 'ic'
    return ic


# ---------------------------------------------------------------- 6. 结构分层 IC (REV10, 主窗)
print('[6] layered IC (REV10, main window) ...', flush=True)

def tercile_layer(mat):
    """按每日截面三分位打层: 0小/1中/2大"""
    q1 = mat.quantile(1 / 3, axis=1)
    q2 = mat.quantile(2 / 3, axis=1)
    l = pd.DataFrame(np.nan, index=mat.index, columns=mat.columns)
    l[mat.le(q1, axis=0)] = 0
    l[mat.gt(q1, axis=0) & mat.le(q2, axis=0)] = 1
    l[mat.gt(q2, axis=0)] = 2
    return l

LAYERS = {}
# 市值(成交额代理)层
LAYERS['size_by_amount20'] = tercile_layer(amt20_w)
# 价格段
price_layer = pd.DataFrame(np.nan, index=close_w.index, columns=close_w.columns)
price_layer[close_w < 5] = 0
price_layer[(close_w >= 5) & (close_w < 20)] = 1
price_layer[close_w >= 20] = 2
LAYERS['price_seg'] = price_layer
# 行业层
imap = pd.read_csv('t_io/rotation/industry_map.csv', dtype={'代码': str})
imap['代码'] = imap['代码'].str.zfill(6)
sym2ind = dict(zip(imap['代码'], imap['行业名称']))
mapped = np.array([sym2ind.get(s) for s in close_w.columns], dtype=object)
ind_layer = pd.DataFrame(np.repeat(mapped[None, :], len(close_w.index), axis=0),
                         index=close_w.index, columns=close_w.columns)
LAYERS['industry'] = ind_layer

layer_rows = []
ind_rows = []
for lname, lmat in LAYERS.items():
    icl = fast_spearman_by_day(rev10_w, fwd5_w, valid_w, layer=lmat.loc[win], min_n=20)
    icl = icl.reset_index()
    icl.columns = ['d', 'l', 'ic']
    icl['d'] = pd.to_datetime(icl['d'])
    icl = icl[icl['l'].notna()]
    icl.to_csv(os.path.join(OUT, f'ic_daily_layered_{lname}.csv'), index=False)
    if lname != 'industry':
        for lv, gdf in icl.groupby('l'):
            g = gdf.set_index('d')['ic'].loc[MAIN_START:MAIN_END].dropna()
            pre = g.loc[:BP].iloc[:-1]
            post = g.loc[BP:]
            if len(pre) < 60 or len(post) < 60:
                continue
            layer_rows.append({'layer': lname, 'seg': int(lv),
                               'ic_pre': round(float(pre.mean()), 4),
                               'ic_post': round(float(post.mean()), 4),
                               'delta': round(float(post.mean() - pre.mean()), 4),
                               'icir_pre': round(float(pre.mean() / pre.std()), 3),
                               'icir_post': round(float(post.mean() / post.std()), 3)})
    else:
        for lv, gdf in icl.groupby('l'):
            g = gdf.set_index('d')['ic'].loc[MAIN_START:MAIN_END].dropna()
            pre = g.loc[:BP].iloc[:-1]
            post = g.loc[BP:]
            if len(pre) < 120 or len(post) < 120:
                continue
            ind_rows.append({'industry': lv, 'n_days': len(g),
                             'ic_pre': round(float(pre.mean()), 4),
                             'ic_post': round(float(post.mean()), 4),
                             'delta': round(float(post.mean() - pre.mean()), 4)})

layer_df = pd.DataFrame(layer_rows)
layer_df.to_csv(os.path.join(OUT, 'layers_size_price.csv'), index=False)
ind_df = pd.DataFrame(ind_rows).sort_values('delta')
ind_df.to_csv(os.path.join(OUT, 'layers_industry.csv'), index=False)
print(layer_df.to_string(index=False), flush=True)
print(ind_df.head(8).to_string(index=False), flush=True)
print(ind_df.tail(8).to_string(index=False), flush=True)

# ---------------------------------------------------------------- 7. 可门控性检验
print('[7] gating test ...', flush=True)
top_vars = env_corr.head(3)['var'].tolist()
gate_rows = []
ic_main = ic_rev10.loc[MAIN_START:MAIN_END].dropna()
for v in top_vars + ['mkt_amount20', 'xsec_disp20']:
    x = env[v].loc[MAIN_START:MAIN_END]
    al = pd.concat([ic_main, x], axis=1, keys=['ic', 'x']).dropna()
    for regime_name, cond in [('full_hi', al['x'] >= al['x'].median()),
                              ('full_lo', al['x'] < al['x'].median()),
                              ('post_hi', (al['x'] >= al['x'].median()) & (al.index >= BP)),
                              ('post_lo', (al['x'] < al['x'].median()) & (al.index >= BP)),
                              ('pre_hi', (al['x'] >= al['x'].median()) & (al.index < BP)),
                              ('pre_lo', (al['x'] < al['x'].median()) & (al.index < BP))]:
        sub = al.loc[cond, 'ic']
        if len(sub) >= 40:
            gate_rows.append({'var': v, 'regime': regime_name, 'n': len(sub),
                              'ic_mean': round(float(sub.mean()), 4),
                              'icir': round(float(sub.mean() / sub.std()), 3)})
gate_df = pd.DataFrame(gate_rows)
gate_df.to_csv(os.path.join(OUT, 'gating_test.csv'), index=False)
print(gate_df.to_string(index=False), flush=True)

print('[done] ->', OUT, flush=True)
