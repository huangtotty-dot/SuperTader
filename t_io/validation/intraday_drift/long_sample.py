# -*- coding: utf-8 -*-
"""日内漂移 · 长样本复核（2026-09-14）。

`run_experiment.py` 的一年样本给出 日内(开→收) +0.13%/日、年化 +31%，
但**单样本 t=1.20 不显著**、且后半段衰减到 1/3 —— 样本不足以确认。

本脚本把样本拉到 **2000 交易日（≈8.2 年，腾讯日K 上限）**，只测**指数层**
（日线 OHLC 足够，不需要 1min）：开→收 vs 昨收→开，逐年给出 t，
以回答"这个效应是真的还是噪声"。

用法：python t_io/validation/intraday_drift/long_sample.py
"""
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

OUT = os.path.dirname(os.path.abspath(__file__))
N_BARS = 2000
SYMS = [('sh000001', '上证指数'), ('sz399001', '深证成指'),
        ('sz399006', '创业板指'), ('sh000688', '科创50'),
        ('sh000300', '沪深300')]
CACHE = os.path.join(OUT, 'kline_cache')


def fetch(sym, n=N_BARS):
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, f'{sym}_{n}.json')
    if os.path.exists(fp):
        return json.load(open(fp, encoding='utf-8'))
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.qq.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    v = (d.get("data") or {}).get(sym) or {}
    rows = v.get("day") or v.get("qfqday") or []
    json.dump(rows, open(fp, 'w', encoding='utf-8'))
    time.sleep(0.4)
    return rows


def stats(x):
    x = np.asarray(x, float)
    if len(x) < 3:
        return None
    return len(x), float(x.mean()), float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def main():
    print(f"{'指数':10}{'n':>6}{'区间':>26}  {'日内(开→收)':>26}  {'隔夜(昨收→开)':>22}")
    out = {}
    for sym, name in SYMS:
        try:
            rows = fetch(sym)
        except Exception as e:
            print(f"{name:10} 抓取失败: {e}")
            continue
        if len(rows) < 300:
            print(f"{name:10} 数据不足 ({len(rows)})")
            continue
        dates = [r[0] for r in rows]
        o = np.array([float(r[1]) for r in rows], float)
        c = np.array([float(r[2]) for r in rows], float)
        intra = c / o - 1
        overn = o[1:] / c[:-1] - 1
        si, so = stats(intra), stats(overn)
        print(f"{name:10}{len(rows):>6}{dates[0]+'~'+dates[-1]:>26}  "
              f"{si[1]*100:>+9.4f}%/日 t={si[2]:>+5.2f} 年化{si[1]*243*100:>+6.1f}%   "
              f"{so[1]*100:>+8.4f}%/日 t={so[2]:>+5.2f}")
        # 逐年
        yrs = {}
        for i, d in enumerate(dates):
            yrs.setdefault(d[:4], []).append(intra[i])
        yr_tab = {}
        print('      逐年 日内均值/t:  ', end='')
        for y in sorted(yrs):
            v = np.array(yrs[y], float)
            if len(v) < 60:
                continue
            tv = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
            yr_tab[y] = {'n': len(v), 'avg': round(float(v.mean()), 5), 't': round(float(tv), 2)}
            print(f"{y}:{v.mean()*100:+.3f}%(t{tv:+.1f}) ", end='')
        print()
        out[name] = {'n': si[0], 'first': dates[0], 'last': dates[-1],
                     'intraday': {'avg_pct': round(si[1] * 100, 4), 't': round(si[2], 2),
                                  'annual_pct': round(si[1] * 243 * 100, 1)},
                     'overnight': {'avg_pct': round(so[1] * 100, 4), 't': round(so[2], 2)},
                     'by_year': yr_tab}
        # 分半
        h = len(intra) // 2
        print(f"      前半({dates[0]}~{dates[h]}): {intra[:h].mean()*100:+.4f}%/日 | "
              f"后半({dates[h+1]}~{dates[-1]}): {intra[h:].mean()*100:+.4f}%/日")
    json.dump(out, open(os.path.join(OUT, 'long_sample_2026-09-14.json'), 'w',
                        encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n[long] -> {os.path.join(OUT, 'long_sample_2026-09-14.json')}")


if __name__ == '__main__':
    main()
