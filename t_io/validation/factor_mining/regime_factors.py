# -*- coding: utf-8 -*-
"""波段 regime 因子包 —— 策略线②「震荡末期与单边上涨阶段低吸高抛」的状态识别层。

## 定位

对应 owner 2026-09-18 方向：底仓全程不动，活动仓在**震荡段低吸高抛**、
**单边上涨段只加不减（防卖飞）**。所以本包挖的不是买卖点，而是
「当前处于哪种 regime」+「该 regime 下次日有多少可捕获的日内波动」。

## 数据

日线面板 `t_io/validation/xsection/panel/`（5674 只含退市股，PIT 设计，
每股最多 2000 根日线，最新 2026-09-17）。全部因子 **t 日收盘后可得**，
无任何未来函数：所有 rolling/ewm 窗口只含 t 及更早数据，打标只用 t 日值。

## 因子清单（日频，pandas/numpy 手写，不依赖 ta-lib）

| 因子 | 定义 |
|---|---|
| adx14 | Wilder ADX(14)。TR/+DM/-DM 后用 `ewm(alpha=1/14, adjust=False)` 平滑（与经典 Wilder 递推仅初始值约定不同，14 期后差异可忽略）；ADX = DX 的同参数平滑 |
| bbw | 布林带宽 (上轨-下轨)/中轨，参数 (20, 2)，std 用总体口径 ddof=0 |
| bbw_pct120 | bbw 在过去 120 日的分位（`rolling(120).rank(pct=True)`），低=带宽收缩 |
| ma_bull_layers | 均线多头链式层数 0-3：L1=close>MA5，L2=L1&&MA5>MA10，L3=L2&&MA10>MA20 |
| ma_bull_cont | 连续版多头度 (close-MA20)/ATR14 |
| range_days | 震荡持续度：ADX<20 的连续天数 |
| trend_up_confirm | 单边上涨确认（事件型 0/1）：ADX 上穿 25 且 +DI>-DI |
| dist_high20 | 距 20 日高点距离 close/rolling_max(high,20)-1（≤0，越负越"低位"） |

## regime 打标规则（写死，见 `label_regimes`，优先级自上而下）

1. **单边上涨**：adx14 > 25 且 +DI > -DI 且 ma_bull_layers >= 2
2. **单边下跌**：adx14 > 25 且 -DI > +DI 且 close < MA20
3. **震荡末期**：adx14 < 20 且 bbw_pct120 <= 0.20（带宽收缩到 120 日最低 20% 分位 + 趋势强度低位）
4. **震荡**：adx14 < 20（未收缩到极致的普通低趋势区间）
5. **混沌**：其余（ADX 20~25 过渡带、ADX>25 但方向/均线不自洽等）

历史不足（MA20/ADX 预热 < 30 根，或 bbw_pct120 需要 140 根）的行 label=NaN，不参与统计。

## IC 层 API 契约（任务2 `ic_layer.py` 的真实契约，2026-09-18 已落地）

```python
from ic_layer import evaluate_factor, decile_analysis, mc_baseline
# factor: 长表 [date, symbol, value]，value 为 t 日收盘后可得
# panel : 长表 [symbol, date, open, high, low, close, volume, amount]（date 去时区）
evaluate_factor(factor, panel, horizons=(1,3,5), min_coverage=30) -> dict  # 按日截面 RankIC
decile_analysis(factor, panel, horizon=1, n_groups=10) -> dict  # 分组收益+单调性+多空净值
mc_baseline(factor, panel, horizon=1, n=200, seed=42) -> dict  # 日期内洗牌零假设 + pass 闸门
```

预注册口径：buy_open（open(t+1+h)/open(t+1)-1，可执行）为主，close 口径为参考。
`factor_health_check()` 优先 import ic_layer；**缺位时**用本模块 `_fb_*`
同签名回退桩（同口径：buy_open 主口径、逐日截面 Spearman、日期内洗牌 MC），
结果标注 `ic_backend='fallback_stub'`。ic_layer 落地后已用真后端复跑。

## 场景价值验证（`scenario_value`，本包的重点）

对每个 (symbol, t) 的 regime 标签，统计 t+1 日：
- 振幅捕获空间 amp = (high-low)/open
- 理论低吸高抛上限 theo = (high-low)/low（low 买 high 卖，不可实现的乐观上界）
- 现实口径 real = close/open - 1（open 买 close 卖）

检验「震荡末期+单边上涨」组的次日 amp 是否显著大于其余 regime：
Mann-Whitney U + 日期内洗牌蒙特卡洛（消除市场级波动聚集的混杂）。

## 用法（分步，每步可断点续跑）

```bash
python regime_factors.py build --shard 0        # 逐分片构建因子（12 片）
python regime_factors.py build --max-symbols 400  # 采样试跑
python regime_factors.py merge                  # 合并分片 → regime_factor_panel.parquet
python regime_factors.py health                 # 因子体检（ic_layer 优先）→ regime_health.jsonl
python regime_factors.py scenario               # 场景价值验证 → regime_scenario.json
python regime_factors.py report                 # 汇总 → regime_results.json
# 所有子命令支持 --tag _suffix：输出文件名加后缀，保留旧结果（口径切换复跑用）
```
"""
import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR = os.path.normpath(os.path.join(HERE, '..', 'xsection', 'panel', 'shards'))
UNIVERSE = os.path.normpath(os.path.join(HERE, '..', 'xsection', 'panel', 'universe.parquet'))

# ── 预注册参数（写死，不参与搜索）──────────────────────────────────────────
ADX_N = 14
ADX_TREND = 25.0          # 单边门槛
ADX_RANGE = 20.0          # 震荡门槛
BB_N, BB_K = 20, 2
BBW_PCT_WIN = 120
BBW_SQUEEZE_Q = 0.20      # 带宽收缩分位阈值
MIN_BARS_ADX = 2 * ADX_N + 2     # ADX 预热
MIN_BARS_LABEL = BB_N + BBW_PCT_WIN  # 打标需完整 140 根历史
MAX_GAP_DAYS = 5          # t→t+1 日历间隔上限（停牌跨越不算"次日"）

REGIME_UP = '单边上涨'
REGIME_DOWN = '单边下跌'
REGIME_SQUEEZE = '震荡末期'
REGIME_RANGE = '震荡'
REGIME_CHAOS = '混沌'
REGIMES = [REGIME_SQUEEZE, REGIME_RANGE, REGIME_UP, REGIME_DOWN, REGIME_CHAOS]

FACTOR_COLS = ['adx14', 'bbw', 'bbw_pct120', 'ma_bull_layers', 'ma_bull_cont',
               'range_days', 'trend_up_confirm', 'dist_high20']


# ── 无 scipy 的统计原语（managed python 无 scipy，与 ic_layer 同纪律）────────
def _spearman(a, b) -> float:
    """Spearman = 秩上的 Pearson（pandas rank + numpy，禁 scipy）。"""
    ra = pd.Series(a).rank(method='average').to_numpy(float)
    rb = pd.Series(b).rank(method='average').to_numpy(float)
    ra, rb = ra - ra.mean(), rb - rb.mean()
    d = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / d) if d > 0 else float('nan')


def _mannwhitney_greater(x: np.ndarray, y: np.ndarray):
    """Mann-Whitney U（H1: x > y），大样本正态渐近 p 值（含连续性校正，无 tie 校正）。

    日聚合样本量 ~2000，渐近近似足够；返回 (U, p_one_sided)。
    """
    from math import erf, sqrt
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    n1, n2 = len(x), len(y)
    r = pd.Series(np.concatenate([x, y])).rank(method='average').to_numpy(float)
    r1 = r[:n1].sum()
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mu, sd = n1 * n2 / 2.0, sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    z = (u1 - mu - 0.5) / sd
    p = 0.5 * (1 - erf(z / sqrt(2)))   # 单侧（greater）：P(Z >= z)
    return float(u1), float(p)


# ════════════════════════════════════════════════════════════════════════════
# 指标实现（全部输入为单 symbol 按时间升序的 numpy 数组，输出同长数组）
# ════════════════════════════════════════════════════════════════════════════
def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder 平滑 ≈ ewm(alpha=1/n, adjust=False)（仅初始值约定不同）。"""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def wilder_adx(high, low, close, n=ADX_N):
    """→ (adx, plus_di, minus_di)，pd.Series。手写 Wilder 平滑，不依赖 ta-lib。"""
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    up_move = h.diff()
    down_move = -l.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=h.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=h.index)
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, n)
    plus_di = 100.0 * _wilder(plus_dm, n) / atr.replace(0, np.nan)
    minus_di = 100.0 * _wilder(minus_dm, n) / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder(dx, n)
    return adx, plus_di, minus_di, atr


def bollinger_bbw(close, n=BB_N, k=BB_K):
    """→ (bbw, mid)。bbw = (上轨-下轨)/中轨 = 2k·σ/MA。"""
    c = pd.Series(close)
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    bbw = (2 * k * sd) / mid.replace(0, np.nan)
    return bbw, mid


def rolling_pct_rank(s: pd.Series, win: int) -> pd.Series:
    """当前值在过去 win 个观测（含自身）中的分位 ∈ (0,1]。"""
    return s.rolling(win).rank(pct=True)


def ma_bull_layers(close):
    """链式多头层数 0-3：L1=close>MA5；L2=L1&&MA5>MA10；L3=L2&&MA10>MA20。"""
    c = pd.Series(close)
    ma5, ma10, ma20 = c.rolling(5).mean(), c.rolling(10).mean(), c.rolling(20).mean()
    l1 = c > ma5
    l2 = l1 & (ma5 > ma10)
    l3 = l2 & (ma10 > ma20)
    return (l1.astype(int) + l2.astype(int) + l3.astype(int)).astype(float).where(ma20.notna()), ma20


def streak_below(s: pd.Series, thr: float) -> pd.Series:
    """s < thr 的连续天数（t 日满足则计入 t）。"""
    cond = (s < thr).astype(int)
    grp = (cond != cond.shift()).cumsum()
    return (cond * (cond.groupby(grp).cumcount() + 1)).astype(float)


def trend_up_confirm(adx, plus_di, minus_di, thr=ADX_TREND):
    """ADX 上穿 thr 且 +DI>-DI → 1.0，否则 0.0（事件型因子）。"""
    cross = (adx > thr) & (adx.shift(1) <= thr)
    return (cross & (plus_di > minus_di)).astype(float)


def dist_to_high(close, high, n=20):
    """close / rolling_max(high, n) - 1 ∈ [-1, 0]，0 = 收在 n 日最高。"""
    return pd.Series(close) / pd.Series(high).rolling(n).max() - 1.0


# ════════════════════════════════════════════════════════════════════════════
# 因子面板构建
# ════════════════════════════════════════════════════════════════════════════
def compute_symbol_factors(df: pd.DataFrame) -> pd.DataFrame:
    """单 symbol（按 eob 升序，列 open/high/low/close）→ 追加全部因子列。

    严格因果：所有输出在 t 行的值只用 [0, t] 区间的输入。
    """
    df = df.sort_values('eob').reset_index(drop=True)
    h, l, c = df['high'], df['low'], df['close']
    adx, pdi, mdi, atr = wilder_adx(h, l, c)
    bbw, mid = bollinger_bbw(c)
    layers, ma20 = ma_bull_layers(c)
    df['adx14'] = adx
    df['plus_di'] = pdi
    df['minus_di'] = mdi
    df['atr14'] = atr
    df['bbw'] = bbw
    df['bbw_pct120'] = rolling_pct_rank(bbw, BBW_PCT_WIN)
    df['ma_bull_layers'] = layers
    df['ma_bull_cont'] = (c - ma20) / atr.replace(0, np.nan)
    df['range_days'] = streak_below(adx, ADX_RANGE)
    df['trend_up_confirm'] = trend_up_confirm(adx, pdi, mdi)
    df['dist_high20'] = dist_to_high(c, h)
    df['ma20'] = ma20
    df['n_hist'] = np.arange(1, len(df) + 1)
    return df


def label_regimes(df: pd.DataFrame) -> pd.Series:
    """向量化打标。规则见模块 docstring；历史不足 → NaN。"""
    valid = (df['n_hist'] >= MIN_BARS_LABEL)
    up = valid & (df['adx14'] > ADX_TREND) & (df['plus_di'] > df['minus_di']) & (df['ma_bull_layers'] >= 2)
    down = valid & ~up & (df['adx14'] > ADX_TREND) & (df['minus_di'] > df['plus_di']) & (df['close'] < df['ma20'])
    sqz = valid & ~up & ~down & (df['adx14'] < ADX_RANGE) & (df['bbw_pct120'] <= BBW_SQUEEZE_Q)
    rng = valid & ~up & ~down & ~sqz & (df['adx14'] < ADX_RANGE)
    lab = pd.Series(REGIME_CHAOS, index=df.index, dtype=object)
    lab[rng] = REGIME_RANGE
    lab[sqz] = REGIME_SQUEEZE
    lab[down] = REGIME_DOWN
    lab[up] = REGIME_UP
    lab[~valid] = np.nan
    return lab


def load_panel(max_symbols=None, seed=20260918, shards_dir=PANEL_DIR):
    """加载日线面板（分片 parquet）→ 单个大 DataFrame，按 (symbol, eob) 排序。

    max_symbols：随机抽 N 只（固定种子，可复现），用于快速试跑。
    """
    import glob
    files = sorted(glob.glob(os.path.join(shards_dir, 'shard_*.parquet')))
    if not files:
        raise FileNotFoundError(f'面板分片不存在: {shards_dir}')
    if max_symbols is None:
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    uni = pd.read_parquet(UNIVERSE)
    syms = uni['symbol'].sample(n=min(max_symbols, len(uni)), random_state=seed).tolist()
    parts = []
    for f in files:
        d = pd.read_parquet(f)
        parts.append(d[d['symbol'].isin(syms)])
    return pd.concat(parts, ignore_index=True)


def build_factor_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """面板 → 因子面板（逐 symbol 计算后拼接，追加 regime 标签与次日字段）。"""
    out = []
    for sym, g in panel.groupby('symbol', sort=False):
        g = compute_symbol_factors(g)
        g['regime'] = label_regimes(g)
        # 次日字段（shift(-1) 是对齐标签用，不是因子输入；因子本身无未来函数）
        nxt_gap = (g['eob'].shift(-1) - g['eob']).dt.days
        has_next = nxt_gap.notna() & (nxt_gap <= MAX_GAP_DAYS)
        o1, h1, l1, c1 = (g['open'].shift(-1), g['high'].shift(-1),
                          g['low'].shift(-1), g['close'].shift(-1))
        g['fwd_cc_ret'] = np.where(has_next, c1 / g['close'] - 1.0, np.nan)   # t收→t+1收
        g['next_amp'] = np.where(has_next, (h1 - l1) / o1.replace(0, np.nan), np.nan)
        g['next_theo'] = np.where(has_next, (h1 - l1) / l1.replace(0, np.nan), np.nan)
        g['next_real'] = np.where(has_next, c1 / o1.replace(0, np.nan) - 1.0, np.nan)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def regime_label(date, symbol, panel=None):
    """单点打标 API：→ regime 字符串（历史不足 → None）。

    panel 不传时自行从磁盘加载该 symbol 的历史（慢，仅调试用；批量请用 build_factor_panel）。
    """
    if panel is None:
        import glob
        frame = None
        for f in sorted(glob.glob(os.path.join(PANEL_DIR, 'shard_*.parquet'))):
            d = pd.read_parquet(f)
            d = d[d['symbol'] == symbol]
            if len(d):
                frame = d
                break
        if frame is None:
            raise KeyError(f'symbol 不在面板中: {symbol}')
        panel = frame
    g = compute_symbol_factors(panel[panel['symbol'] == symbol])
    g['regime'] = label_regimes(g)
    row = g[g['eob'] == pd.Timestamp(date, tz='Asia/Shanghai')]
    if row.empty:
        row = g[g['eob'].dt.strftime('%Y-%m-%d') == str(date)[:10]]
    if row.empty:
        raise KeyError(f'{symbol} 在 {date} 无数据')
    v = row['regime'].iloc[0]
    return None if pd.isna(v) else str(v)


# ════════════════════════════════════════════════════════════════════════════
# IC 层：优先 ic_layer（任务2 真实契约），缺位用同签名内置回退桩
# ════════════════════════════════════════════════════════════════════════════
def _norm_symbol(s: pd.Series) -> pd.Series:
    """'SHSE.600519' → '600519'（向量化；与 ic_layer.load_panel 的口径一致）。"""
    s = s.astype(str)
    parts = s.str.split('.')
    first, second = parts.str[0], parts.str[1]
    return pd.Series(np.where(second.notna() & second.str.isdigit(), second, first),
                     index=s.index)


def _ic_panel(fp: pd.DataFrame) -> pd.DataFrame:
    """fp → ic_layer 契约的 panel：symbol 纯代码、date 去时区、去重排序。"""
    p = fp[['symbol', 'eob', 'open', 'high', 'low', 'close', 'volume', 'amount']].copy()
    p['symbol'] = _norm_symbol(p['symbol'])
    p['date'] = p['eob'].dt.tz_localize(None).dt.normalize()
    p = p.drop(columns='eob').drop_duplicates(['symbol', 'date'])
    return p.sort_values(['symbol', 'date'], kind='mergesort').reset_index(drop=True)


def _ic_factor(fp: pd.DataFrame, col: str) -> pd.DataFrame:
    """fp → ic_layer 契约的因子长表 [date, symbol, value]（symbol 归一纯代码）。"""
    f = pd.DataFrame({'date': fp['eob'].dt.tz_localize(None).dt.normalize(),
                      'symbol': _norm_symbol(fp['symbol']),
                      'value': fp[col].astype(float)})
    return f.dropna(subset=['value'])


# ── 内置回退桩（ic_layer 不存在时使用；与真实契约同签名同口径）─────────────
def _fb_fwd(panel, horizons=(1, 3, 5)):
    """buy_open_h = open(t+1+h)/open(t+1)-1（与 ic_layer 同口径）。"""
    p = panel[['symbol', 'date', 'open']].sort_values(['symbol', 'date'], kind='mergesort')
    g = p.groupby('symbol', sort=False)
    o1 = g['open'].shift(-1)
    out = p[['symbol', 'date']].copy()
    for h in horizons:
        out[f'buy_open_{h}'] = g['open'].shift(-(1 + h)) / o1 - 1.0
    return out


def _fb_evaluate_factor(factor, panel, horizons=(1, 3, 5), min_coverage=30):
    f = factor.dropna(subset=['value'])
    m = f.merge(_fb_fwd(panel, horizons), on=['symbol', 'date'], how='inner')
    res = {}
    for h in sorted(set(horizons)):
        col = f'buy_open_{h}'
        mm = m[['date', 'value', col]].dropna()
        vals = {}
        for d, g in mm.groupby('date', sort=True):
            if len(g) >= min_coverage:
                ic = _spearman(g['value'].to_numpy(), g[col].to_numpy())
                if np.isfinite(ic):
                    vals[d] = ic
        ics = pd.Series(vals, dtype=float)
        n = len(ics)
        mu = float(ics.mean()) if n else float('nan')
        sd = float(ics.std(ddof=1)) if n > 1 else float('nan')
        st = {'rank_ic_mean': mu, 'rank_ic_std': sd,
              'icir': mu / sd if sd and np.isfinite(sd) and sd > 0 else float('nan'),
              'win_rate': float((ics > 0).mean()) if n else float('nan'), 'n_days': n}
        res[h] = {'buy_open': dict(st), 'close': dict(st), **st}
    return res


def _fb_decile_analysis(factor, panel, horizon=1, n_groups=10):
    f = factor.dropna(subset=['value'])
    m = f.merge(_fb_fwd(panel, (horizon,)), on=['symbol', 'date'], how='inner')
    col = f'buy_open_{horizon}'
    m = m.dropna(subset=[col])
    pct = m.groupby('date', sort=True)['value'].rank(method='average', pct=True)
    m = m.assign(grp=np.minimum((pct * n_groups).astype(int) + 1, n_groups))
    gr = m.groupby(['date', 'grp'], sort=True)[col].mean().unstack('grp')
    gr = gr.reindex(columns=list(range(1, n_groups + 1)))
    ls = gr[n_groups] - gr[1]
    means = gr.mean()
    mono = _spearman(means.to_numpy(), means.index.to_numpy(float))
    return {'group_ret': gr, 'long_short': (1.0 + ls.fillna(0.0)).cumprod(),
            'monotonicity': float(mono)}


def _fb_mc_baseline(factor, panel, horizon=1, n=200, seed=42):
    real = _fb_evaluate_factor(factor, panel, (horizon,))[horizon]['rank_ic_mean']
    f = factor.dropna(subset=['value'])
    m = f.merge(_fb_fwd(panel, (horizon,)), on=['symbol', 'date'], how='inner')
    col = f'buy_open_{horizon}'
    m = m.dropna(subset=[col])
    rng = np.random.default_rng(seed)
    null = []
    groups = [g for _, g in m.groupby('date', sort=True) if len(g) >= 30]
    for _ in range(n):
        ics = []
        for g in groups:
            v = g['value'].to_numpy().copy()
            rng.shuffle(v)
            ic = _spearman(v, g[col].to_numpy())
            if np.isfinite(ic):
                ics.append(ic)
        if ics:
            null.append(float(np.mean(ics)))
    null = np.array(null)
    q95 = float(np.percentile(null, 95)) if len(null) else float('nan')
    return {'null_mean': float(null.mean()) if len(null) else float('nan'),
            'null_std': float(null.std(ddof=1)) if len(null) > 1 else float('nan'),
            'mc_rank': float((null < real).mean()) if len(null) else float('nan'),
            'pass': bool(real > q95) if len(null) else False}


def _load_ic_backend():
    try:
        import ic_layer  # 任务2 产出
        return {'evaluate_factor': ic_layer.evaluate_factor,
                'decile_analysis': ic_layer.decile_analysis,
                'mc_baseline': ic_layer.mc_baseline}, 'ic_layer'
    except ImportError:
        return {'evaluate_factor': _fb_evaluate_factor,
                'decile_analysis': _fb_decile_analysis,
                'mc_baseline': _fb_mc_baseline}, 'fallback_stub'


def factor_health_check(fp: pd.DataFrame, factors=FACTOR_COLS, mc_perm=100,
                        horizons=(1, 3, 5)):
    """因子体检：每个因子过 evaluate_factor / decile_analysis / mc_baseline 全检。

    口径（预注册，与 ic_layer 一致）：buy_open 主口径 = open(t+2)/open(t+1)-1
    （T+1 开盘买、次日开盘卖），因子 t 日收盘后可得，无未来函数。
    """
    backend, name = _load_ic_backend()
    panel_ic = _ic_panel(fp)
    res = {}
    for f in factors:
        fdf = _ic_factor(fp, f)
        t0 = time.time()
        ev = backend['evaluate_factor'](fdf, panel_ic, horizons=horizons)
        dc = backend['decile_analysis'](fdf, panel_ic, horizon=1)
        mc_r = backend['mc_baseline'](fdf, panel_ic, horizon=1, n=mc_perm)
        h1 = ev[1]
        gr_means = dc['group_ret'].mean()
        res[f] = {
            'evaluate_factor': {int(h): {
                'rank_ic_mean': round(ev[h]['buy_open']['rank_ic_mean'], 4),
                'rank_ic_std': round(ev[h]['buy_open']['rank_ic_std'], 4),
                'icir': round(ev[h]['buy_open']['icir'], 3),
                'win_rate': round(ev[h]['buy_open']['win_rate'], 3),
                'n_days': int(ev[h]['buy_open']['n_days']),
                'close_ic_mean': round(ev[h]['close']['rank_ic_mean'], 4),
            } for h in sorted(ev)},
            'decile': {'group_mean_ret': {str(int(k)): round(float(v), 5)
                                          for k, v in gr_means.items()},
                       'monotonicity': round(dc['monotonicity'], 3),
                       'long_short_final': round(float(dc['long_short'].iloc[-1]), 4)},
            'mc': {k: (round(v, 4) if isinstance(v, float) else v)
                   for k, v in mc_r.items()},
            'sec': round(time.time() - t0, 1)}
        print(f"  [体检] {f:16s} IC(h1)={h1['buy_open']['rank_ic_mean']:+.4f} "
              f"ICIR={h1['buy_open']['icir']:+.3f} 单调性={dc['monotonicity']:+.3f} "
              f"MC过闸={mc_r['pass']} ({res[f]['sec']}s)", flush=True)
    return {'ic_backend': name,
            'fwd_ret_def': 'buy_open: open(t+1+h)/open(t+1)-1（主）; close 口径参考',
            'horizons': list(horizons), 'mc_perm': mc_perm, 'factors': res}


# ════════════════════════════════════════════════════════════════════════════
# 场景价值验证（本包重点）：各 regime 下次日可捕获波动
# ════════════════════════════════════════════════════════════════════════════
TARGET_GROUP = (REGIME_SQUEEZE, REGIME_UP)   # 策略线② 的目标区间


def scenario_value(fp: pd.DataFrame, n_perm=1000, seed=20260918):
    """按 regime 分组统计次日 amp/theo/real，并检验目标组是否显著更大。

    显著性两条腿：
    1. Mann-Whitney U（日聚合组均值，渐近 p 值，手写实现无 scipy）
    2. 日期内洗牌 MC：消除「高波动日所有 regime 一起波动」的市场级混杂。
    """
    d = fp.dropna(subset=['regime', 'next_amp']).copy()
    d['target'] = d['regime'].isin(TARGET_GROUP)

    # ── 分组分布 ──
    rows = {}
    for r in REGIMES:
        g = d[d['regime'] == r]
        if not len(g):
            continue
        rows[r] = {
            'n': int(len(g)),
            'pct_of_all': round(len(g) / len(d) * 100, 1),
            'amp_mean': round(float(g['next_amp'].mean()), 4),
            'amp_median': round(float(g['next_amp'].median()), 4),
            'amp_q75': round(float(g['next_amp'].quantile(.75)), 4),
            'theo_mean': round(float(g['next_theo'].mean()), 4),
            'real_mean': round(float(g['next_real'].mean()), 5),
            'real_median': round(float(g['next_real'].median()), 5),
        }

    # ── 检验 1：Mann-Whitney U（对日聚合的组均值，避免伪复制）──
    daily = d.groupby(['eob', 'target'])[['next_amp', 'next_theo', 'next_real']].mean().reset_index()
    a = daily[daily['target']].set_index('eob')
    b = daily[~daily['target']].set_index('eob')
    common = a.index.intersection(b.index)
    a, b = a.loc[common], b.loc[common]
    mw = {}
    for col, name in [('next_amp', 'amp'), ('next_theo', 'theo'), ('next_real', 'real')]:
        u, p = _mannwhitney_greater(a[col].to_numpy(), b[col].to_numpy())
        mw[name] = {'target_mean': round(float(a[col].mean()), 5),
                    'other_mean': round(float(b[col].mean()), 5),
                    'diff': round(float(a[col].mean() - b[col].mean()), 5),
                    'diff_pct': round(float((a[col].mean() / b[col].mean() - 1) * 100), 1),
                    'U_p_one_sided': float(f'{p:.3e}'), 'n_days': int(len(common))}

    # ── 检验 2：日期内洗牌 MC（向量化：组内随机键排序 → 前 k 个当作"目标组"）──
    obs_diff = float(a['next_amp'].mean() - b['next_amp'].mean())
    dd = d.sort_values('eob', kind='mergesort')
    # 全市场 9M 行 × n_perm 次 lexsort 过慢：每日截面等概抽样至 cap 行做置换检验
    # （置换检验在抽样子集上仍有效，仅检验力略降；obs_diff 与 MW 用全量）
    cap = 500
    gid_full = pd.factorize(dd['eob'], sort=True)[0]
    if len(dd) > cap * (gid_full.max() + 1):
        rng0 = np.random.default_rng(seed + 1)
        keys0 = rng0.random(len(dd))
        order0 = np.lexsort((keys0, gid_full))
        gid_s0 = gid_full[order0]
        starts0 = np.r_[True, gid_s0[1:] != gid_s0[:-1]]
        grp0 = np.maximum.accumulate(np.where(starts0, np.arange(len(dd)), 0))
        keep_sorted = (np.arange(len(dd)) - grp0) < cap
        keep = np.zeros(len(dd), dtype=bool)
        keep[order0] = keep_sorted
        dd = dd[keep]
    gid = pd.factorize(dd['eob'], sort=True)[0].astype(np.int64)
    amp = dd['next_amp'].to_numpy(float)
    tgt = dd['target'].to_numpy(bool)
    n_days = int(gid.max()) + 1
    k_tgt = np.bincount(gid, weights=tgt.astype(float), minlength=n_days).astype(np.int64)
    n_day = np.bincount(gid, minlength=n_days).astype(np.int64)
    tot = np.bincount(gid, weights=amp, minlength=n_days)
    valid_day = (k_tgt > 0) & (k_tgt < n_day)
    # 每次置换内累积「日期均值差」
    print(f'[scenario] MC 置换 {n_perm} 次 × {len(dd):,} 行（每日上限 {cap} 行抽样）...', flush=True)
    rng = np.random.default_rng(seed)
    cnt = 0
    for _p in range(n_perm):
        keys = rng.random(len(dd))
        order = np.lexsort((keys, gid))          # 组内按随机键排序
        # 排序空间内的组内序号 < k_tgt 的行 → 本次置换的"伪目标组"
        gid_s = gid[order]
        starts = np.r_[True, gid_s[1:] != gid_s[:-1]]
        grp_start = np.maximum.accumulate(np.where(starts, np.arange(len(dd)), 0))
        pos = np.arange(len(dd)) - grp_start
        sel_sorted = pos < k_tgt[gid_s]
        sel = np.empty(len(dd), dtype=bool)
        sel[order] = sel_sorted
        s1 = np.bincount(gid, weights=np.where(sel, amp, 0.0), minlength=n_days)
        with np.errstate(invalid='ignore', divide='ignore'):
            diff = s1 / k_tgt - (tot - s1) / (n_day - k_tgt)
        if np.nanmean(diff[valid_day]) >= obs_diff:
            cnt += 1
        if (_p + 1) % 25 == 0:
            print(f'  [scenario] MC {_p + 1}/{n_perm}', flush=True)
    mc_p = (cnt + 1) / (n_perm + 1)

    verdict = ('证实' if (mw['amp']['U_p_one_sided'] < 0.05 and mw['amp']['diff'] > 0 and mc_p < 0.05)
               else '证伪')
    return {'group_stats': rows, 'mann_whitney': mw,
            'mc_shuffle': {'obs_diff_amp': round(obs_diff, 5), 'p_value': round(mc_p, 4),
                           'n_perm': n_perm},
            'hypothesis': '震荡末期+单边上涨的次日振幅显著大于其他 regime',
            'verdict': verdict}


# ════════════════════════════════════════════════════════════════════════════
# 主流程（分步子命令，每步 < 5 分钟，可断点续跑）
# ════════════════════════════════════════════════════════════════════════════
def _shard_files():
    import glob
    return sorted(glob.glob(os.path.join(PANEL_DIR, 'shard_*.parquet')))


def cmd_build(args):
    """逐分片构建因子面板 → regime_fp_shard_XXXX{tag}.parquet（可并行/续跑）。"""
    files = _shard_files()
    idxs = range(len(files)) if args.shard is None else [args.shard]
    for i in idxs:
        out = os.path.join(HERE, f'regime_fp_shard_{i:04d}{args.tag}.parquet')
        if os.path.exists(out) and not args.force:
            print(f'[build] 跳过已存在 {out}')
            continue
        t0 = time.time()
        panel = pd.read_parquet(files[i])
        fp = build_factor_panel(panel)
        fp.to_parquet(out, index=False)
        print(f'[build] shard {i}: {len(panel):,} 行 × {panel["symbol"].nunique()} 只 '
              f'→ {out} ({time.time() - t0:.0f}s)', flush=True)


def cmd_merge(args):
    """合并分片因子面板 → regime_factor_panel{tag}.parquet + regime 分布打印。"""
    import glob
    parts = sorted(glob.glob(os.path.join(HERE, f'regime_fp_shard_*{args.tag}.parquet')))
    if not parts:
        raise FileNotFoundError(f'无 regime_fp_shard_*{args.tag}.parquet，先跑 build')
    fp = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    out = os.path.join(HERE, f'regime_factor_panel{args.tag}.parquet')
    fp.to_parquet(out, index=False)
    print(f'[merge] {len(parts)} 片 → {len(fp):,} 行 × {fp["symbol"].nunique()} 只 → {out}')
    print('[merge] regime 分布:')
    print(fp['regime'].value_counts(dropna=False).to_string())


def _load_fp(tag=''):
    pq = os.path.join(HERE, f'regime_factor_panel{tag}.parquet')
    if not os.path.exists(pq):
        raise FileNotFoundError(f'先跑 build + merge（缺 {pq}）')
    return pd.read_parquet(pq)


_IC_PANEL_CACHE = os.path.join(HERE, 'regime_ic_panel_cache.parquet')


def _ic_panel_cached(fp: pd.DataFrame, tag: str = '') -> pd.DataFrame:
    """_ic_panel 带磁盘缓存（全市场对齐 ~67s，跨调用复用）。tag 区分窗口。"""
    cache = _IC_PANEL_CACHE.replace('.parquet', f'{tag}.parquet')
    if os.path.exists(cache):
        return pd.read_parquet(cache)
    p = _ic_panel(fp)
    p.to_parquet(cache, index=False)
    return p


def cmd_health(args):
    """因子体检，分步执行（--steps eval|decile|mc，缺省全步）。

    全市场面板下 ic_layer 每次调用都要重做前瞻收益，单因子三步 > 5 分钟，
    故拆步：每步逐因子追加 regime_health_{step}.jsonl，可断点续跑。
    --start-date：窗口右对齐（如 2023-09-01 = 最近 3 年），输出文件名带窗口标签。
    """
    backend, name = _load_ic_backend()
    steps = args.steps.split(',') if args.steps else ['eval', 'decile', 'mc']
    factors = args.factors.split(',') if args.factors else FACTOR_COLS
    wtag = f'_{args.start_date}' if args.start_date else ''
    outs = {s: os.path.join(HERE, f'regime_health_{s}{wtag}{args.tag}.jsonl') for s in steps}
    done = {s: set() for s in steps}
    for s in steps:
        if os.path.exists(outs[s]) and not args.force:
            with open(outs[s], encoding='utf-8') as f:
                done[s] = {json.loads(x)['factor'] for x in f if x.strip()}
    fp = _load_fp(args.tag)
    if args.start_date:
        fp = fp[fp['eob'] >= pd.Timestamp(args.start_date, tz='Asia/Shanghai')]
        print(f'[health] 窗口 {args.start_date} 起 → {len(fp):,} 行', flush=True)
    panel_ic = None
    print(f'[health] 后端={name} steps={steps} tag={args.tag!r}', flush=True)
    for step in steps:
        for fac in factors:
            if fac in done[step]:
                continue
            if panel_ic is None:
                t = time.time()
                panel_ic = _ic_panel_cached(fp, wtag + args.tag)
                print(f'[health] panel 对齐 ({time.time() - t:.0f}s)', flush=True)
            t0 = time.time()
            fdf = _ic_factor(fp, fac)
            if step == 'eval':
                ev = backend['evaluate_factor'](fdf, panel_ic, horizons=(1, 3, 5))
                rec = {'factor': fac, 'ic_backend': name,
                       'evaluate_factor': {str(int(h)): {
                           'rank_ic_mean': round(ev[h]['buy_open']['rank_ic_mean'], 4),
                           'rank_ic_std': round(ev[h]['buy_open']['rank_ic_std'], 4),
                           'icir': round(ev[h]['buy_open']['icir'], 3),
                           'win_rate': round(ev[h]['buy_open']['win_rate'], 3),
                           'n_days': int(ev[h]['buy_open']['n_days']),
                           'close_ic_mean': round(ev[h]['close']['rank_ic_mean'], 4)}
                           for h in sorted(ev)}}
                msg = f"IC(h1)={ev[1]['buy_open']['rank_ic_mean']:+.4f} " \
                      f"ICIR={ev[1]['buy_open']['icir']:+.3f}"
            elif step == 'decile':
                dc = backend['decile_analysis'](fdf, panel_ic, horizon=1)
                rec = {'factor': fac, 'ic_backend': name,
                       'decile': {'group_mean_ret': {str(int(k)): round(float(v), 5)
                                                     for k, v in dc['group_ret'].mean().items()},
                                  'monotonicity': round(dc['monotonicity'], 3),
                                  'long_short_final': round(float(dc['long_short'].iloc[-1]), 4)}}
                msg = f"单调性={dc['monotonicity']:+.3f}"
            else:
                mc_r = backend['mc_baseline'](fdf, panel_ic, horizon=1, n=args.mc_perm)
                rec = {'factor': fac, 'ic_backend': name, 'mc_perm': args.mc_perm,
                       'mc': {k: (round(v, 4) if isinstance(v, float) else v)
                              for k, v in mc_r.items()}}
                msg = f"MC过闸={mc_r['pass']} rank={mc_r['mc_rank']:.2f}"
            with open(outs[step], 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
            print(f'  [health:{step}] {fac:16s} {msg} ({time.time() - t0:.0f}s)', flush=True)


def cmd_scenario(args):
    fp = _load_fp(args.tag)
    print(f'[scenario] 面板 {len(fp):,} 行，开始分组统计 ...', flush=True)
    t0 = time.time()
    sv = scenario_value(fp, n_perm=args.scenario_perm)
    print(f'[scenario] 计算完成 ({time.time() - t0:.0f}s)', flush=True)
    out = os.path.join(HERE, f'regime_scenario{args.tag}.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(sv, f, ensure_ascii=False, indent=2, default=str)
    print(f"[scenario] 结论={sv['verdict']}  amp差={sv['mc_shuffle']['obs_diff_amp']:+.5f} "
          f"MC_p={sv['mc_shuffle']['p_value']} → {out}")
    for r, s in sv['group_stats'].items():
        print(f"  {r:6s} n={s['n']:>9,} amp均={s['amp_mean']:.4f} "
              f"theo均={s['theo_mean']:.4f} real均={s['real_mean']:+.5f}")


def cmd_report_meta(args):
    """汇总 meta + 分布 + 体检 jsonl → regime_results.json（报告数据源）。

    体检读取优先级：3 年窗口（regime_health_*_2023-09-01.jsonl）> 全历史。
    """
    fp = _load_fp(args.tag)
    dist = fp['regime'].value_counts(dropna=False)
    health = {}
    for step in ('eval', 'decile', 'mc'):
        for wtag in ('_2023-09-01', ''):
            hp = os.path.join(HERE, f'regime_health_{step}{wtag}{args.tag}.jsonl')
            if os.path.exists(hp):
                with open(hp, encoding='utf-8') as f:
                    for x in f:
                        if x.strip():
                            r = json.loads(x)
                            rec = health.setdefault(r['factor'], {})
                            # 窗口版优先：已有窗口版记录时，全历史版不覆盖
                            if wtag == '' and str(rec.get('window', '')).startswith('近3年'):
                                continue
                            r['window'] = '近3年(2023-09起)' if wtag else 'full'
                            rec.update(r)
    sp = os.path.join(HERE, f'regime_scenario{args.tag}.json')
    sv = json.load(open(sp, encoding='utf-8')) if os.path.exists(sp) else None
    result = {'meta': {'date': '2026-09-18', 'n_rows': int(len(fp)),
                       'n_symbols': int(fp['symbol'].nunique()),
                       'panel_range': [str(fp['eob'].min()), str(fp['eob'].max())]},
              'regime_dist': {str(k): int(v) for k, v in dist.items()},
              'health_check': health, 'scenario_value': sv}
    out = os.path.join(HERE, f'regime_results{args.tag}.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f'[report] → {out}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name, fn in [('build', cmd_build), ('merge', cmd_merge),
                     ('health', cmd_health), ('scenario', cmd_scenario),
                     ('report', cmd_report_meta)]:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        p.add_argument('--force', action='store_true')
        p.add_argument('--tag', default='',
                       help="输出文件名后缀标签，如 '_prevadj'（保留旧结果，新旧可比）")
        if name == 'build':
            p.add_argument('--shard', type=int, default=None, help='只跑第 N 片')
            p.add_argument('--max-symbols', type=int, default=None, help='采样试跑')
        if name == 'health':
            p.add_argument('--factors', default=None)
            p.add_argument('--steps', default=None, help='eval,decile,mc 子集')
            p.add_argument('--mc-perm', type=int, default=100)
            p.add_argument('--start-date', default=None, help='窗口起点，如 2023-09-01')
        if name == 'scenario':
            p.add_argument('--scenario-perm', type=int, default=200)
    args = ap.parse_args()
    if args.cmd == 'build' and args.max_symbols:
        # 采样试跑：单文件直出，不分片
        panel = load_panel(args.max_symbols)
        fp = build_factor_panel(panel)
        out = os.path.join(HERE, f'regime_factor_panel{args.tag}.parquet')
        fp.to_parquet(out, index=False)
        print(f'[build] 采样 {args.max_symbols} 只 → {out} ({len(fp):,} 行)')
        return
    args.fn(args)


if __name__ == '__main__':
    main()
