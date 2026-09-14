# -*- coding: utf-8 -*-
"""条件化日内：按**开盘缺口**分桶，只在特定日子做「开买收卖」— 8 年 ETF 池（2026-09-15）。

## 为什么
用户约束：**A股 T+1、必须有底仓**（真 T+0 品种不适用）。
无条件日内已证不可交易（33 只全池，2bp 滑点即抹平）。
但**个股一年样本**显示缺口分桶里极端桶远超成本（1~3% 高开 +0.312%、<−3% 低开 +0.446%，
均为费后）→ 这是**条件化**做T：只在特定日子出手，降低往返次数、提高单位捕获。

## 本脚本（8 年 × 33 只 ETF，消除一年样本与选样偏差）
  gap = 开盘/昨收 − 1，分桶；每桶统计 日内(开→收) 净收益（滑点 1/2bp 两档）、按日 t、逐年稳定性。
  对照：同池「无条件日内」与「买入持有」。

用法：python t_io/validation/intraday_drift/gap_conditional.py
"""
import json
import os
import sys
import time
import urllib.request

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
OUT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(OUT, 'kline_cache')
N = 2000
COMMISSION = 0.0002
BUCKETS = [(-1.0, -0.05, '≤−5%'), (-0.05, -0.03, '−5~−3%'), (-0.03, -0.01, '−3~−1%'),
           (-0.01, 0.01, '−1~+1%'), (0.01, 0.03, '+1~+3%'), (0.03, 0.05, '+3~+5%'),
           (0.05, 1.0, '>+5%')]
ETFS = [('sh510300', '沪深300ETF'), ('sh510500', '中证500ETF'), ('sh510050', '上证50ETF'),
        ('sh510880', '红利ETF'), ('sh512100', '中证1000ETF'), ('sh588000', '科创50ETF'),
        ('sz159915', '创业板ETF'), ('sz159919', '沪深300ETF(深)'), ('sz159901', '深100ETF'),
        ('sh512880', '证券ETF'), ('sh512480', '半导体ETF'), ('sh512660', '军工ETF'),
        ('sh512010', '医药ETF'), ('sh512170', '医疗ETF'), ('sh512690', '酒ETF'),
        ('sh512800', '银行ETF'), ('sh512400', '有色金属ETF'), ('sh512980', '传媒ETF'),
        ('sh515030', '新能源车ETF'), ('sh515790', '光伏ETF'), ('sh516160', '新能源ETF'),
        ('sz159755', '电池ETF'), ('sh512200', '房地产ETF'), ('sz159928', '消费ETF'),
        ('sh516110', '汽车ETF'), ('sh512580', '环保ETF'), ('sz159865', '养殖ETF'),
        ('sh512760', '芯片ETF'), ('sh515180', '红利ETF(易)'), ('sh515120', '创新药ETF'),
        ('sh513050', '中概互联ETF'), ('sh513100', '纳指ETF'), ('sh518880', '黄金ETF')]


def fetch(sym):
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, f'{sym}_{N}.json')
    if os.path.exists(fp):
        return json.load(open(fp, encoding='utf-8'))
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{N},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.qq.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    v = (d.get("data") or {}).get(sym) or {}
    rows = v.get("day") or v.get("qfqday") or []
    json.dump(rows, open(fp, 'w', encoding='utf-8'))
    time.sleep(0.35)
    return rows


def main():
    recs = []      # (date, gap, day_net_at_0bp)
    for sym, name in ETFS:
        try:
            r = fetch(sym)
        except Exception:
            continue
        if len(r) < 250:
            continue
        d = np.array([x[0] for x in r])
        o = np.array([float(x[1]) for x in r], float)
        c = np.array([float(x[2]) for x in r], float)
        gap = np.concatenate([[np.nan], o[1:] / c[:-1] - 1])
        intra = c / o - 1
        for i in range(1, len(r)):
            if not np.isfinite(gap[i]):
                continue
            recs.append((d[i], float(gap[i]), float(intra[i]), name))
    print(f"[gap] ETF={len({r[3] for r in recs})} 样本={len(recs)} 日\n")

    for slip in (0.0001, 0.0002):
        cost = 2 * (COMMISSION + slip)
        print(f"===== 滑点 {slip*1e4:.0f}bp/边（双边成本 {cost*100:.3f}%）=====")
        print(f"{'gap 桶':>10}{'n':>7}{'净均%/日':>10}{'t':>8}{'年化%*':>9}{'正收益占比':>11}")
        for lo, hi, lab in BUCKETS:
            sub = [(dt, net - cost, nm) for dt, g, net, nm in recs if lo <= g < hi]
            if len(sub) < 100:
                continue
            byd = {}
            for dt, v, nm in sub:
                byd.setdefault(dt, []).append(v)
            dm = np.array([np.mean(v) for _, v in sorted(byd.items())])
            t = dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm))) if len(dm) > 2 else 0
            ann = dm.mean() * 243 * 100
            print(f"{lab:>10}{len(sub):>7}{np.mean([v for _, v, _ in sub])*100:>10.4f}"
                  f"{t:>8.2f}{ann:>9.1f}{np.mean([v > 0 for _, v, _ in sub])*100:>10.1f}%")
        print()

    # 对照：无条件
    for slip in (0.0001,):
        cost = 2 * (COMMISSION + slip)
        byd = {}
        for dt, g, net, nm in recs:
            byd.setdefault(dt, []).append(net - cost)
        dm = np.array([np.mean(v) for _, v in sorted(byd.items())])
        print(f"[对照] 无条件日内(1bp): 净均 {dm.mean()*100:+.4f}%/日  t={dm.mean()/(dm.std(ddof=1)/np.sqrt(len(dm))):.2f}"
              f"  年化 {dm.mean()*243*100:+.1f}%")
    json.dump({'n_sample': len(recs)}, open(os.path.join(OUT, 'gap_conditional_2026-09-15.json'), 'w'))


if __name__ == '__main__':
    main()
