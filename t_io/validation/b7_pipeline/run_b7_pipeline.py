# -*- coding: utf-8 -*-
"""B7 全管线决赛 — 生产规则模拟器（实验员_E3，2026-09-15）。

## 为什么不直接进 harness_backtest.py（任务①结论）
harness 的策略接入 = SignalEngine.evaluate（Renko 决策核）逐分钟 tick，信号类型固定
BUY_LOW/ADD_POS/SELL_HIGH，无自定义策略插件口；且两处硬假设与 B7 隔夜腿冲突：
  1) 信号结算 settle_signal：信号后最多 30 根**当日** 1min K 线 ±0.5%/0.4% 触及判定；
  2) 闭环 compute_closed_loop：**当日 FIFO 配对**，跨日腿只进 open_long/open_short 永不结算；
  3) 费用口径：卖 0.00065（佣金0.00015+印花税0.0005）≠ 生产全成本 0.00121。
硬改 harness 会破坏其与实盘同源的语义 → 按任务预案改用「生产规则模拟器」：
复刻 execution/auto 的执行规则（费用/可用量/归位/回补门控）跑同一信号序列。

## 复刻的生产规则（出处逐条可查）
  R1 费用：卖 0.00121 / 买 0.00015（main.py:417-422，GM 全成本：佣金+印花税+过户费 / 佣金）。
  R2 底仓存在性校验：base ≥ 100 股才允许卖底仓（holdings.json 生产底仓，只读）。
  R3 T+1 可用量校验：sell_qty = min(base, available)，100 股取整；available 扣在途卖单
     （sell_channels.py:451-455 TAIL 同口径）。模拟假设底仓 intact（owner 三段式默认态），
     当日 14:55 前未动底仓 → available = base。该假设在报告中声明。
  R4 数量不变硬约束（owner 2026-09-14 裁决，sell_channels.py:262-329 _force_tail_buyback）：
     尾盘 14:50+ 对「未平卖出且 pos < base_ref」无条件市价回补。B7 14:55 卖出后，
     下一分钟 bar 即触发 → 隔夜腿在生产现状下被当场杀死（变体 V1 量化此损耗）。
  R5 回补价格记忆门控（t_engine_auto.py:652-731 + arm_awaiting_buyback:765-791）：
     卖出后 arm 链 target = sell × 0.998（awaiting_buyback_vwap_gap 默认）；
     premium = price/sell − 1：
       premium > +1%（buyback_above_sell_delay_pct=0.01）→ delayed 不接；
       0 < premium ≤ +1%（buyback_above_sell_downgrade_pct=0.0）→ downgrade 数量减半；
       target < price ≤ sell（premium ∈ (−0.2%, 0]）→ not_target 等回踩；
       price ≤ target → 正常接回。
     盘中始终未成交 → 14:50 强制回补（R4）。链 3 交易日过期（buyback_persist_days=3），
     因 R4 当日必平，模拟中不触发。
  R6 尾盘归位 TAIL（sell_channels.py:444-500，14:50-15:00 超底仓部分强制卖出归位）：
     B7 卖出后 pos=0 < base_ref，TAIL 只卖超仓 → 不触发，无冲突（报告中说明）。
  R7 信号口径（与 E1 一致，无未来函数）：tail30 = close(14:55)/close(14:30) − 1 > 1%，
     卖出价 close(14:55)，信号只用 ≤14:55 数据；接回用次日数据属策略内生合法未来。

## 变体
  V0_offline        E1 离线口径复现：无条件次日开盘接回（对照基准）。
  V1_prod_hard      生产现状：R4 数量不变硬约束 → 14:56 当日强制回补。
  V2_prod_overnight B7 获隔夜豁免（R4 对该策略关闭）+ R5 生产回补门控全量生效：
                    premium≤−0.2% → open 接回（近似：生产还需 Renko 向下砖，声明该近似）；
                    not_target/delayed → 盘中首个 low ≤ target 按 target 限价接回，
                    全天未到 → 14:50 收盘价强制回补；downgrade → 半仓 open + 半仓前逻辑。

## 池子
  prod5    = 000988/002451/002639/300054/603667（生产五票池）
  harness5 = 000988/588170/600176/600481/603667（harness 默认池）

数据：t_io/backtest_1year_data（复用 macd_divergence_t/run_experiment_v2 数据层，与 E1 同源）。
窗口：2025-09-14 ~ 2026-08-26；OOS 自 2026-06-01（均与 E1 对齐）。
"""
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
for _p in (ROOT, _MD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402  复用 1min 数据层（与 E1/calibrate 同口径）

OUT = os.path.join(HERE, 'results_b7_pipeline_2026-09-15.json')
WIN_START, WIN_END = '2025-09-14', '2026-08-26'
OOS_START = '2026-06-01'
FEE_S, FEE_B = 0.00121, 0.00015      # R1：生产全成本（main.py:417-422）
HARNESS_FEE_S = 0.00065              # harness 口径：佣金0.00015+印花税0.0005
MIN_BAR = 100
N_MC, MC_SEED = 200, 20260915

# R5 生产回补门控参数（t_engine_auto 默认值）
VWAP_GAP = 0.998          # awaiting_buyback_vwap_gap → target = sell × 0.998
DELAY_PCT = 0.01          # buyback_above_sell_delay_pct
DOWNGRADE_PCT = 0.0       # buyback_above_sell_downgrade_pct
FORCE_LABEL = '14:50'     # R4 尾盘强制回补时点
SELL_LABEL = '14:55'      # B7 卖出时点
HARD_BUY_LABEL = '14:56'  # V1：卖出后下一分钟 bar 强制回补

POOLS = {
    'prod5': ['000988', '002451', '002639', '300054', '603667'],
    'harness5': ['000988', '588170', '600176', '600481', '603667'],
}

# R2：生产底仓（t_io/state/holdings.json 只读；缺失票按 0 → 底仓存在性校验不过）
def load_base_map():
    fp = os.path.join(ROOT, 't_io', 'state', 'holdings.json')
    base = {}
    try:
        raw = json.load(open(fp, encoding='utf-8'))
        for k, h in raw.items():
            code = k.split('_')[0]
            base[code] = int(h.get('base', 0) or 0)
    except Exception as e:
        print(f'[b7p] holdings.json 读取失败（底仓校验将全拒）: {e}')
    return base


def _close_at(day, label):
    for b in day:
        if b['t'] == label:
            return b['c']
    return None


def collect(codes):
    """逐票收集信号日特征 + 次日接回所需数据。信号特征全部 ≤14:55，无未来函数。"""
    rows = []
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if WIN_START <= d <= WIN_END]
        if len(dates) < 10:
            print(f'[b7p] {code} 交易日不足({len(dates)})，跳过')
            continue
        for k, d in enumerate(dates):
            day = merged[d]
            if len(day) < MIN_BAR or day[0]['o'] <= 0:
                continue
            c1430, c1455 = _close_at(day, '14:30'), _close_at(day, SELL_LABEL)
            if not (c1430 and c1455):
                continue
            if k + 1 >= len(dates):
                continue                      # 无次日数据的末日剔除（与 E1 一致）
            nd = merged[dates[k + 1]]
            rows.append({
                'code': code, 'date': d, 'sell': c1455,
                'c1456': _close_at(day, HARD_BUY_LABEL) or day[-1]['c'],
                'tail30': c1455 / c1430 - 1,
                'next_open': nd[0]['o'],
                'next_bars': nd,              # 次日全分钟（限价扫描用，策略内生合法未来）
                'next_force': _close_at(nd, FORCE_LABEL) or nd[-1]['c'],
            })
    return rows


def net(sell, buy):
    """单位净收益（R1 费用口径）。"""
    if not buy or buy <= 0:
        return None
    return (sell * (1 - FEE_S) - buy * (1 + FEE_B)) / sell


def buyback_price_prod(r):
    """R5 生产回补门控 → (接回均价, 路径标签)。target 限价扫描 t<14:50，未到则 14:50 强平。"""
    sell, o = r['sell'], r['next_open']
    target = sell * VWAP_GAP
    premium = o / sell - 1

    def scan_limit():
        for b in r['next_bars']:
            if b['t'] >= FORCE_LABEL:
                break
            if b['l'] <= target:
                return target, 'limit@target'
        return r['next_force'], 'forced@14:50'

    if premium <= target / sell - 1:          # open ≤ target → 正常低吸接回
        return o, 'open(dip)'
    if premium <= DOWNGRADE_PCT:              # target < open ≤ sell → not_target 等回踩
        return scan_limit()[0], 'not_target→' + scan_limit()[1]
    if premium <= DELAY_PCT:                  # 0 < premium ≤ 1% → downgrade 半仓
        p2, path2 = scan_limit()
        return (o + p2) / 2, 'downgrade(half open + half ' + path2 + ')'
    # premium > 1% → delayed 开盘不接，盘中等回踩，否则强平
    p, path = scan_limit()
    return p, 'delayed→' + path


def _stat(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    if len(a) == 0:
        return None
    return {'n': int(len(a)), 'mean': round(float(a.mean()) * 100, 4),
            'median': round(float(np.median(a)) * 100, 4),
            'win': round(float((a > 0).mean()), 4)}


def max_loss_streak(trades):
    s = best = 0
    for t in sorted(trades, key=lambda x: (x['date'], x['code'])):
        s = s + 1 if t['net'] <= 0 else 0
        best = max(best, s)
    return best


def top3_share(trades):
    per = {}
    for t in trades:
        per[t['code']] = per.get(t['code'], 0.0) + t['net']
    tot = sum(per.values())
    if tot <= 0:
        return None
    return round(sum(sorted(per.values(), reverse=True)[:3]) / tot, 4)


def run_variant(rows, mode, base_map):
    """mode ∈ {V0_offline, V1_prod_hard, V2_prod_overnight}。返回逐笔交易列表。"""
    trades = []
    for r in rows:
        if r['tail30'] is None or r['tail30'] <= 0.01:   # S1 信号
            continue
        # R2 底仓存在性 + R3 T+1 可用量（intact 假设下 available=base，100 股取整）
        base = base_map.get(r['code'], 0)
        sell_qty = (base // 100) * 100
        if sell_qty < 100:
            continue
        if mode == 'V0_offline':
            n = net(r['sell'], r['next_open'])
            path = 'next_open'
        elif mode == 'V1_prod_hard':
            n = net(r['sell'], r['c1456'])                # R4：当日强制回补
            path = 'forced@14:56(数量不变硬约束)'
        else:
            bp, path = buyback_price_prod(r)              # R5：生产回补门控
            n = net(r['sell'], bp)
        if n is None:
            continue
        trades.append({'code': r['code'], 'date': r['date'], 'sell': r['sell'],
                       'tail30': round(r['tail30'] * 100, 3),
                       'next_gap': round((r['next_open'] / r['sell'] - 1) * 100, 3),
                       'path': path, 'net': n})
    return trades


def cell_stats(trades, pool_rows, rng):
    nets = [t['net'] for t in trades]
    st = _stat(nets)
    if st is None:
        return None
    # 随机基线（V0 口径资格池全样本，与 E1 同法）
    pool_nets = np.array([net(r['sell'], r['next_open']) for r in pool_rows], float)
    pool_nets = pool_nets[np.isfinite(pool_nets)]
    mc = None
    mc_rank = None
    if len(pool_nets) >= st['n'] > 0:
        means = np.array([float(rng.choice(pool_nets, size=st['n'], replace=False).mean())
                          for _ in range(N_MC)])
        mc = {'mc_mean': round(float(means.mean()) * 100, 4),
              'mc_p05': round(float(np.percentile(means, 5)) * 100, 4),
              'mc_p95': round(float(np.percentile(means, 95)) * 100, 4)}
        mc_rank = round(float((means < st['mean'] / 100).mean()), 4)
    oos = _stat([t['net'] for t in trades if t['date'] >= OOS_START])
    ins = _stat([t['net'] for t in trades if t['date'] < OOS_START])
    paths = {}
    for t in trades:
        key = t['path'].split('(')[0].split('→')[0]
        paths[key] = paths.get(key, 0) + 1
    per_stock = {}
    for c in sorted({t['code'] for t in trades}):
        sub = [t for t in trades if t['code'] == c]
        s2 = _stat([t['net'] for t in sub])
        per_stock[c] = {'n': s2['n'], 'net_mean%': s2['mean'], 'win': s2['win']}
    return {'n': st['n'], 'net_mean%': st['mean'], 'net_median%': st['median'],
            'win': st['win'], 'mc': mc, 'mc_rank': mc_rank,
            'delta_vs_mc_pp': (round(st['mean'] - mc['mc_mean'], 4) if mc else None),
            'oos': oos, 'in_sample': ins,
            'max_loss_streak': max_loss_streak(trades),
            'top3_share': top3_share(trades),
            'buyback_paths': paths, 'per_stock': per_stock,
            'trades': [{k: (round(v * 100, 4) if k == 'net' else v)
                        for k, v in t.items() if k != 'next_bars'} for t in trades]}


def main():
    v2.END = WIN_END
    base_map = load_base_map()
    rng = np.random.default_rng(MC_SEED)
    all_codes = sorted({c for cs in POOLS.values() for c in cs})
    print(f'[b7p] 加载数据: {all_codes}')
    data = {c: collect([c]) for c in all_codes}

    result = {'meta': {
        'experiment': 'B7 全管线决赛·生产规则模拟器（E3）',
        'date': '2026-09-15',
        'window': [WIN_START, WIN_END], 'oos_start': OOS_START,
        'fee': {'sell': FEE_S, 'buy': FEE_B,
                'source': 'main.py:417-422 GM全成本（佣金+印花税+过户费/佣金）',
                'harness_fee_sell': HARNESS_FEE_S},
        'signal': 'S1: close(14:55)/close(14:30)-1 > 1%，信号锁死≤14:55（无未来函数）',
        'pools': POOLS, 'base_map': {c: base_map.get(c, 0) for c in all_codes},
        'prod_rules': {
            'R2_底仓存在性': 'base≥100股（holdings.json 只读）',
            'R3_T+1可用量': 'sell_qty=min(base,available)，intact假设→available=base，100股取整',
            'R4_数量不变硬约束': 'sell_channels.py:262-329，14:50+无条件回补未平卖出（V1）',
            'R5_回补门控': f'target=sell×{VWAP_GAP}; delay>{DELAY_PCT}; downgrade>{DOWNGRADE_PCT}; '
                           f'not_target; 14:50强制回补（V2）',
            'R6_尾盘归位TAIL': 'pos<base_ref 不触发（只卖超仓），与B7卖出无冲突',
        },
        'approximations': [
            'V2 正常接回按次日 open 成交（生产还需 Renko 向下砖确认，未模拟砖触发）',
            'V2 限价接回按 target 成交（low≤target 即视为成交，未做流动性/冲击校验）',
            '底仓 intact 假设：当日 14:55 前无底仓卖出、无未平 T 腿占用可用量',
            '588170 为 ETF（无印花税），仍按统一 0.00121 计（保守，与 E1 口径一致）',
        ],
        'n_mc': N_MC, 'mc_seed': MC_SEED,
    }, 'pools': {}}

    for pool_name, codes in POOLS.items():
        rows = [r for c in codes for r in data.get(c, [])]
        days = sorted({r['date'] for r in rows})
        print(f'\n[b7p] === {pool_name} ({len(codes)}票) 股票·日={len(rows)} 交易日={len(days)} ===')
        gap = _stat([r['next_open'] / r['sell'] - 1 for r in rows])
        pool_out = {'n_rows': len(rows), 'trading_days': len(days),
                    'next_gap_全样本': gap, 'variants': {}}
        # 每天都卖基线（V0 口径，检验信号增量）
        all_sell = [{'code': r['code'], 'date': r['date'],
                     'net': net(r['sell'], r['next_open'])} for r in rows]
        pool_out['每天都卖基线'] = _stat([t['net'] for t in all_sell])

        for mode in ('V0_offline', 'V1_prod_hard', 'V2_prod_overnight'):
            trades = run_variant(rows, mode, base_map)
            cell = cell_stats(trades, rows, rng)
            pool_out['variants'][mode] = cell
            if cell:
                print(f"[b7p] {mode:18s} n={cell['n']:3d} 净均={cell['net_mean%']:+.4f}% "
                      f"胜={cell['win']:.3f} 中位={cell['net_median%']:+.4f}% "
                      f"OOS={cell['oos']['mean'] if cell['oos'] else '--'}%"
                      f"(n={cell['oos']['n'] if cell['oos'] else 0}) "
                      f"连亏={cell['max_loss_streak']} TOP3={cell['top3_share']}")
                if mode == 'V2_prod_overnight':
                    print(f'       回补路径: {cell["buyback_paths"]}')
        result['pools'][pool_name] = pool_out

    # harness 口径对照（结构性差异量化，非跑 harness）：
    # ① 费用差：harness 卖 0.00065 vs 生产 0.00121 → 每笔 +0.056pp 乐观偏置
    # ② 闭环：当日 FIFO → B7 卖腿进 open_short 永不结算，closed_pnl=0
    # ③ 结算：信号后 30 根 ±0.5% 触及 → 与隔夜真实成交价无关
    result['harness_vs_offline'] = {
        'fee_bias_pp_per_trade': round((FEE_S - HARNESS_FEE_S) * 100, 4),
        'closed_loop': 'harness 当日FIFO：14:55卖腿跨日不配对 → open_short 挂账，闭环PnL=0',
        'settle': 'settle_signal 仅信号后30根当日K线±0.5%/0.4%触及判定，无法表达隔夜跳空收益',
        'conclusion': 'harness 口径下 B7 收益完全不可见（非衰减，是结构性不表达）',
    }

    json.dump(result, open(OUT, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\n[b7p] -> {OUT}')


if __name__ == '__main__':
    main()
