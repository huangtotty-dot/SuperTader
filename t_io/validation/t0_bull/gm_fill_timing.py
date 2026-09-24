# -*- coding: utf-8 -*-
"""定位掘金回测的**成交落在哪一根 60s bar**（2026-09-23）。

## 为什么需要它
GM 回测里 OGR 买腿的成交价比「09:31 首根 bar 的 open」高 **+1.2~1.9%**（120~190bp）。
而离线 stage9 用 39 票 × 1 年 1min 实测：低开票「竞价→09:31」的成本只有
**中位 +2.2bp、p90 +70bp**（`results_e0_stage9_exec_2026-09-22.json`）。
GM 那个量级超出离线 p90 两到三倍 ⇒ 不像真实滑点，更像**回测撮合时点**的产物：
`backtest_holdings.py` 传 `backtest_match_mode=1`，若其语义是「按**下一根** bar 开盘价撮合」，
则回测把「实盘的秒级成交」错算成「延迟一分钟成交」，会把这条规则的执行成本**严重高估**。

本脚本把每条腿的 `fill_px` / `ref_px` 与当日 09:30~09:35 各根 bar 的 open/close 对齐，
用「匹配率 + 中位偏离」把「成交到底发生在哪根 bar」钉死 —— 从而判定
**GM 的数字是真实滑点还是撮合伪影**。

## 用法
    # 只解析本地审计（不连 GM，可随时跑）
    python gm_fill_timing.py <回测输出目录> --no-gm
    # 连 GM 拉 bar 对齐（⚠️ 必须在回测**结束**后跑，避免与回测抢终端）
    python gm_fill_timing.py <回测输出目录> [--limit 12]

判读：
  · `fill` 与某根 bar 的 open/close **匹配率接近 100%** ⇒ 命中该根 ⇒ 撮合时点被钉死。
  · 若命中的是 **09:32 的 open** ⇒ 「延迟一分钟成交」⇒ 回测高估执行成本，实盘不成立。
  · 若哪根都不匹配 ⇒ fill 价不是任何 bar 的 OHLC（另有撮合/滑点规则），需再查。
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[3]
for _p in (str(ROOT), str(ROOT / 'execution' / 'auto'), str(ROOT / 'execution' / 'auto' / '_gm')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TOL = 0.0005          # 5bp：判定「命中该 bar 字段」
SLOTS = ('09:30:00', '09:31:00', '09:32:00', '09:33:00', '09:34:00')
FIELDS = 'symbol,eob,open,high,low,close,volume,amount'


def load_legs(d: Path):
    """从审计取腿：{code, gm_symbol, date, ref_px, fill_px, kind}（kind: buy|sell）。"""
    out = []
    for line in (d / 'backtrace.jsonl').open(encoding='utf-8'):
        if '"ogr_buy"' not in line and '"ogr_sell"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        e = o.get('event')
        if e == 'ogr_buy':
            out.append({'kind': 'buy', 'code': o.get('code'),
                        'gm': o.get('gm_symbol'), 'date': str(o.get('time'))[:10],
                        'ref_px': o.get('ref_px'), 'fill_px': o.get('fill_px')})
        elif e == 'ogr_sell':
            out.append({'kind': 'sell', 'code': o.get('code'),
                        'gm': o.get('gm_symbol'), 'date': str(o.get('time'))[:10],
                        'ref_px': o.get('ref_px'), 'fill_px': o.get('fill_px')})
    return out


def fetch_bars(gm_symbol: str, date: str):
    """当日 09:40 前的 60s bars，键为 eob 的 HH:MM:SS。"""
    from gm.api import history_n, ADJUST_PREV
    his = history_n(symbol=gm_symbol, frequency='60s', count=12,
                    fields=FIELDS, fill_missing='Previous', adjust=ADJUST_PREV,
                    end_time=f'{date} 09:40:00')
    if not his:
        return {}
    return {str(b['eob'])[11:19]: {k: (float(b[k]) if b.get(k) is not None else None)
                                  for k in ('open', 'high', 'low', 'close')}
            for b in his}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--limit', type=int, default=12, help='最多查几个 (票,日) 组合')
    ap.add_argument('--no-gm', action='store_true', help='只解析本地审计，不连 GM')
    a = ap.parse_args()
    d = Path(a.outdir)
    legs = load_legs(d)
    if not legs:
        print(f'{d.name}: 审计里没有 ogr_buy/ogr_sell（该轮 OGR 未触发或被跳过）'); return

    by = collections.defaultdict(list)
    for L in legs:
        by[(L['gm'], L['date'])].append(L)
    n_fill = sum(1 for L in legs if L['fill_px'])
    print(f'{d.name}: 腿 {len(legs)} 条（其中有真实成交价 {n_fill} 条）'
          f'  涉及 {len(by)} 个 (票,日)')
    print(f'  ref_px 覆盖: {sum(1 for L in legs if L["ref_px"])}/{len(legs)}'
          f'  fill_px 覆盖: {n_fill}/{len(legs)}')
    for L in legs[:5]:
        print(f'  样 {L["kind"]:4s} {L["code"]} {L["date"]}  ref={L["ref_px"]}  fill={L["fill_px"]}')
    if a.no_gm:
        print('\n[--no-gm] 仅本地解析；连 GM 对齐请去掉该参数（回测结束后再跑）')
        return

    from utils.gm_token import load_token
    from gm.api import set_token
    set_token(load_token())

    # 统计：每个 (bar 时点, 字段) 上「命中率」与「中位偏离」
    hit = collections.Counter()
    dev = collections.defaultdict(list)
    miss = 0
    for (gm_sym, date), group in sorted(by.items())[:a.limit]:
        try:
            bars = fetch_bars(gm_sym, date)
        except Exception as e:
            print(f'  [skip] {gm_sym} {date} 拉 bar 失败: {e}'); continue
        if not bars:
            print(f'  [skip] {gm_sym} {date} 无 bar'); continue
        for L in group:
            fp = L['fill_px']
            if not fp:
                continue
            best = None
            for slot in SLOTS:
                b = bars.get(slot)
                if not b:
                    continue
                for f in ('open', 'close', 'high', 'low'):
                    v = b.get(f)
                    if not v:
                        continue
                    r = fp / v - 1
                    dev[(slot, f)].append(r * 100)
                    if abs(r) <= TOL:
                        hit[(slot, f)] += 1
                        if best is None:
                            best = (slot, f)
            if best is None:
                miss += 1
            # ref 价也定位一下（看它到底是不是某根 bar 的价）
            rp = L['ref_px']
            if rp:
                for slot in SLOTS:
                    b = bars.get(slot)
                    if not b or not b.get('open'):
                        continue
                    r = rp / b['open'] - 1
                    if abs(r) <= TOL:
                        hit[(slot, 'open(ref)')] += 1
                        break
    n = sum(1 for L in legs if L['fill_px'])
    print(f'\n=== 成交价落在哪根 bar（±5bp 命中率，分母 {n} 条有价腿）===')
    if not hit:
        print('  无任何命中 ⇒ fill 价不等于这些 bar 的 OHLC（另有撮合/滑点规则）')
    for (slot, f), c in sorted(hit.items(), key=lambda kv: -kv[1]):
        med = statistics.median(dev[(slot, f)]) if dev[(slot, f)] else float('nan')
        print(f'  {slot} {f:10s} 命中 {c:3d}/{n} = {c / n:6.1%}   中位偏离 {med:+.4f}%')
    print(f'  哪根都不命中: {miss} 条')
    print('\n判读：命中率接近 100% 的那一格 = 撮合时点 ⇒ 据此判定'
          '「延迟一分钟」是回测伪影还是真实滑点。')


if __name__ == '__main__':
    main()
