# -*- coding: utf-8 -*-
"""E0 Stage13：独立股票池 + 生存者偏差检验（2026-09-22）。

预注册见 `doc/experiment/2026-09-22_低开反转规则_样本外预注册.md` 的**第二节**，
池子定义 / 随机种子 / 样本量 / 判据均已在拉数前冻结。

两步：
    python e0_stage13_indep_universe.py sample    # 打印并落盘冻结样本（不拉数）
    python e0_stage13_indep_universe.py fetch     # 拉 30min 到 ts_indep/
    python e0_stage13_indep_universe.py test      # 按判据出结论
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from e0_stage10_fetch_oos import fetch_one, reduce_day, START, END   # noqa: E402
from e0_stage11_oos_test import cse, apply_rule, load_oos            # noqa: E402
from core.cost_model import fees                                     # noqa: E402

UNIV = ROOT / 't_io' / 'validation' / 'xsection' / 'panel' / 'universe.parquet'
SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
OUT = HERE / 'ts_indep'
SEED, N = 20260922, 400                      # ── 冻结 ──
RULE_REL, PASS_NET, PASS_T, REJ_NET, REJ_T = -0.01, 0.20, 2.0, 0.0, 1.0
MIN_LEGS, MIN_DAYS = 500, 100
REF_981 = 0.5245                             # 主面板样本外参考


def to_ts(sym: str) -> str:
    ex, code = str(sym).split('.')
    return f'{code}.{"SH" if ex == "SHSE" else "SZ"}'


def eligible() -> pd.DataFrame:
    u = pd.read_parquet(UNIV)
    u['listed'] = pd.to_datetime(u['listed_date'], utc=True).dt.tz_localize(None)
    u['delisted'] = pd.to_datetime(u['delisted_date'], utc=True).dt.tz_localize(None)
    u['ts'] = u['symbol'].map(to_ts)
    # 文件名形如 000001.SZ_30min_d540.json ⇒ 前 9 字符即 ts_code
    panel = {f.name[:9] for f in SRC.iterdir()
             if re.match(r'^\d{6}\.(SH|SZ)_30min_d540\.json$', f.name)}
    w1, w0 = pd.Timestamp('2025-03-28'), pd.Timestamp('2019-01-01')
    el = u[(u['listed'] <= w1) & (u['delisted'] >= w0) & (~u['ts'].isin(panel))].copy()
    el['is_delisted'] = el['delisted'] < pd.Timestamp('2037-12-31')
    return el.sort_values('ts').reset_index(drop=True)


def frozen_sample() -> pd.DataFrame:
    el = eligible()
    idx = np.random.default_rng(SEED).choice(len(el), size=min(N, len(el)), replace=False)
    s = el.iloc[np.sort(idx)].reset_index(drop=True)
    return s


def cmd_sample() -> None:
    s = frozen_sample()
    print(f'合格池 {len(eligible())} 只 → 冻结样本 {len(s)} 只（seed={SEED}）')
    print(f'  其中已退市 {int(s["is_delisted"].sum())} 只 ({s["is_delisted"].mean():.1%})')
    print(f'  板块: ' + str({k: int(sum(1 for c in s["ts"] if c.split(".")[0].startswith(k)))
                            for k in ('60', '00', '30', '68')}))
    p = HERE / 'indep_sample_400.csv'
    s[['ts', 'sec_name', 'listed', 'delisted', 'is_delisted']].to_csv(p, index=False)
    print(f'  → {p}')
    print('  （样本一经落盘即冻结；fetch 与 test 均读该文件）')


def cmd_fetch(workers: int = 6) -> None:
    s = pd.read_csv(HERE / 'indep_sample_400.csv')
    OUT.mkdir(parents=True, exist_ok=True)
    todo = [c for c in s['ts'] if not (OUT / f'{c.replace(".", "_")}.parquet').exists()]
    print(f'待拉 {len(todo)} / {len(s)}   workers={workers}   → {OUT}')
    t0 = time.perf_counter()
    ok = empty = fail = done = 0
    lock = threading.Lock()

    def work(code: str):
        try:
            d = fetch_one(code)
        except Exception as e:
            return code, 'fail', str(e)[:80]
        return code, ('empty' if d.empty else 'ok'), d

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in cf.as_completed({ex.submit(work, c): c for c in todo}):
            code, st, d = fut.result()
            if st == 'ok':
                d.to_parquet(OUT / f'{code.replace(".", "_")}.parquet', index=False)
                ok += 1
            elif st == 'empty':
                empty += 1
            else:
                fail += 1
                with lock:
                    print(f'  {code} 失败: {d}')
            done += 1
            if done % 25 == 0 or done == len(todo):
                el = time.perf_counter() - t0
                print(f'  [{done}/{len(todo)}] ok={ok} 空={empty} 失败={fail}  {el:.0f}s', flush=True)
    print(f'\n完成：新拉 {ok}  空 {empty}  失败 {fail}  用时 {time.perf_counter()-t0:.0f}s')


def load_indep() -> pd.DataFrame:
    parts = [pd.read_parquet(f) for f in sorted(OUT.glob('*.parquet'))]
    if not parts:
        return pd.DataFrame()
    P = pd.concat(parts, ignore_index=True)
    if 'prev_close' not in P.columns:
        P['prev_close'] = P.groupby('code')['cl_1500'].shift(1)
    return P


def report(tag: str, R: pd.DataFrame) -> dict:
    x, g = R['net'].values, R['date'].values
    se = cse(x, g)
    st = {'tag': tag, 'n': int(len(x)), 'days': int(R['date'].nunique()),
          'net': float(x.mean()) if len(x) else float('nan'),
          'se': se, 't': (x.mean() / se if se else float('nan'))}
    print(f"  {tag:26s} n={st['n']:6d} 日={st['days']:4d} 费后={st['net']:+.4f}%  "
          f"SE={se:.4f}  t={st['t']:+.2f}")
    return st


def cmd_test() -> None:
    sp = pd.read_csv(HERE / 'indep_sample_400.csv')
    P = load_indep()
    if P.empty:
        print('无数据，先跑 fetch')
        return
    print(f'独立池样本=400（冻结）  已拉={P["code"].nunique()} 只  '
          f'日级行={len(P)}  {P["date"].min()} ~ {P["date"].max()}')
    delisted = set(sp[sp['is_delisted']]['ts'].str.replace('.', '_', regex=False))
    got = {f.stem for f in OUT.glob('*.parquet')}
    print(f'  已拉到的退市股 {len(got & delisted)}/{len(delisted)}')

    print('\n' + '=' * 86)
    print('§A 预注册检验：独立 400 只池 · 2019-01 ~ 2025-03 · 规则原样')
    print('=' * 86)
    R = apply_rule(P)
    s = report('独立池 全期间', R)
    for lab, a, b in (('2019-2021', '2019-01-01', '2021-12-31'),
                      ('2022-2025', '2022-01-01', '2025-03-28')):
        sub = R[(R['date'] >= a) & (R['date'] <= b)]
        if len(sub):
            report(lab, sub)

    print('\n§B 生存者偏差的正面检验（退市股子集 vs 在市子集）')
    R2 = R.copy()
    # parquet 里 code 列是 ts_code（带点），与 sp['ts'] 同格式
    R2['is_del'] = R2['code'].isin(set(sp[sp['is_delisted']]['ts']))
    for lab, sub in (('退市股子集', R2[R2['is_del']]), ('在市股子集', R2[~R2['is_del']])):
        if len(sub) >= 30:
            report(lab, sub)
        else:
            print(f'  {lab:26s} n={len(sub)} —— 过薄，只报不判')

    print('\n§C 市场代理敏感性：改用主面板 981 只的 mkt_gap（更干净的市场状态）')
    Mp = load_oos()
    if 'prev_close' not in Mp.columns:
        Mp['prev_close'] = Mp.groupby('code')['cl_1500'].shift(1)
    Mp['gap'] = Mp['op_auc'] / Mp['prev_close'] - 1
    mg = (Mp[np.isfinite(Mp['gap'])].groupby('date')['gap'].median().rename('mkt981'))
    Q = P.copy()
    Q['gap'] = Q['op_auc'] / Q['prev_close'] - 1
    Q = Q[np.isfinite(Q['gap']) & (Q['op_auc'] > 0) & (Q['cl_1000'] > 0)]
    Q = Q.merge(mg, left_on='date', right_index=True, how='inner')
    Q['rel'] = Q['gap'] - Q['mkt981']
    Rq = Q[(Q['mkt981'] < 0) & (Q['rel'] <= RULE_REL)].copy()
    fs, fb = fees('stock')
    Rq['net'] = ((Rq['cl_1000'] / Rq['op_auc']) * (1 - fs) - (1 + fb)) * 100
    print(f'   （共同交易日 {Q["date"].nunique()} 天）')
    sq = report('独立池 + 981 市场代理', Rq)

    print('\n' + '=' * 86)
    print('§D 判定（判据与第一份完全相同，事前冻结）')
    print('=' * 86)
    n, d, net, t = s['n'], s['days'], s['net'], s['t']
    print(f'  独立池：n={n}  日={d}  费后={net:+.4f}%/腿  t={t:+.2f}')
    print(f'  参考（主面板 981 只，同期）：{REF_981:+.4f}%/腿')
    if n < MIN_LEGS or d < MIN_DAYS:
        v = '功效不足'
    elif net >= PASS_NET and abs(t) >= PASS_T:
        v = '✅ 通过'
    elif net < REJ_NET or abs(t) < REJ_T:
        v = '❌ 否决'
    else:
        v = '⚠️ 存疑'
    print(f'\n  ── 判定：{v} ──')
    print(f'  （独立池/主面板 净均比 = {net/REF_981:.2f}）')

    out = HERE / 'results_e0_stage13_indep_2026-09-22.json'
    out.write_text(json.dumps({
        'prereg': 'doc/experiment/2026-09-22_低开反转规则_样本外预注册.md §第二份',
        'seed': SEED, 'n_sample': int(len(sp)), 'n_fetched': int(P['code'].nunique()),
        'delisted_in_sample': int(sp['is_delisted'].sum()),
        'A_indep': s, 'C_mkt981': sq, 'verdict': v,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'sample'
    {'sample': cmd_sample, 'fetch': cmd_fetch, 'test': cmd_test}[cmd]()
