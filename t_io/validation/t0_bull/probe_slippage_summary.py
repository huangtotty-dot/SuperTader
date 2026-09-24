# -*- coding: utf-8 -*-
"""滑点探针的判读工具（口径与判据**逐条**来自预注册文档，不在这里重新发明）。

预注册：`doc/experiment/2026-09-24_真实买侧滑点测量_预注册.md`
数据：`t_io/validation/slippage_probe/probe_*.jsonl`（由 `execution/auto/probe_slippage.py` 写）

## 口径（§1）
- **买侧滑点** = `成交价 / 09:31 首根 60s bar 的 open − 1`（用委托回报的成交价）
- **伪影探测器** = `成交价 / 前收 − 1`（§5 结局 C 用）
- 卖侧（参考）= `10:00 成交价 / 10:00 参考价 − 1`

## 判据（§5，**先写死，不许事后调**）
  通过  买侧滑点中位 ≤ 0.35pp
  否决  买侧滑点中位 ≥ 1pp
  测不出 `成交价/前收−1` 的分布与「滑点比例」一致（或与 09:31 bar 无关）⇒ 模拟盘也是 bar 撮合
  中间区 0.35~1pp ⇒ 不判，扩样本到 ~100 笔
只统计**做 T 的腿**（`T_BUY`），**不含第 0 天建底仓**（`BASE_BUY`）。

用法：python probe_slippage_summary.py [--dir t_io/validation/slippage_probe]
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[3]

PASS_BAR = 0.35      # pp
FAIL_BAR = 1.00      # pp
SLIPPAGE_RATIO = 0.0001


def load(d: Path):
    recs = []
    for f in sorted(d.glob('probe_*.jsonl')):
        for line in f.open(encoding='utf-8'):
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    return recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=str(ROOT / 't_io' / 'validation' / 'slippage_probe'))
    a = ap.parse_args()
    d = Path(a.dir)
    if not d.exists():
        print(f'尚无数据目录 {d}（探针还没跑）'); return
    recs = load(d)
    if not recs:
        print(f'{d} 里没有记录（探针还没跑出第一天？）'); return

    # 委托回报：取 status==3（全部成交）的成交价
    fills: dict = {}
    for r in recs:
        if r.get('event') != 'ORDER_STATUS':
            continue
        if int(r.get('status') or 0) != 3:
            continue
        key = (r.get('code'), r.get('date'), r.get('side'))
        px = float(r.get('fill_px') or 0)
        if px > 0:
            fills.setdefault(key, []).append((px, float(r.get('qty') or 0)))
    # 同键多条（部分成交）⇒ 按量加权
    fpx = {k: sum(p * q for p, q in v) / max(sum(q for _, q in v), 1e-9)
           for k, v in fills.items()}

    legs, base = [], []
    for r in recs:
        e = r.get('event')
        if e not in ('T_BUY', 'BASE_BUY'):
            continue
        c, day = r.get('code'), r.get('date')
        ref, pc = float(r.get('ref_open') or 0), float(r.get('prev_close') or 0)
        fp = fpx.get((c, day, 'BUY'))
        row = {'code': c, 'date': day, 'ref_open': ref, 'prev_close': pc, 'fill_px': fp}
        (base if e == 'BASE_BUY' else legs).append(row)

    got = [r for r in legs if r['fill_px'] and r['ref_open'] > 0]
    print(f'记录 {len(recs)} 条 / 文件 {len(glob.glob(str(d / "probe_*.jsonl")))} 天')
    print(f'做T买腿 {len(legs)} 条，其中取到成交回报 {len(got)} 条'
          f'（建底仓 {len(base)} 条，不计入）')
    if not got:
        print('还没有可判的做T腿。若已过第 0 天，检查 ORDER_STATUS 是否落盘 / 字段名是否正确。')
        return

    slip = [(r['fill_px'] / r['ref_open'] - 1) * 100 for r in got]
    artif = [((r['fill_px'] / r['prev_close'] - 1) * 100 if r['prev_close'] else float('nan'))
             for r in got]

    def pct(v, q):
        v = sorted(x for x in v if x == x)
        return v[min(len(v) - 1, max(0, int(len(v) * q)))] if v else float('nan')

    med = st.median(slip)
    print(f'\n=== 买侧滑点（成交价/09:31 open − 1）n={len(slip)} ===')
    print(f'  中位 {med:+.3f}pp   均值 {st.fmean(slip):+.3f}pp   '
          f'p10 {pct(slip,0.10):+.3f}  p90 {pct(slip,0.90):+.3f}   '
          f'胜(≤0) {(sum(1 for x in slip if x <= 0))/len(slip):.1%}')

    am = st.median([x for x in artif if x == x])
    d_ratio = [abs(x - SLIPPAGE_RATIO * 100) for x in artif if x == x]
    print(f'\n=== 伪影探测器（成交价/前收 − 1）中位 {am:+.4f}pp ===')
    print(f'  与 slippage 比例({SLIPPAGE_RATIO*100:.4f}pp) 的偏离中位 '
          f'{st.median(d_ratio) if d_ratio else float("nan"):.4f}pp')

    print('\n=== 预注册判据（§5）===')
    if (d_ratio and st.median(d_ratio) < 0.01) or (abs(am) < 0.01):
        print('  ⚠️ **测不出（结局 C）**：成交价与前收的关系像 bar 撮合（≈slippage 比例），'
              '模拟盘也在按 bar 撮合 ⇒ 此路径测不出真实滑点，需另议（极小仓位真金实盘）。')
    elif med <= PASS_BAR:
        print(f'  ✅ **通过（结局 A）**：中位 {med:+.3f}pp ≤ {PASS_BAR}pp ⇒ 规则余量保住。')
    elif med >= FAIL_BAR:
        print(f'  ❌ **否决（结局 B）**：中位 {med:+.3f}pp ≥ {FAIL_BAR}pp ⇒ 优势归零，规则关闭。')
    else:
        print(f'  ⏸ **中间区**：中位 {med:+.3f}pp 落在 {PASS_BAR}~{FAIL_BAR}pp ⇒ 不判，'
              f'扩到 ~100 笔再判。')
    print(f'  （样本 {len(slip)} 笔；预注册要求 30 笔起步，sd 大则扩到 ~100。）')


if __name__ == '__main__':
    main()
