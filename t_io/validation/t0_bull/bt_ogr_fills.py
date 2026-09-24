# -*- coding: utf-8 -*-
"""掘金回测核查（成交口径）：OGR 每腿用**真实成交价**核算（2026-09-23）。

## 为什么必须做这一步
`bt_ogr_review.py` 的口径A 用审计 `ref_px`（09:31 首根 open / 10:00 价）算收益，
但它**不等于实际成交价**；交叉核对显示实际成交系统性更差。
要判断规则**真实**的每腿收益（含滑点/撮合），必须用真实成交价。

## 两个价从哪来
2026-09-23 起 `ogr_buy` / `ogr_sell` 只在**成交回调**（status==3）里落账——
被拒的卖单落 `ogr_sell_rejected` 且**腿保留**（不再被误记成已平腿）。
事件自带 `fill_px` ⇒ 直接可用；老产物无 `fill_px` 时回退到事件桥配对
（按 (code,side,qty) 配 order→fill）。

## 判据（事前冻结）
见 `doc/experiment/2026-09-22_低开反转规则_样本外预注册.md` §3。主指标 =
**费后净均收益/腿**，标准误按**日聚类** `se = sqrt(Σ_day(Σ_{i∈day} x_i)²)/N`：

  通过    净均 ≥ +0.20%/腿 且 |t| ≥ 2
  否决    净均 < 0 或 |t| < 1
  存疑    其余
  功效不足 腿数 < 500 或覆盖交易日 < 100（**本回测几乎必然落在这一档**，
          因该池仅 ~20 只 × ~30 个 OGR 日）

⚠️ 有两个**不同**的参照，别混（2026-09-23 我踩过一次，见文件内常量处的注释）：
  ① **本篮子靶子** = Stage17 §2 离线值 +1.1558%/腿（竞价口径，n=223/36日/t=2.64），
     扣 stage9 的「竞价→09:31」18.688bp ⇒ **+0.9689%/腿**。这是 GM 这一轮该对上的数。
     它**不是**「来路不明」——曾被我误删，实际是本篮子唯一正确的对照。
  ② 面板级冻结判据：门槛 +0.20%/腿 且 |t|≥2，幂下限 500 腿/100 日。
     本篮子 n≈200 ⇒ 按 ② 必判「功效不足」，② 只用于 981 主面板那种大样本。

用法：python bt_ogr_fills.py <回测输出目录> [--since --until]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.cost_model import fees                     # noqa: E402

FS, FB = fees('stock')
# ── 两个不同的参照，别混（2026-09-23 踩过一次）───────────────────────────
# ① 本篮子靶子（Stage17 §2，离线、**集合竞价入场**口径）：+1.1558%/腿
#    （n=223 / 36 日 / t=2.64 —— 见 tmp/ogr_basket_expectation.py 只读重算）。
#    ⚠️ 它**不是**「来路不明」：这正是本篮子唯一正确的对照值。
#    扣掉 stage9 实测的「竞价→09:31」18.688pp ⇒ 与 GM 的 09:31 限价入场可比 = +0.9689%/腿。
# ② 冻结判据（981 主面板 / 6.2 年样本外 / 费后 / 竞价口径）：门槛 +0.20%/腿 且 |t|≥2，
#    幂下限 腿数≥500 且 日数≥100。基准 +0.3385%/腿 只作面板级参照 ——
#    本篮子 n≈200 远小于 500 ⇒ 按 ② 必然判「功效不足」，故 GM 这一轮只能与 ① 比。
BASKET_REF_AUC = 1.1558                     # %/腿，竞价入场 + **全样本中位**代理（Stage17 §2）
STAGE9_AUC_TO_0931 = 0.18688                # pp/腿，竞价→09:31（stage9 V1−V4）
# ★ 本轮实跑对上的靶子：竞价入场 + **L20 代理**（Stage18 §4，n=205/35日/t=1.98）。
#   为什么不是 1.1558：L20 代理是修好后 L4 实际用的口径（全样本中位在实盘不可得）。
BASKET_REF = 0.9864
# 供参照（都不再是主靶子）：1.1558 = 全样本代理；0.9689 = 竞价扣 18.7bp 的 09:31 口径
PASS_BAR = 0.20            # %/腿，费后门槛（面板级冻结判据）
PANEL_NET_REF = 0.3385     # %/腿，费后；面板样本外 2019-01~2025-03 n=61403 t=8.07
MIN_LEGS, MIN_DAYS = 500, 100
# 已知在掘金数据集里价格不连续的票，默认剔除（可 --exclude 覆盖）：
# 301396 —— 3/30 首根 open 111.91 vs prev_close 194.99（gap −42.6%），
# 且日志 `LIMIT_DATA_BAD ... up=153.54 pre_close=127.95` 与审计 prev_close 179.27 自相矛盾。
DATA_BAD_CODES = {'301396'}


def load_events(d: Path):
    """按顺序返回 [(kind, code, side, qty, price)]，kind ∈ {order, fill}。"""
    seq = []
    for f in sorted(d.glob('events_*.jsonl')):
        for line in f.open(encoding='utf-8'):
            if '"order"' not in line and '"fill"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            e = o.get('event')
            if e in ('order', 'fill'):
                seq.append((e, o.get('code'), o.get('side'),
                            int(o.get('qty') or 0), float(o.get('price') or 0)))
    return seq


def pair_fills(seq, side, qty, ref_px):
    """老产物回退路径：先按 (code,side,qty,price≈ref) 锚 order，再取下一条同键 fill。"""
    for i, (k, c, s, q, p) in enumerate(seq):
        if k != 'order' or s != side or q != qty:
            continue
        if ref_px > 0 and abs(p / ref_px - 1) > 0.02:
            continue
        for j in range(i + 1, min(i + 40, len(seq))):
            k2, c2, s2, q2, p2 = seq[j]
            if k2 == 'fill' and c2 == c and s2 == side and q2 == qty:
                return p2
    return None


def load_legs(d: Path):
    """读审计：返回 (legs, audit_kinds)。legs = 已平腿 [{code,date,buy_px,sell_px,...}]。"""
    buys, sells, rej, kinds = {}, [], collections.Counter(), collections.Counter()
    for line in (d / 'backtrace.jsonl').open(encoding='utf-8'):
        if '"ogr_' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        e = o.get('event') or ''
        if not e.startswith('ogr_'):
            continue
        kinds[e] += 1
        day = str(o.get('time'))[:10]
        if e == 'ogr_buy':
            buys[(o['code'], day)] = o
        elif e == 'ogr_sell':
            sells.append((o['code'], day, o.get('buy_px'), o.get('ref_px'),
                          int(o.get('leg_qty') or o.get('qty') or 0), o.get('fill_px')))
        elif e == 'ogr_sell_rejected':
            rej[(o['code'], day)] = o
    legs = []
    for c, day, buy_px, sell_ref, q, sell_fill in sells:
        b = buys.get((c, day)) or {}
        legs.append({'code': c, 'date': day,
                     'buy_ref': b.get('ref_px') or buy_px, 'sell_ref': sell_ref,
                     'buy_fill': b.get('fill_px'), 'sell_fill': sell_fill,
                     'qty': q})
    return legs, kinds, rej, buys


def net(bp, sp):
    """费后净收益 %（买 bp 卖 sp）。"""
    return ((sp / bp) * (1 - FS) - (1 + FB)) * 100


def stat(x, days):
    """日聚类标准误 + t + 三态判定。"""
    x = np.asarray(list(x), dtype=float)
    n = len(x)
    if n == 0:
        return {'n': 0, 'days': 0, 'mean': float('nan'), 'se': float('nan'),
                't': float('nan'), 'verdict': '无样本'}
    by_day = collections.defaultdict(float)
    for xi, dd in zip(x, days):
        by_day[dd] += xi
    se = (sum(v * v for v in by_day.values()) ** 0.5) / n
    t = x.mean() / se if se > 0 else float('nan')
    nd = len(by_day)
    if n < MIN_LEGS or nd < MIN_DAYS:
        v = '功效不足'
    elif x.mean() >= PASS_BAR and abs(t) >= 2:
        v = '通过'
    elif x.mean() < 0 or abs(t) < 1:
        v = '否决'
    else:
        v = '存疑'
    return {'n': n, 'days': nd, 'mean': float(x.mean()), 'se': float(se),
            't': float(t), 'verdict': v}


def show(tag, s):
    if s['n'] == 0:
        print(f'{tag}: 无样本'); return
    print(f'{tag}: n={s["n"]} 日={s["days"]}  净均={s["mean"]:+.4f}%/腿  '
          f'se={s["se"]:.4f}  t={s["t"]:+.2f}   ⇒ **{s["verdict"]}**')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--since', default='2026-03-30')
    ap.add_argument('--until', default='2026-07-01')
    ap.add_argument('--exclude', default=','.join(sorted(DATA_BAD_CODES)),
                    help='逗号分隔的剔除码（默认剔除数据坏票 %s）；给空串则不剔除'
                         % ','.join(sorted(DATA_BAD_CODES)))
    a = ap.parse_args()
    d = Path(a.outdir)
    seq = load_events(d)
    legs, kinds, rej, buys = load_legs(d)

    # ── 可卖性诊断：买腿有几个真的平掉了 ──
    n_buy, n_sell_open = kinds.get('ogr_buy', 0), kinds.get('ogr_sell', 0)
    print(f'{d.name}: order/fill 事件 {len(seq)} 条')
    print(f'  ogr_buy={n_buy}  ogr_sell(真成交)={n_sell_open}  '
          f'ogr_sell_rejected={kinds.get("ogr_sell_rejected", 0)}  '
          f'ogr_sell_skip={kinds.get("ogr_sell_skip", 0)}  '
          f'ogr_buy_rejected={kinds.get("ogr_buy_rejected", 0)}  '
          f'ogr_sell_submit={kinds.get("ogr_sell_submit", 0)}')
    unclosed = max(0, n_buy - n_sell_open)
    print(f'  ⚠️ 买了但没平掉的腿={unclosed}'
          f'（其中 {len(rej)} 条有 status=8 拒单留痕 —— 见「可卖性」）')

    # ── 两腿价：优先用审计自带 fill_px，缺失回退事件桥配对 ──
    n_fb = 0
    for r in legs:
        if not (r['buy_fill'] and r['buy_fill'] > 0):
            n_fb += 1
            r['buy_fill'] = pair_fills(seq, 'BUY', int(buys.get((r['code'], r['date']),
                                                               {}).get('qty') or 0),
                                       float(r['buy_ref'] or 0))
        if not (r['sell_fill'] and r['sell_fill'] > 0):
            n_fb += 1
            r['sell_fill'] = pair_fills(seq, 'SELL', r['qty'], float(r['sell_ref'] or 0))
    got = [r for r in legs if r['buy_fill'] and r['sell_fill']
           and r['buy_fill'] > 0 and r['sell_fill'] > 0]
    print(f'  两腿都取到真实成交价: {len(got)}/{len(legs)}'
          + (f'（其中 {n_fb} 条腿靠事件桥回退配对）' if n_fb else ''))
    if not got:
        print('  配对不足 —— 见下方样本'); [print('  ', r) for r in legs[:3]]; return

    fill_x = np.array([net(r['buy_fill'], r['sell_fill']) for r in got])
    ref_x = np.array([net(r['buy_ref'], r['sell_ref']) for r in got])
    win = [(r, net(r['buy_fill'], r['sell_fill'])) for r in got
           if a.since <= r['date'] <= a.until]

    print(f'\n=== 成交口径（真实 fill）全窗口 ===')
    print(f'  净均={fill_x.mean():+.4f}%  中位={np.median(fill_x):+.4f}%  '
          f'胜率={(fill_x > 0).mean():.3f}')
    print(f'=== 同腿的 口径A（ref_px）===')
    print(f'  净均={ref_x.mean():+.4f}%  ⇒ **口径A 高估 {ref_x.mean() - fill_x.mean():+.4f}pp/腿**')

    print(f'\n=== 判定窗口 {a.since}~{a.until} ===')
    s = stat([v for _, v in win], [r['date'] for r, _ in win])
    show('  成交口径', s)
    sref = stat([net(r['buy_ref'], r['sell_ref']) for r, _ in win],
                [r['date'] for r, _ in win])
    show('  口径A   ', sref)
    if s['n']:
        print(f'  ★ 本篮子靶子(Stage17 §2, 09:31 入场)={BASKET_REF:+.4f}%/腿   '
              f'Δ(成交−靶子)={s["mean"] - BASKET_REF:+.4f}pp')
        print(f'    （靶子由竞价口径 {BASKET_REF_AUC:+.4f} − stage9 折价 {STAGE9_AUC_TO_0931:.4f} 得到；'
              f'面板级费后基准 {PANEL_NET_REF:+.4f}% 仅供参照，口径/样本都不同）')
    print(f'  冻结判据(面板级)：门槛 {PASS_BAR:+.2f}%/腿 且 |t|≥2；'
          f'幂下限 腿数≥{MIN_LEGS} 且 日数≥{MIN_DAYS}')

    # ── 剔除数据坏票后的口径（默认含 301396）──
    bad = {s.strip() for s in (a.exclude or '').split(',') if s.strip()}
    dropped = [r for r, _ in win if r['code'] in bad]
    if bad and dropped:
        w2 = [(r, v) for r, v in win if r['code'] not in bad]
        print(f'\n=== 剔除数据坏票 {sorted(bad)}（{len(dropped)} 腿）后 ===')
        show('  成交口径', stat([v for _, v in w2], [r['date'] for r, _ in w2]))
    elif bad:
        print(f'\n（窗口内没有 {sorted(bad)} 的腿，未做剔除）')

    # ── 买腿/卖腿各自的偏离（执行滞后诊断）──
    db = np.array([(r['buy_fill'] / r['buy_ref'] - 1) * 100 for r in got if r['buy_ref']])
    ds = np.array([(r['sell_fill'] / r['sell_ref'] - 1) * 100 for r in got if r['sell_ref']])
    print(f'\n  买腿 fill vs ref 偏离：中位={np.median(db):+.3f}%  p90={np.percentile(db, 90):+.3f}%')
    print(f'  卖腿 fill vs ref 偏离：中位={np.median(ds):+.3f}%  p10={np.percentile(ds, 10):+.3f}%')
    if len(ds) and np.allclose(ds, 0.0):
        # 老产物（2026-09-23 晚前的 gm_main）把 `ogr_sell.ref_px` 也写成了成交价 ⇒ 卖腿偏离
        # 恒 0、口径A 只剩买腿一项。不是「卖腿没滑点」，是量不出来。
        print('  ⚠️ 卖腿偏离恒为 0 ⇒ 该产物把 ref_px 写成了成交价，卖腿滑点不可测（非真实为 0）')
    (d / 'ogr_fill_review.json').write_text(json.dumps(
        legs, ensure_ascii=False, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
