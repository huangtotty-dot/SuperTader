# -*- coding: utf-8 -*-
"""B7 频率归因（离线，确定性）—— 回答「信号达标的日子漏在哪一步」。

## 为什么要有这个工具

GM 回测不适合做归因：服务端会崩(status 1018)、单账户不能并行、产物随运行漂移。
本项目在同一个问题上连续 7 轮结论出错，**病根全是「分析前没核对数据的来源与范围」**。
故本脚本把归因改成**离线、确定性**：
  输入 = 一次成功回测的 fill 事件（持仓轨迹）+ tail_buyback_forced 事件 + 本地 1min 数据
  输出 = 每个 S1 日的唯一分类
GM 只负责产出一次轨迹；之后所有分析都在这里做，秒级、可重复、不依赖服务端。

## 🔒 内置的防错护栏（针对上面那 7 次失误）

1. **覆盖范围核对（最重要）**：先从日志/[ir] 行取回测实际覆盖的日期区间，
   **S1 日列表严格限制在该区间内**。实测教训：曾拿「只跑 13 天」的产物去对「45 天」的 S1 列表，
   17 个"未达标"里绝大多数是根本没跑到的日子。
2. **事件 sink 核对**：b7_* 在主 sink(backtrace.jsonl)，tail_buyback_forced 在 events_*.jsonl；
   两处都读，且打印各自计数。教训：曾因 `most_common(8)` 截断而误判"没有 b7 事件"。
3. **播种 fill 的日期归属**：底仓播种带**墙上时钟**日期（如 2026-09-17），会被 `d0<=day` 静默滤掉
   → 持仓从 0 起算、全部误判"仓不足"。此处统一归到回测首日。
4. **产物新鲜度**：打印产物时间戳与 mtime，提醒调用方确认是本轮、且上一轮已退出（单账户不可并行）。

## 分类（每 S1 日至多一类）

  ① 成交                  —— b7_overnight_sell 命中
  ② 钩子后拦截            —— b7_guard_block 命中（带 reason）
  ③ 钩子前被尾盘回补挡    —— 无 b7 事件，但同日同票有 14:50~14:55 的 tail_buyback_forced
  ④ 持仓不足(< base_ref)  —— 无 b7 事件，且当日收盘持仓 < 目标底仓（无仓可卖）
  ⑤ 引擎 t30 未达标       —— 其余（信号在引擎口径下 < 1%）

用法：python t_io/validation/t0_schemes/b7_gate_attribution.py --dir <回测产物目录> [--log <回测日志>]
"""
import argparse
import collections
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
for _p in (ROOT, os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402


def _covered_range(log_path):
    """回测实际覆盖的日期区间 —— 从日志的 [ir] <date> 行取（不信任 --end 参数）。"""
    if not log_path or not os.path.exists(log_path):
        return None, None, 0
    raw = open(log_path, 'rb').read()
    for enc in ('gbk', 'utf-8'):
        try:
            txt = raw.decode(enc, 'ignore')
            break
        except Exception:
            continue
    ds = re.findall(r'\[ir\]\s+(20\d\d-\d\d-\d\d)', txt)
    return (min(ds), max(ds), len(set(ds))) if ds else (None, None, 0)


def _read_events(d, name):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p, encoding='utf-8'):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True, help='回测产物目录')
    ap.add_argument('--log', default=None, help='回测日志（用于核对覆盖范围）')
    ap.add_argument('--codes', default='588170,600481,002451,000988,600176,603667,300054,002639,300153')
    args = ap.parse_args()
    d = args.dir
    codes = args.codes.split(',')

    print('=' * 78)
    print(f'B7 频率归因 · {d}')
    for f in sorted(os.listdir(d)):
        fp = os.path.join(d, f)
        import datetime as _dt
        print(f'  产物 {f:34} {os.path.getsize(fp):>10,} B  '
              f'{_dt.datetime.fromtimestamp(os.path.getmtime(fp)):%Y-%m-%d %H:%M:%S}')
    cov = _covered_range(args.log)
    print(f'🔒 日志覆盖范围核对: {cov[0]} ~ {cov[1]}（{cov[2]} 个交易日）')
    if not cov[0]:
        print('  ⚠️ 取不到覆盖范围 → 拒绝分析（防"拿部分数据对全窗口清单"的老错）')
        return
    print('=' * 78)

    # 主 sink：b7_* 事件
    bt = _read_events(d, 'backtrace.jsonl')
    sells, blocks = set(), {}
    for e in bt:
        t = e.get('event')
        if t == 'b7_overnight_sell':
            sells.add((e.get('code'), str(e.get('time'))[:10]))
        elif t == 'b7_guard_block':
            blocks[(e.get('code'), str(e.get('time'))[:10])] = e.get('reason')
    # 次 sink：tail_buyback_forced
    forced = collections.defaultdict(list)
    for fn in os.listdir(d):
        if fn.startswith('events_'):
            for e in _read_events(d, fn):
                if e.get('event') == 'tail_buyback_forced':
                    forced[(e.get('code'), str(e.get('time'))[:10])].append(str(e.get('time'))[11:19])
    print(f'b7_overnight_sell={len(sells)}  b7_guard_block={len(blocks)}  '
          f'tail_buyback_forced(按日)={len(forced)}')

    # 持仓轨迹（播种 fill 归到回测首日）
    pos = collections.defaultdict(list)
    for fn in os.listdir(d):
        if not fn.startswith('events_'):
            continue
        for e in _read_events(d, fn):
            if e.get('event') == 'fill' and e.get('pos_after') is not None:
                day = str(e.get('time'))[:10]
                if day.startswith('2026-09'):        # 播种用墙上时钟 → 归首日
                    day = cov[0]
                pos[e.get('code')].append((day, int(e['pos_after'])))

    def pos_on(c, day):
        v = 0
        for d0, pv in sorted(pos.get(c, [])):
            if d0 <= day:
                v = pv
        return v

    base = {}                                        # 目标底仓：首笔 fill 的量
    for fn in os.listdir(d):
        if fn.startswith('events_'):
            for e in _read_events(d, fn):
                if e.get('event') == 'fill' and e.get('side') == 'BUY':
                    base.setdefault(e.get('code'), int(e.get('qty') or 0))

    # S1 日（本地口径 tail30>1%），**限制在覆盖区间内**
    rows = []
    for c in codes:
        try:
            dates, merged, _ = v2.merge_days(c)
        except Exception:
            continue
        ds = [x for x in dates if cov[0] <= x <= cov[1]]
        for i, x in enumerate(ds):
            if i + 1 >= len(ds):
                continue

            def ca(day, h):
                for b in day:
                    if b['t'] == h:
                        return b['c']
            a1 = ca(merged[x], '14:30')
            a2 = ca(merged[x], '14:55')
            if not (a1 and a2) or a2 / a1 - 1 <= 0.01:
                continue
            rows.append((c, x, (merged[ds[i + 1]][0]['o'] / a2 - 1) * 100))

    cat = collections.defaultdict(list)
    for c, day, gap in rows:
        k = (c, day)
        if k in sells:
            cl = '① 成交'
        elif k in blocks:
            cl = '② 钩子后拦截: ' + str(blocks[k])
        elif forced.get(k):
            cl = '③ 钩子前被尾盘回补挡'
        elif base.get(c) and pos_on(c, day) < base[c]:
            cl = '④ 持仓不足(<base_ref)'
        else:
            cl = '⑤ 引擎 t30 未达标'
        cat[cl].append(gap)

    n = len(rows)
    print(f'\n覆盖区间内 tail30>1% 日: n={n}')
    print(f'{"分类":34}{"n":>4}{"占比":>7}{"缺口均":>10}')
    for k, v in sorted(cat.items(), key=lambda x: -len(x[1])):
        print(f'  {k:32}{len(v):>4}{100 * len(v) / max(n, 1):>6.0f}%{sum(v) / len(v):>+10.3f}%')
    print('\n⚠️ 止损/趋势退出清仓的票，其"持仓不足"属设计行为，非 B7 缺陷。')


if __name__ == '__main__':
    main()
