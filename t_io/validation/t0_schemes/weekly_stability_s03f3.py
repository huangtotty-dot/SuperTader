# -*- coding: utf-8 -*-
"""s0#3×F3 逐周稳定性检验 — 2026-09-22（任务A，月度粒度太粗的补充）。

数据：全部来自缓存（_cache_b7ff_legs.json + _cache_b7ff_z.json），不重算 z 矩阵。
口径：F3 = 信号日14:30 bar 的 s0#3 z < -1 时允许 B7 出手；费后净收益 = legs['net']。
分组：ISO 自然周（周一为起点），week = 'YYYY-Www'（如 '2026-W06'）。
edge 口径与逐月检验一致：edge = (F3 该周净均 − 对照全体净均) × 该周腿数。
"""
import json
import os
import sys
from collections import defaultdict
from datetime import date

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
LEGS = json.load(open(os.path.join(HERE, '_cache_b7ff_legs.json'), encoding='utf-8'))
ZMAP = json.load(open(os.path.join(HERE, '_cache_b7ff_z.json'), encoding='utf-8'))
OOS_START = '2026-06-01'
FID = 's0#3'


def iso_week(ds):
    d = date.fromisoformat(ds)
    y, w, _ = d.isocalendar()
    return f'{y}-W{w:02d}'


f3_legs, ctrl_legs = [], []
for l in LEGS:
    z = ZMAP.get(FID, {}).get(l['code'], {}).get(l['date'])
    rec = {'code': l['code'], 'date': l['date'], 'net': l['net'],
           'week': iso_week(l['date']), 'oos': l['oos'], 'z': z}
    ctrl_legs.append(rec)
    if z is not None and z < -1.0:
        f3_legs.append(rec)

assert len(f3_legs) == 82, f'F3 腿数 {len(f3_legs)} != 82，需排查'
assert len(ctrl_legs) == 421, f'对照腿数 {len(ctrl_legs)} != 421，需排查'

mean_ctrl = sum(l['net'] for l in ctrl_legs) / len(ctrl_legs) * 100
mean_f3 = sum(l['net'] for l in f3_legs) / len(f3_legs) * 100


def agg(legs):
    """按 ISO 周聚合 -> {week: {n, net_mean%, win, sum_net%}}"""
    g = defaultdict(list)
    for l in legs:
        g[l['week']].append(l['net'])
    out = {}
    for w in sorted(g):
        a = g[w]
        n = len(a)
        out[w] = {'n': n,
                  'net_mean%': round(sum(a) / n * 100, 4),
                  'win': round(sum(1 for x in a if x > 0) / n, 4),
                  'sum_net%': round(sum(a) * 100, 4)}
    return out


f3_w, ctrl_w = agg(f3_legs), agg(ctrl_legs)
total_f3 = sum(l['net'] for l in f3_legs) * 100
total_ctrl = sum(l['net'] for l in ctrl_legs) * 100
total_edge = round(total_f3 - mean_ctrl * len(f3_legs), 4)

weeks = sorted(set(list(f3_w) + list(ctrl_w)))
rows = []
for w in weeks:
    f, c = f3_w.get(w), ctrl_w.get(w)
    row = {'week': w,
           'period': 'OOS' if w >= iso_week(OOS_START) else 'IS',
           'f3': f, 'ctrl': c}
    if f:
        row['edge_contrib%'] = round((f['net_mean%'] - mean_ctrl) * f['n'], 4)
        row['thin'] = f['n'] < 5
    rows.append(row)

# ── 1) 正收益周占比 ──
f3_weeks_pos = [r for r in rows if r.get('f3') and r['f3']['net_mean%'] > 0]
f3_weeks_all = [r for r in rows if r.get('f3')]
pos_ratio = len(f3_weeks_pos) / len(f3_weeks_all)

# ── 2) edge 集中度：正 edge 周排序，前 3 周占正 edge 总量的比例 ──
pos_edge = sorted([r for r in f3_weeks_all if r['edge_contrib%'] > 0],
                  key=lambda r: r['edge_contrib%'], reverse=True)
pos_edge_total = sum(r['edge_contrib%'] for r in pos_edge)
top3_share = (round(sum(r['edge_contrib%'] for r in pos_edge[:3]) / pos_edge_total, 4)
              if pos_edge_total > 0 else None)
top3_weeks = [(r['week'], r['edge_contrib%']) for r in pos_edge[:3]]
top3_share_of_total_edge = (round(sum(r['edge_contrib%'] for r in pos_edge[:3]) / total_edge, 4)
                            if total_edge > 0 else None)

# ── 3) IS / OOS 分段周级别表现 ──
def segment(rows, period):
    sub = [r for r in rows if r.get('f3') and r['period'] == period]
    legs = [l for l in f3_legs if (l['oos'] if period == 'OOS' else not l['oos'])]
    if not sub or not legs:
        return None
    n = len(legs)
    pos = sum(1 for r in sub if r['f3']['net_mean%'] > 0)
    return {'weeks': len(sub), 'legs': n,
            'net_mean%': round(sum(l['net'] for l in legs) / n * 100, 4),
            'win': round(sum(1 for l in legs if l['net'] > 0) / n, 4),
            'pos_weeks': pos, 'pos_week_ratio': round(pos / len(sub), 4),
            'thin_weeks': sum(1 for r in sub if r['thin'])}


seg = {'IS': segment(rows, 'IS'), 'OOS': segment(rows, 'OOS')}

# ── 4) 连续负周最长连黑（按 F3 周净均正负计） ──
max_streak, cur, cur_seq, max_seq = 0, 0, [], []
for r in f3_weeks_all:
    if r['f3']['net_mean%'] <= 0:
        cur += 1
        cur_seq.append(r['week'])
        if cur > max_streak:
            max_streak, max_seq = cur, list(cur_seq)
    else:
        cur, cur_seq = 0, []

# ── 5) 结论规则 ──
# 正收益周占比 ≥55% 且 top3 正 edge 份额 ≤50% 且 OOS 净均>0 → 逐周稳定
# top3 正 edge 份额 >50% → 集中
# 其余 → 无法判 / 混合
if top3_share is not None and top3_share > 0.5:
    verdict = '集中：前3正edge周贡献 {:.1%} > 50%，edge 依赖少数周'.format(top3_share)
elif pos_ratio >= 0.55 and seg['OOS'] and seg['OOS']['net_mean%'] > 0:
    verdict = '逐周稳定：正收益周占比 {:.1%}，top3 份额 {:.1%}，OOS 同向为正'.format(
        pos_ratio, top3_share)
else:
    verdict = '无法判 / 混合：正收益周占比 {:.1%}，top3 份额 {}，需人工复核'.format(
        pos_ratio, f'{top3_share:.1%}' if top3_share is not None else 'NA')

result = {
    'meta': {'task': 's0#3×F3 逐周稳定性检验', 'experimenter': '量化实验检验员',
             'date': '2026-09-22', 'fid': FID,
             'expr': 'ts_min_w30(ts_delay_w20(tail30_ret(vwap)))',
             'variant': 'F3: z<-1 才允许 B7 出手', 'oos_start': OOS_START,
             'week_def': 'ISO 自然周（周一起）',
             'source': '缓存复用：_cache_b7ff_legs.json + _cache_b7ff_z.json（未重算 z 矩阵）'},
    'overall': {
        'f3': {'n': len(f3_legs), 'net_mean%': round(mean_f3, 4),
               'win': round(sum(1 for l in f3_legs if l['net'] > 0) / len(f3_legs), 4),
               'sum_net%': round(total_f3, 4)},
        'ctrl': {'n': len(ctrl_legs), 'net_mean%': round(mean_ctrl, 4),
                 'win': round(sum(1 for l in ctrl_legs if l['net'] > 0) / len(ctrl_legs), 4),
                 'sum_net%': round(total_ctrl, 4)},
        'total_edge_over_ctrl_mean%': total_edge,
        'lift_pp': round(mean_f3 - mean_ctrl, 4)},
    'weekly': rows,
    'analysis': {
        'f3_weeks_total': len(f3_weeks_all),
        'pos_week_ratio': round(pos_ratio, 4),
        'thin_weeks': sum(1 for r in f3_weeks_all if r['thin']),
        'top3_pos_edge_weeks': top3_weeks,
        'top3_share_of_pos_edge': top3_share,
        'top3_share_of_total_edge': top3_share_of_total_edge,
        'segments': seg,
        'max_neg_week_streak': {'len': max_streak, 'weeks': max_seq},
        'concentration_rule': '前3正edge周占全部正edge > 50% 视为集中',
        'verdict': verdict},
}
out = os.path.join(HERE, 'results_s03f3_weekly_2026-09-22.json')
json.dump(result, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# ── 控制台摘要 ──
print(f"F3 n={len(f3_legs)} 净均={mean_f3:+.4f}% | 对照 n={len(ctrl_legs)} 净均={mean_ctrl:+.4f}% | "
      f"总edge={total_edge:+.2f}pp·腿")
print(f"{'周':10s} {'期':4s} {'F3n':>4s} {'F3净均%':>9s} {'F3胜率':>7s} {'F3累计%':>9s} "
      f"{'对照n':>5s} {'对照净均%':>9s} {'edge贡献':>9s}")
for r in rows:
    f, c = r.get('f3'), r.get('ctrl')
    fs = f"{f['n']:4d} {f['net_mean%']:+9.4f} {f['win']:7.3f} {f['sum_net%']:+9.3f}" if f else '   -         -       -         -'
    cs = f"{c['n']:5d} {c['net_mean%']:+9.4f}" if c else '    -         -'
    ec = f"{r['edge_contrib%']:+9.3f}" if r.get('edge_contrib%') is not None else '        -'
    thin = ' [样本薄]' if r.get('thin') else ''
    print(f"{r['week']:10s} {r['period']:4s} {fs} {cs} {ec}{thin}")
print(f"\nF3 覆盖周数={len(f3_weeks_all)}，正收益周占比={pos_ratio:.1%}，样本薄周={sum(1 for r in f3_weeks_all if r['thin'])}")
print(f"前3正edge周={top3_weeks}，占正edge总量={top3_share:.1%}，占总edge={top3_share_of_total_edge:.1%}" if top3_share else '无正edge周')
print(f"IS: {seg['IS']}")
print(f"OOS: {seg['OOS']}")
print(f"最长连黑={max_streak}周 {max_seq}")
print(f"结论: {verdict}")
print(f"-> {out}")
