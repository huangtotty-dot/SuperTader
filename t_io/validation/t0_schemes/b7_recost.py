# -*- coding: utf-8 -*-
"""B7 隔夜反T 台账按新成本精确重算（2026-09-22）。

## 为什么需要
B7 是当前唯一进生产的做T策略，其 +0.6375%（对照）/ +1.457%（F3 过滤）都是用
旧成本 `卖 0.121% / 买 0.015%`（往返 0.136%，内含 2023 年前废止的 0.1% 印花税）
算出的。按 owner 实际费率（往返 0.06908%），**相对结论不变（同一常数平移到每条腿），
但绝对值会整体上移**，上线预期锚必须更新。

## 精确性
`net_of` 只用 (sell, buy) 两价：
    net = ( sell×(1−fs) − buy×(1+fb) ) / sell
`_cache_b7ff_legs.json` 存了 `sell` 但未存 `buy`（接回价 = 次日开盘）。
故从本地日线缓存取次日开盘补 `buy`，并**用旧成本反算 `net` 与该缓存逐腿比对**：
只有全部对上，才证明补出的 `buy` 与该实验口径一致，新成本数字才可信。

产出：`results_b7_recost_2026-09-22.json`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cost_model import fees, round_trip  # noqa: E402

FM = ROOT / 't_io' / 'validation' / 'factor_mining'
if str(FM) not in sys.path:
    sys.path.insert(0, str(FM))
import minute_data as md  # noqa: E402  与实验同一分钟数据层

HERE = Path(__file__).resolve().parent
LEGS = HERE / '_cache_b7ff_legs.json'
Z = HERE / '_cache_b7ff_z.json'


def next_open_map(code: str) -> dict:
    """{交易日: 当日首根 bar 开盘} —— 取自**分钟数据**，与 `sell` 同价格基准。

    ⚠️ 不能用 `t_io/cache/daily_kline/*.json`：那是前复权到「今天」的序列，
    与实验中分钟序列的价格基准不同源，两者相除会得到错误的 buy/sell 比
    （实测 421/421 腿偏离，588170 达 67pp —— 见 2026-09-22 首版的反验失败）。
    """
    df = md.load_minutes(code)
    if df is None or df.empty:
        return {}
    df = df.sort_values('time')
    d = df['time'].dt.strftime('%Y-%m-%d')
    return {k: float(v) for k, v in
            df.groupby(d)['open'].first().items()}


def main() -> None:
    legs = json.loads(LEGS.read_text(encoding='utf-8'))
    print(f'腿数 = {len(legs)}')

    o_map = {c: next_open_map(c) for c in {r['code'] for r in legs}}

    # 次日开盘：在所有交易日序列里取 date 之后最近的一天
    all_dates = {}
    for c in o_map:
        all_dates[c] = sorted(o_map[c])

    def buy_next(code: str, date: str):
        ds = all_dates[code]
        i = np.searchsorted(ds, date)
        j = i + 1 if i < len(ds) and ds[i] == date else i
        return o_map[code][ds[j]] if j < len(ds) else None

    fs_old, fb_old = fees('legacy')
    rec = []
    for r in legs:
        b = buy_next(r['code'], r['date'])
        if b is None or b <= 0:
            rec.append({**r, 'buy': None, 'net_old_recalc': None})
            continue
        # ⚠️ 缓存的 net 是**小数**（均值 0.0063746 = 台账 E1 的 0.6375%），不是百分数
        net_old = (r['sell'] * (1 - fs_old) - b * (1 + fb_old)) / r['sell']
        rec.append({**r, 'buy': b, 'net_old_recalc': net_old})

    ok = [x for x in rec if x['net_old_recalc'] is not None]
    dev = np.array([abs(x['net'] - x['net_old_recalc']) for x in ok])
    print(f'补出 buy 的腿 {len(ok)}/{len(legs)}')
    print(f'旧成本反算 vs 缓存 net: max|Δ| = {dev.max():.3e}  平均 {dev.mean():.3e}')
    if dev.max() > 1e-6:
        worst = max(ok, key=lambda x: abs(x['net'] - x['net_old_recalc']))
        print(f'⚠️ 口径不一致！最大偏离腿: {worst["code"]} {worst["date"]} '
              f'存 net={worst["net"]:.4f} 反算={worst["net_old_recalc"]:.4f} '
              f'sell={worst["sell"]} buy={worst["buy"]}')
        print('⇒ 补出的次日开盘与该实验口径不同，新成本数字不可用。')
        return

    print('✅ 反算逐腿一致 ⇒ 补出的 buy 口径正确，新成本重算精确\n')

    # z 缓存结构为 {factor: {code: {date: z}}}；F3 用的是过闸的 s0#3
    zmap = {}
    if Z.exists():
        zj = json.loads(Z.read_text(encoding='utf-8'))
        zmap = zj.get('s0#3', {}) if isinstance(zj, dict) else {}

    out = {}
    for venue in ('legacy', 'stock', 'etf'):
        fs, fb = fees(venue)
        nets = []
        for x in ok:
            n = (x['sell'] * (1 - fs) - x['buy'] * (1 + fb)) / x['sell']
            nets.append(n)
        nets = np.array(nets) * 100          # 转百分数供报告
        oos = np.array([x['oos'] for x in ok])
        # F3 过滤：信号日 14:30 z < −1
        if zmap:
            zv = [zmap.get(x['code'], {}).get(x['date']) for x in ok]
            f3 = np.array([z is not None and z < -1.0 for z in zv])
            n3 = nets[f3]
        else:
            f3, n3 = np.array([], bool), np.array([])
        out[venue] = {
            'E1_all': {'n': int(len(nets)), 'mean': round(float(nets.mean()), 4),
                       'win': round(float((nets > 0).mean()), 4)},
            'E1_IS': {'n': int((~oos).sum()), 'mean': round(float(nets[~oos].mean()), 4)},
            'E1_OOS': {'n': int(oos.sum()), 'mean': round(float(nets[oos].mean()), 4)},
            'F3': {'n': int(len(n3)),
                   'mean': round(float(n3.mean()), 4) if len(n3) else None,
                   'win': round(float((n3 > 0).mean()), 4) if len(n3) else None},
            'round_trip': round_trip(venue),
        }

    print(f"{'venue':8s}{'往返%':>8s}{'E1 n':>7s}{'E1净均%':>10s}{'E1胜率':>8s}"
          f"{'IS净均%':>10s}{'OOS净均%':>10s}{'F3 n':>7s}{'F3净均%':>10s}{'F3胜率':>8s}")
    def _f(x, w=10, p=4):
        return f'{x:>+{w}.{p}f}' if x is not None else f'{"—":>{w}}'

    for v in ('legacy', 'stock', 'etf'):
        o = out[v]
        print(f"{v:8s}{o['round_trip']*100:>8.4f}{o['E1_all']['n']:>7d}"
              f"{o['E1_all']['mean']:>+10.4f}{o['E1_all']['win']:>8.3f}"
              f"{o['E1_IS']['mean']:>+10.4f}{o['E1_OOS']['mean']:>+10.4f}"
              f"{o['F3']['n']:>7d}{_f(o['F3']['mean'])}{_f(o['F3']['win'], 8, 3)}")

    d = out['stock']['E1_all']['mean'] - out['legacy']['E1_all']['mean']
    print(f'\n⇒ 换新成本后 B7 对照净均上移 {d:+.4f}pp/腿'
          f'（{out["legacy"]["E1_all"]["mean"]:+.4f}% → {out["stock"]["E1_all"]["mean"]:+.4f}%）')
    print(f'   F3 过滤层净均 {out["legacy"]["F3"]["mean"]:+.4f}% → '
          f'{out["stock"]["F3"]["mean"]:+.4f}%（相对提升 '
          f'{out["stock"]["F3"]["mean"]-out["stock"]["E1_all"]["mean"]:+.4f}pp，'
          f'旧口径 {out["legacy"]["F3"]["mean"]-out["legacy"]["E1_all"]["mean"]:+.4f}pp —— 不变）')

    p = HERE / 'results_b7_recost_2026-09-22.json'
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {p}')


if __name__ == '__main__':
    main()
