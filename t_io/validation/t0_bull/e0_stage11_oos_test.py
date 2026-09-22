# -*- coding: utf-8 -*-
"""E0 Stage11：按预注册判据出样本外结论（2026-09-22）。

判据**已在** `doc/experiment/2026-09-22_低开反转规则_样本外预注册.md` 事前冻结，
本脚本只负责执行，不得改判据。

## 三段输出
  §1 仪器校验 —— 用**同一套归约代码**从 d540 缓存重算样本内数字，必须复现
                 +0.6404%/腿（t=4.61）。不复现则整条链路不可信，直接中止。
  §2 样本外 —— 2019-01-02 ~ 2025-03-28（预注册期间），规则原样不改。
  §3 预注册判定 —— 通过 / 否决 / 存疑，+ 次要描述（逐月、分时段、流动性变体）

用法：python e0_stage11_oos_test.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from e0_stage10_fetch_oos import reduce_day            # noqa: E402
from core.cost_model import fees                       # noqa: E402

SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
OOS_DIR = HERE / 'ts_oos'
FEE = 'stock'

# ── 预注册常量（冻结，勿改）──
RULE_MKT = 'mkt_gap < 0'
RULE_REL = -0.01
PASS_NET, PASS_T = 0.20, 2.0
REJ_NET, REJ_T = 0.0, 1.0
MIN_LEGS, MIN_DAYS = 500, 100
INSAMPLE_REF = {'net': 0.6404, 't': 4.61, 'n': 22225, 'days': 184}


def cse(x: np.ndarray, g: np.ndarray) -> float:
    if len(x) == 0:
        return float('nan')
    s = pd.DataFrame({'x': x, 'g': g}).groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / len(x))


def load_d540() -> pd.DataFrame:
    """样本内面板（仪器校验用），走与样本外**相同的 reduce_day**。"""
    rows = []
    for f in sorted(SRC.iterdir()):
        if not re.match(r'^\d{6}\.(SH|SZ)_30min_d540\.json$', f.name):
            continue
        code = f.name.split('.')[0]
        d = json.loads(f.read_text(encoding='utf-8'))
        bars = d if isinstance(d, list) else (d.get('rows') or d.get('data') or [])
        if not bars:
            continue
        df = pd.DataFrame(bars).rename(columns={'time': 'trade_time'})
        r = reduce_day(df, code)
        if not r.empty:
            rows.append(r)
    return pd.concat(rows, ignore_index=True)


def load_oos() -> pd.DataFrame:
    parts = []
    for f in sorted(OOS_DIR.glob('*.parquet')):
        parts.append(pd.read_parquet(f))
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def apply_rule(P: pd.DataFrame) -> pd.DataFrame:
    """预注册规则原样：mkt_gap<0 且 rel<=-1% → 09:30 竞价买 / 10:00 卖。"""
    P = P.copy()
    P['gap'] = P['op_auc'] / P['prev_close'] - 1
    P = P[np.isfinite(P['gap']) & (P['op_auc'] > 0) & (P['cl_1000'] > 0)]
    P['mkt_gap'] = P.groupby('date')['gap'].transform('median')
    P['rel'] = P['gap'] - P['mkt_gap']
    R = P[(P['mkt_gap'] < 0) & (P['rel'] <= RULE_REL)].copy()
    fs, fb = fees(FEE)
    R['net'] = ((R['cl_1000'] / R['op_auc']) * (1 - fs) - (1 + fb)) * 100
    return R


def report(tag: str, R: pd.DataFrame) -> dict:
    x, g = R['net'].values, R['date'].values
    se = cse(x, g)
    st = {'tag': tag, 'n': int(len(x)), 'days': int(R['date'].nunique()),
          'net': float(x.mean()) if len(x) else float('nan'),
          'se': se, 't': (x.mean() / se if se else float('nan')),
          'win': float((x > 0).mean()) if len(x) else float('nan')}
    print(f"  {tag:26s} n={st['n']:6d} 日={st['days']:4d}  "
          f"费后={st['net']:+.4f}%  SE={se:.4f}  t={st['t']:+.2f}  胜率={st['win']:.3f}")
    return st


def main() -> None:
    print('=' * 88)
    print('§1 仪器校验：用同一套归约从 d540 缓存重算样本内，须复现 +0.6404% / t=4.61')
    print('=' * 88)
    ins = load_d540()
    ins['prev_close'] = ins.groupby('code')['cl_1500'].shift(1)
    ins = ins[np.isfinite(ins['prev_close'])]
    Ri = apply_rule(ins)
    si = report('样本内(2025-03-31起)', Ri)
    ok_net = abs(si['net'] - INSAMPLE_REF['net']) < 0.02
    ok_n = abs(si['n'] - INSAMPLE_REF['n']) <= 30
    print(f"  → 复现检查: n={si['n']} (参考 {INSAMPLE_REF['n']}) "
          f"净均={si['net']:+.4f} (参考 +{INSAMPLE_REF['net']})  "
          f"{'✅通过' if (ok_net and ok_n) else '❌不符 —— 链路不可信，后续结论作废'}")

    print('\n' + '=' * 88)
    print('§2 样本外：2019-01-02 ~ 2025-03-28（预注册期间，规则原样未改）')
    print('=' * 88)
    oos = load_oos()
    if oos.empty:
        print('  无样本外数据（ts_oos/ 为空）—— 先跑 e0_stage10_fetch_oos.py')
        return
    dups = int(oos.duplicated(subset=['code', 'date']).sum())
    oos = oos.drop_duplicates(subset=['code', 'date'], keep='first')
    print(f'  票={oos["code"].nunique()}  原始日级行={len(oos)}  '
          f'重复行={dups}  区间 {oos["date"].min()} ~ {oos["date"].max()}')
    # prev_close 由本票序列内的 15:00 收盘给出（跨块连续，Stage10 已 concat 后归约）
    if 'prev_close' not in oos.columns:
        oos['prev_close'] = oos.groupby('code')['cl_1500'].shift(1)
    Ro = apply_rule(oos)
    so = report('样本外 全期间', Ro)

    print('\n  ── 分时段（描述，不参与判定）──')
    for lab, a, b in (('2019-2021', '2019-01-01', '2021-12-31'),
                      ('2022-2025', '2022-01-01', '2025-03-28')):
        s = Ro[(Ro['date'] >= a) & (Ro['date'] <= b)]
        if len(s):
            report(lab, s)

    print('\n  ── 逐月为正比例（描述）──')
    mo = Ro.assign(ym=Ro['date'].str[:7]).groupby('ym')['net'].agg(['mean', 'size'])
    pos = int((mo['mean'] > 0).sum())
    print(f'    {pos}/{len(mo)} 个月为正  ({pos/len(mo):.1%})  '
          f'最差月 {mo["mean"].min():+.4f}% ({mo["mean"].idxmin()})  '
          f'最好月 {mo["mean"].max():+.4f}% ({mo["mean"].idxmax()})')

    print('\n  ── 流动性变体（描述，不替代主判据）──')
    if 'amt' in Ro.columns:
        q = oos[oos['amt'].notna()].copy()
        q['gap'] = q['op_auc'] / q['prev_close'] - 1
        q = q[np.isfinite(q['gap'])]
        q['mkt_gap'] = q.groupby('date')['gap'].transform('median')
        q['rel'] = q['gap'] - q['mkt_gap']
        med = q.groupby('date')['amt'].transform('median')
        q['hi'] = q['amt'] >= med
        hit = q[(q['mkt_gap'] < 0) & (q['rel'] <= RULE_REL)]
        fs, fb = fees(FEE)
        hit = hit.assign(net=((hit['cl_1000'] / hit['op_auc']) * (1 - fs) - (1 + fb)) * 100)
        for lab, s in (('流动性≥当日中位', hit[hit['hi']]), ('流动性<中位', hit[~hit['hi']])):
            if len(s) > 50:
                report(lab, s)

    print('\n' + '=' * 88)
    print('§3 预注册判定')
    print('=' * 88)
    n, d, net, t = so['n'], so['days'], so['net'], so['t']
    print(f"  样本外：n={n}  日={d}  费后={net:+.4f}%/腿  t={t:+.2f}")
    print(f"  判据：通过需 净均≥+{PASS_NET}% 且 |t|≥{PASS_T}；"
          f"否决为 净均<{REJ_NET}% 或 |t|<{REJ_T}")
    if n < MIN_LEGS or d < MIN_DAYS:
        verdict = '功效不足（腿数或天数不够）'
    elif net >= PASS_NET and abs(t) >= PASS_T:
        verdict = '✅ 通过'
    elif net < REJ_NET or abs(t) < REJ_T:
        verdict = '❌ 否决'
    else:
        verdict = '⚠️ 存疑'
    print(f'\n  ── 判定：{verdict} ──')
    print(f"  （样本内参考：净均 +{INSAMPLE_REF['net']}%  t={INSAMPLE_REF['t']}；"
          f"样本外/样本内 净均比 = {net/INSAMPLE_REF['net']:.2f}）")

    out = HERE / 'results_e0_stage11_oos_2026-09-22.json'
    out.write_text(json.dumps({
        'prereg': 'doc/experiment/2026-09-22_低开反转规则_样本外预注册.md',
        'instrument_check': si, 'instrument_ok': bool(ok_net and ok_n),
        'oos': so, 'verdict': verdict,
        'monthly_pos_share': f'{pos}/{len(mo)}',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
