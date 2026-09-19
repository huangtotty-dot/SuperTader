# -*- coding: utf-8 -*-
"""regime_expansion 单元测试（合成数据，验证窗口对齐 / 事件边沿 / 突破标记 / MC 通路）。"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import regime_expansion as rx


def _mk_symbol(sym, n, amp_lo=0.02, amp_hi=0.06, split=30, adx_const=10.0,
               bbw_pct=0.10, regime_tail=rx.REGIME_SQUEEZE, start='2020-01-01'):
    """合成单 symbol：split 前振幅 amp_lo、split 起 amp_hi；split 起 regime=regime_tail。"""
    eob = pd.date_range(start, periods=n, freq='B', tz='Asia/Shanghai')
    close = np.full(n, 10.0)
    amp = np.where(np.arange(n) < split, amp_lo, amp_hi)
    half = amp * 10.0 / 2.0
    df = pd.DataFrame({
        'symbol': sym, 'eob': eob, 'close': close,
        'high': close + half, 'low': close - half,
        'adx14': np.full(n, adx_const),
        'bbw_pct120': np.full(n, bbw_pct),
        'n_hist': np.arange(1, n + 1),
        'regime': np.where(np.arange(n) < split, rx.REGIME_RANGE, regime_tail),
    })
    return df


def _prep(df):
    df = df.sort_values(['symbol', 'eob'], kind='mergesort').reset_index(drop=True)
    win = df.groupby('symbol', sort=False, group_keys=False).apply(rx.compute_windows)
    df = pd.concat([df, win], axis=1)
    df['event'] = rx.mark_events(df, use_label=True)
    df['board'] = rx.board_of(df['symbol'])
    return df


def test_windows_alignment():
    """pre20/postN 窗口对齐与边界 NaN。"""
    d = _prep(_mk_symbol('SHSE.600000', 60))
    g = d[d['symbol'] == 'SHSE.600000'].reset_index(drop=True)
    # pre20：t>=20 有值，等于前 20 日 amp 均值；t=19 之前 NaN
    assert np.isnan(g['pre20'].iloc[19])
    assert abs(g['pre20'].iloc[20] - 0.02) < 1e-6
    # post3：t 行 = amp[t+1..t+3]；末尾 3 行 NaN
    assert np.isnan(g['post3'].iloc[-1]) and np.isnan(g['post3'].iloc[-3])
    # 事件在 split=30（标签转入震荡末期首日），且唯一
    ev = g[g['event']]
    assert len(ev) == 1 and ev.index[0] == 30
    i = 30
    assert abs(g['pre20'].iloc[i] - 0.02) < 1e-6          # 前 20 日全在低幅段
    assert abs(g['post5'].iloc[i] - 0.06) < 1e-6          # 后 5 日全在高幅段
    assert abs(g['post10'].iloc[i] - 0.06) < 1e-6
    print('PASS test_windows_alignment')


def test_event_edge_cases():
    """历史不足(NaN)→震荡末期 也算进入；连续末期只计首日；阈值≤22 时条件口径=打标口径。"""
    a = _mk_symbol('SHSE.600001', 50, split=20)
    a.loc[a.index[:20], 'regime'] = np.nan                # 前 20 日历史不足
    d = _prep(a)
    g = d.reset_index(drop=True)
    assert g['event'].sum() == 1 and g['event'].idxmax() == 20
    # 条件口径（ADX<20 & BBW≤0.20，n_hist≥140 不满足 → 用 MIN_BARS_LABEL 低样本下全 False）
    ev_cond = rx.mark_events(g, adx_thr=20.0, bbw_q=0.20, use_label=False)
    assert ev_cond.sum() == 0                              # n_hist 不足 140 → 无事件
    g2 = g.copy()
    g2['n_hist'] = np.arange(200, 200 + len(g2))           # 伪造充足历史
    g2.loc[g2.index[:20], ['adx14', 'bbw_pct120']] = np.nan  # 前 20 日指标未就绪（同打标 NaN）
    ev_cond2 = rx.mark_events(g2, adx_thr=20.0, bbw_q=0.20, use_label=False)
    assert ev_cond2.sum() == 1 and ev_cond2.idxmax() == 20  # 与打标口径一致
    print('PASS test_event_edge_cases')


def test_breakout_flag():
    """事件后 10 日内 ADX 上穿 25 → brk10=1。"""
    a = _mk_symbol('SHSE.600002', 60, split=30)
    a.loc[a.index[33], 'adx14'] = 26.0                     # 事件后第 3 日上穿
    d = _prep(a)
    g = d.reset_index(drop=True)
    i = int(g[g['event']].index[0])
    assert g['brk10'].iloc[i] == 1.0
    # 对照：全程无上穿
    b = _prep(_mk_symbol('SZSE.000002', 60, split=30))
    g2 = b.reset_index(drop=True)
    j = int(g2[g2['event']].index[0])
    assert g2['brk10'].iloc[j] == 0.0
    print('PASS test_breakout_flag')


def test_board_map():
    s = pd.Series(['SHSE.600519', 'SZSE.000001', 'SZSE.300750', 'SHSE.688981', 'BJSE.830799'])
    b = rx.board_of(s).tolist()
    assert b == ['沪主板60', '深主板00', '创业板30', '科创板68', '其他']
    print('PASS test_board_map')


def test_h1_h2_smoke():
    """两 symbol 小样本：H1 应检出扩张（diff>0），H2/MC 通路不崩。"""
    a = _mk_symbol('SHSE.600003', 80, split=40)
    b = _mk_symbol('SZSE.300003', 80, split=40)
    c = _mk_symbol('SZSE.000003', 80, split=10 ** 9, regime_tail=rx.REGIME_RANGE)  # 全程震荡
    d = _prep(pd.concat([a, b, c], ignore_index=True))
    r1 = rx.h1_test(d, 5, n_perm=5, seed=1)
    assert r1['n_events'] == 2
    assert r1['diff_mean'] > 0.03                          # 0.06-0.02=0.04
    assert 0.0 < r1['mc_p'] <= 1.0
    # H2：对照=震荡日（split 前标签），事件后振幅 0.06 > 对照前向 ~0.02→0.04 混合
    r2 = rx.h2_test(d, 5, n_perm=5, seed=1)
    assert r2['n_events'] == 2 and r2['n_control'] > 0
    assert 0.0 < r2['mc_p'] <= 1.0
    print(f"PASS test_h1_h2_smoke (H1 diff={r1['diff_mean']:+.4f} mc_p={r1['mc_p']}; "
          f"H2 diff={r2['diff_mean']:+.4f} mc_p={r2['mc_p']})")


def test_window_incomplete_dropped():
    """窗口不完整的事件被弃用（postN NaN 不硬凑）。"""
    a = _mk_symbol('SHSE.600004', 45, split=40)            # 事件后仅 4 日 → post5 NaN
    d = _prep(a)
    r = rx.h1_test(d, 5, n_perm=3, seed=1)
    assert r['n_events'] == 0                              # N=5 弃用
    r3 = rx.h1_test(d, 3, n_perm=3, seed=1)
    assert r3['n_events'] == 1                             # N=3 可用
    print('PASS test_window_incomplete_dropped')


if __name__ == '__main__':
    test_windows_alignment()
    test_event_edge_cases()
    test_breakout_flag()
    test_board_map()
    test_h1_h2_smoke()
    test_window_incomplete_dropped()
    print('ALL TESTS PASS')
