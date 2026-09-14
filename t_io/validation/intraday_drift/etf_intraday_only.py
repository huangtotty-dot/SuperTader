# -*- coding: utf-8 -*-
"""只做日内（开买收卖，隔夜空仓）vs 买入持有 — ETF 长样本（2026-09-14）。

动机：8 年指数样本证实「日内(开→收)为正、隔夜为负」且显著（上证 t=+3.69 / 隔夜 t=−4.49）。
个股双边 0.136%（含印花税）吃掉 +0.078%/日 → 净负；**ETF 免印花税**，故单测 ETF。
对照臂：买入持有（同期不动）。若"只做日内"系统性跑赢持有 → 效应可捕捉，而非只是趋势。

T+1 可执行性：A股 T+1，当日买入不可当日卖。但**卖的是底仓**（持仓未变），
即本系统既有的"底仓做T 一档"结构，故可执行。

用法：python t_io/validation/intraday_drift/etf_intraday_only.py
"""
import json
import os
import time
import urllib.request

import numpy as np

OUT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(OUT, 'kline_cache')
N = 2000
COST = 0.00036          # ETF 双边（佣金，免印花税）
ETFS = [('sh510300', '沪深300ETF'), ('sh510500', '中证500ETF'), ('sh510050', '上证50ETF'),
        ('sz159915', '创业板ETF'), ('sh512880', '证券ETF'), ('sh512480', '半导体ETF'),
        ('sh512660', '军工ETF'), ('sh512010', '医药ETF'), ('sh515180', '红利ETF'),
        ('sh588000', '科创50ETF'), ('sh512760', '芯片ETF'), ('sz159949', '创业板50ETF')]


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
    time.sleep(0.4)
    return rows


def main():
    print(f"{'ETF':16}{'n':>5}{'区间':>14}{'只做日内(净)':>14}{'买入持有':>11}{'超额':>11}")
    res = []
    for sym, name in ETFS:
        try:
            r = fetch(sym)
        except Exception as e:
            print(f"{name:16} 失败 {e}")
            continue
        if len(r) < 250:
            continue
        o = np.array([float(x[1]) for x in r], float)
        c = np.array([float(x[2]) for x in r], float)
        intra_net = (c / o - 1) - COST
        cum = (np.prod(1 + intra_net) - 1) * 100
        bh = (c[-1] / c[0] - 1) * 100
        res.append((name, len(r), cum, bh))
        print(f"{name:16}{len(r):>5}{r[0][0][2:]+'~'+r[-1][0][2:]:>14}{cum:>+13.1f}%{bh:>10.1f}%{cum-bh:>+10.1f}pp")
    a = np.array([x[2] for x in res]); b = np.array([x[3] for x in res])
    print(f"\n中位: 只做日内 {np.median(a):+.1f}%  买入持有 {np.median(b):+.1f}%")
    print(f"只做日内跑赢持有: {(a>b).sum()}/{len(res)} | 只做日内为正: {(a>0).sum()}/{len(res)}")
    json.dump({'cost': COST, 'rows': [{'name': n, 'n': k, 'intraday_only_pct': round(x, 1),
                                       'buyhold_pct': round(y, 1)} for n, k, x, y in res]},
              open(os.path.join(OUT, 'etf_intraday_only_2026-09-14.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
