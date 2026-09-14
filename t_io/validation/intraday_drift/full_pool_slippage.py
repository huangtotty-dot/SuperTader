# -*- coding: utf-8 -*-
"""宽基/行业 ETF 全池 · 只做日内（开买收卖、隔夜空仓）— 滑点敏感性（2026-09-15）。

## 为什么
`etf_intraday_only.py` 在 12 只 ETF 上得「12/12 跑赢买入持有」，但**那 12 只是我挑的**
（含军工ETF +407% 这类大牛）→ 选择偏差未排除。且**只扣了 0.036% 佣金、未计滑点**。

## 本脚本
1. 扩到 **~30 只 ETF 全池**（宽基/行业/主题/跨境/商品混编，不做挑选）；
2. 滑点敏感性：`slip ∈ {0, 1, 2, 3, 5} bp/边`（宽基 ETF 盘口 ~2-3bp，行业 ETF 更宽）；
3. 对照臂 = **买入持有**（同期不动）；
4. 同时输出「跑赢持有的只数」与「净收益为正的只数」随滑点的变化——
   **边际（+0.078%/日）与执行成本同量级**，故这张表就是可交易性的答案。

用法：python t_io/validation/intraday_drift/full_pool_slippage.py
"""
import json
import os
import time
import urllib.request

import numpy as np

OUT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(OUT, 'kline_cache')
N = 2000
COMMISSION = 0.0002          # ETF 单边佣金（免印花税）；双边 0.04%
SLIPS = [0.0, 0.0001, 0.0002, 0.0003, 0.0005]

ETFS = [
    ('sh510300', '沪深300ETF'), ('sh510500', '中证500ETF'), ('sh510050', '上证50ETF'),
    ('sh510880', '红利ETF'), ('sh512100', '中证1000ETF'), ('sh588000', '科创50ETF'),
    ('sz159915', '创业板ETF'), ('sz159919', '沪深300ETF(深)'), ('sz159901', '深100ETF'),
    ('sh512880', '证券ETF'), ('sh512480', '半导体ETF'), ('sh512660', '军工ETF'),
    ('sh512010', '医药ETF'), ('sh512170', '医疗ETF'), ('sh512690', '酒ETF'),
    ('sh512800', '银行ETF'), ('sh512400', '有色金属ETF'), ('sh512980', '传媒ETF'),
    ('sh515030', '新能源车ETF'), ('sh515790', '光伏ETF'), ('sh516160', '新能源ETF'),
    ('sz159755', '电池ETF'), ('sh512200', '房地产ETF'), ('sz159928', '消费ETF'),
    ('sh516110', '汽车ETF'), ('sh512580', '环保ETF'), ('sz159865', '养殖ETF'),
    ('sh512760', '芯片ETF'), ('sh515180', '红利ETF(易)'), ('sh515120', '创新药ETF'),
    ('sh513050', '中概互联ETF'), ('sh513100', '纳指ETF'), ('sh518880', '黄金ETF'),
]


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
    data = {}
    for sym, name in ETFS:
        try:
            r = fetch(sym)
        except Exception as e:
            print(f"  {name} 抓取失败: {str(e)[:50]}")
            continue
        if len(r) >= 250:
            data[name] = r
    print(f"[pool] 有效 ETF: {len(data)}/{len(ETFS)}\n")

    print(f"{'滑点/边':>8}{'净均%/日':>10}{'t(按日)':>9}{'年化%':>8}"
          f"{'只做日内>持有':>15}{'只做日内>0':>12}{'中位超额pp':>12}")
    out = {'commission': COMMISSION, 'n_etf': len(data), 'by_slip': {}}
    for slip in SLIPS:
        side_s = COMMISSION + slip
        side_b = COMMISSION + slip
        excs, wins_pos = [], []
        daily = []                      # 等权组合的按日收益（取各自交易日对齐后近似：用序号对齐）
        per_etf = []
        for name, r in data.items():
            o = np.array([float(x[1]) for x in r], float)
            c = np.array([float(x[2]) for x in r], float)
            net = (c * (1 - side_s) - o * (1 + side_b)) / o
            cum = (np.prod(1 + net) - 1) * 100
            bh = (c[-1] / c[0] - 1) * 100
            excs.append(cum - bh)
            per_etf.append({'name': name, 'n': len(r), 'net_cum_pct': round(cum, 1),
                            'bh_pct': round(bh, 1)})
            if len(net) > len(daily):
                daily = list(net)
            elif len(net) > 100:
                pass
        # 组合作日内曲线：长度对齐到最短（近似）
        L = min(len(np.array([float(x[1]) for x in r], float)) for r in data.values())
        mat = []
        for name, r in data.items():
            o = np.array([float(x[1]) for x in r], float)[-L:]
            c = np.array([float(x[2]) for x in r], float)[-L:]
            mat.append((c * (1 - side_s) - o * (1 + side_b)) / o)
        port = np.mean(np.array(mat), axis=0)
        t = port.mean() / (port.std(ddof=1) / np.sqrt(len(port)))
        exc = np.array(excs)
        n_beat = int((exc > 0).sum())
        n_pos = sum(1 for x in per_etf if x['net_cum_pct'] > 0)
        print(f"{slip*1e4:>6.0f}bp{port.mean()*100:>10.4f}{t:>9.2f}{port.mean()*243*100:>8.1f}"
              f"{n_beat:>11}/{len(exc)}{n_pos:>9}/{len(per_etf)}{np.median(exc):>12.1f}")
        out['by_slip'][f'{slip*1e4:.0f}bp'] = {
            'port_avg_pct': round(float(port.mean() * 100), 4), 't': round(float(t), 2),
            'annual_pct': round(float(port.mean() * 243 * 100), 1),
            'beat_hold': int((exc > 0).sum()), 'n_etf': len(exc),
            'net_positive': sum(1 for x in per_etf if x['net_cum_pct'] > 0),
            'median_excess_pp': round(float(np.median(exc)), 1), 'per_etf': per_etf}
    json.dump(out, open(os.path.join(OUT, 'full_pool_slippage_2026-09-15.json'), 'w',
                        encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n[pool] -> {os.path.join(OUT, 'full_pool_slippage_2026-09-15.json')}")


if __name__ == '__main__':
    main()
