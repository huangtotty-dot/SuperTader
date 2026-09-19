# -*- coding: utf-8 -*-
"""分钟级因子挖掘统一数据加载 API（因子挖掘阶段2 / 任务S2-2，2026-09-19）。

补齐本地缺口：此前每个实验脚本（如 macd_divergence_t/run_experiment_v2.py 的 merge_days）
各自写一遍「CSV + 盘中快照」双源合并，本模块把它收敛为唯一入口。

数据源：
  A. t_io/backtest_1year_data/   39 只 × 1 年 1min CSV（gm 缓存）
     字段 ts_code,time,close,open,high,low,volume,amount；约 5.8 万根/只；
     文件名两种形态：<code>_1year_1min.csv 与 <code>.SZ_1year_1min.csv。
  B. t_io/minute_snapshots/{年}/{月}/  盘中分钟快照 JSON
     文件名严格 <code>_YYYY-MM-DD.json（带 _A/_B 账户后缀的文件一律排除）；
     bars[] 字段 time/open/high/low/close/volume/amount。

合并规则（沿用 B7 口径）：同一 (票, 交易日) 两源都有时，整日照搬根数更多者
（平手取 CSV）；不做逐根拼接，避免标签口径（起点/终点）混排。

纪律：
  - 无未来函数设计：本模块只提供「截至某时点已存在的数据」，不提供任何
    依赖当日收盘价/全日统计的便利函数；日内切片、重采样均由调用方显式触发。
  - 只用 pandas / numpy（managed python 3.12, pandas 3.0.2 / numpy 2.4.4 可跑）。

API：
  load_minutes(symbol, start=None, end=None) -> DataFrame[time,open,high,low,close,volume,amount]
  load_pool(symbols=None, start=None, end=None) -> dict[symbol, DataFrame]
  resample_bars(df, freq='5min') -> DataFrame（bar 不跨午休、不跨日）
  iter_days(df) -> Iterator[(date_str, day_df)]
  health_report(symbols=None, start=None, end=None) -> DataFrame（每只覆盖度与异常统计）
"""
import sys
import time as _time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
CSV_DIR = ROOT / 't_io' / 'backtest_1year_data'
SNAP_DIR = ROOT / 't_io' / 'minute_snapshots'

BAR_COLS = ['time', 'open', 'high', 'low', 'close', 'volume', 'amount']

# A股连续竞价时段（分钟标签落在闭区间内即视为该时段的 bar）
SESSIONS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
SESSION_MINUTES = 120  # 每时段 120 分钟

# health_report 阈值
MIN_DAY_BARS = 200      # 全日不足 200 根视为「缺口日」（完整日约 240/241 根）
JUMP_RET = 0.11         # 日内相邻 bar 收益绝对值超过 11% 视为价格跳变异常

import re
_SNAP_RE = re.compile(r'^(\d{6})_(\d{4}-\d{2}-\d{2})\.json$')


# ---------------- 代码归一化 ----------------

def norm_symbol(symbol):
    """归一化为 6 位数字代码：'000988.SZ' / 'SZ000988' / '000988' -> '000988'。"""
    s = str(symbol).strip().upper()
    m = re.search(r'(\d{6})', s)
    if not m:
        raise ValueError(f'无法解析股票代码: {symbol!r}')
    return m.group(1)


def pool_symbols():
    """CSV 全集（默认池）：backtest_1year_data 下全部 6 位代码，排序返回。"""
    codes = set()
    for f in CSV_DIR.glob('*1min.csv'):
        m = re.match(r'^(\d{6})', f.name)
        if m:
            codes.add(m.group(1))
    return sorted(codes)


def snapshot_symbols():
    """快照源覆盖的全部 6 位代码（排除 _A/_B 账户后缀文件）。"""
    codes = set()
    for f in SNAP_DIR.glob('20*/*/*.json'):
        m = _SNAP_RE.match(f.name)
        if m:
            codes.add(m.group(1))
    return sorted(codes)


# ---------------- 单源加载 ----------------

def _csv_path(symbol):
    """定位某代码的 1min CSV（兼容 000988_*.csv 与 000988.SZ_*.csv 两种命名）。"""
    for f in sorted(CSV_DIR.glob(symbol + '*1min.csv')):
        rest = f.name[len(symbol):]
        if rest.startswith(('_', '.')):
            return f
    return None


def load_csv(symbol, start=None, end=None):
    """加载 A 源（gm 一年期 1min CSV）。返回标准 BAR_COLS 列，time 为 Timestamp。"""
    symbol = norm_symbol(symbol)
    f = _csv_path(symbol)
    if f is None:
        return pd.DataFrame(columns=BAR_COLS)
    df = pd.read_csv(f)
    out = pd.DataFrame({
        'time': pd.to_datetime(df['time']),
        'open': df['open'].astype(float),
        'high': df['high'].astype(float),
        'low': df['low'].astype(float),
        'close': df['close'].astype(float),
        'volume': df['volume'].astype(float),
        'amount': df['amount'].astype(float),
    })
    return _clip(out, start, end)


def load_snapshots(symbol, start=None, end=None):
    """加载 B 源（盘中分钟快照 JSON）。文件名严格匹配，排除 _A/_B 后缀。"""
    import json
    symbol = norm_symbol(symbol)
    rows = []
    for f in sorted(SNAP_DIR.glob('20*/*/' + symbol + '_*.json')):
        m = _SNAP_RE.match(f.name)
        if not m or m.group(1) != symbol:
            continue
        dt = m.group(2)
        if start is not None and dt < str(start):
            continue
        if end is not None and dt > str(end):
            continue
        d = json.loads(f.read_text(encoding='utf-8'))
        for b in d.get('bars', []):
            rows.append((b['time'], b['open'], b['high'], b['low'],
                         b['close'], b['volume'], b['amount']))
    if not rows:
        return pd.DataFrame(columns=BAR_COLS)
    out = pd.DataFrame(rows, columns=BAR_COLS)
    out['time'] = pd.to_datetime(out['time'])
    for c in BAR_COLS[1:]:
        out[c] = out[c].astype(float)
    return out


def _clip(df, start, end):
    if df.empty:
        return df
    if start is not None:
        df = df[df['time'] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df['time'] < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df


# ---------------- 双源合并 ----------------

def merge_frames(csv_df, snap_df):
    """双源合并核心：同一交易日两源取根数多者（平手取 CSV），去重、排序。

    两个输入均为 BAR_COLS 格式；返回同样格式，time 升序、无重复。
    """
    frames = []
    for src, df in (('csv', csv_df), ('snap', snap_df)):
        if df is not None and not df.empty:
            frames.append(df.assign(_src=src))
    if not frames:
        return pd.DataFrame(columns=BAR_COLS)
    both = pd.concat(frames, ignore_index=True)
    both['_date'] = both['time'].dt.date

    # 每 (date, src) 根数 -> 选每日胜者
    cnt = both.groupby(['_date', '_src'], observed=True).size().unstack(fill_value=0)
    for col in ('csv', 'snap'):
        if col not in cnt.columns:
            cnt[col] = 0
    winner = np.where(cnt['snap'] > cnt['csv'], 'snap', 'csv')
    win_map = dict(zip(cnt.index, winner))

    keep = both['_date'].map(win_map) == both['_src']
    out = both.loc[keep, BAR_COLS]
    out = out.drop_duplicates(subset='time', keep='first')
    return out.sort_values('time').reset_index(drop=True)


def load_minutes(symbol, start=None, end=None):
    """加载某只标的的合并分钟线。

    返回 DataFrame[time,open,high,low,close,volume,amount]，
    time 为 pd.Timestamp 且严格升序、无重复。start/end 为 'YYYY-MM-DD' 闭区间。
    """
    symbol = norm_symbol(symbol)
    csv_df = load_csv(symbol, start, end)
    snap_df = load_snapshots(symbol, start, end)
    return merge_frames(csv_df, snap_df)


def load_pool(symbols=None, start=None, end=None, verbose=False):
    """批量加载。默认 symbols=None 即 CSV 全集（39 只）。

    返回 dict[symbol, DataFrame]；无数据的代码不省略（值为空 DataFrame）。
    """
    if symbols is None:
        symbols = pool_symbols()
    out = {}
    t0 = _time.perf_counter()
    for i, s in enumerate(symbols):
        s = norm_symbol(s)
        out[s] = load_minutes(s, start, end)
        if verbose:
            print(f'  [{i + 1}/{len(symbols)}] {s}: {len(out[s])} 根 '
                  f'({_time.perf_counter() - t0:.1f}s 累计)')
    return out


# ---------------- 重采样 ----------------

def _session_bucket(times, freq_min):
    """把 bar 时间映射为 (date, session_start_minute, bucket) 三键。

    时段内偏移 offset = 分钟标签 - 时段起点；bucket = offset // freq_min，
    恰好落在时段终点标签的 bar（如 11:30、15:00）并入最后一个桶。
    时段外的 bar 返回 NaN（调用方丢弃）。这样 bar 天然不跨午休、不跨日。
    """
    minutes = times.dt.hour * 60 + times.dt.minute
    sess_start = np.full(len(times), np.nan)
    offset = np.full(len(times), np.nan)
    for lo, hi in SESSIONS:
        m = (minutes >= lo) & (minutes <= hi)
        sess_start[m] = lo
        offset[m] = np.minimum(minutes[m] - lo, SESSION_MINUTES - 1)
    bucket = np.floor(offset / freq_min)
    return sess_start, bucket


def resample_bars(df, freq='5min'):
    """1min -> 5min/15min 重采样。

    约束：bar 不跨午休、不跨日（按交易日 × 时段内分桶）。
    聚合：open=first, high=max, low=min, close=last, volume/amount=sum。
    time 标签 = 桶起点（如 5min 桶 09:30 覆盖 09:30-09:34 标签的 1min bar）。
    时段外的 bar 丢弃。freq 支持 '5min' / '15min'（或任意 'Nmin'）。
    注意：恰好落在时段终点标签的 bar（11:30 / 15:00，gm CSV 为终点标签口径）
    并入当段最后一桶，故该桶最多含 freq+1 根 1min bar；量额守恒不受影响。
    """
    if df.empty:
        return pd.DataFrame(columns=BAR_COLS)
    freq_min = int(str(freq).replace('min', ''))
    if SESSION_MINUTES % freq_min != 0:
        raise ValueError(f'freq={freq} 不能整除单时段 {SESSION_MINUTES} 分钟')

    df = df.sort_values('time').reset_index(drop=True)
    sess_start, bucket = _session_bucket(df['time'], freq_min)
    ok = ~np.isnan(sess_start)
    df = df.loc[ok].copy()
    df['_date'] = df['time'].dt.date
    df['_sess'] = sess_start[ok].astype(int)
    df['_bucket'] = bucket[ok].astype(int)

    g = df.groupby(['_date', '_sess', '_bucket'], observed=True, sort=True)
    out = pd.DataFrame({
        'open': g['open'].first(),
        'high': g['high'].max(),
        'low': g['low'].min(),
        'close': g['close'].last(),
        'volume': g['volume'].sum(),
        'amount': g['amount'].sum(),
        '_n1m': g.size(),
    }).reset_index()
    base = pd.to_datetime(out['_date']) + pd.to_timedelta(
        out['_sess'] + out['_bucket'] * freq_min, unit='min')
    out['time'] = base
    return out[['time', 'open', 'high', 'low', 'close', 'volume', 'amount',
                '_n1m']].rename(columns={'_n1m': 'n_1min'}).reset_index(drop=True)


# ---------------- 按日迭代 ----------------

def iter_days(df):
    """按交易日切片迭代器：yield (date_str 'YYYY-MM-DD', day_df)。

    day_df 为当日全部 bar（升序），供 GP 适应度按日评估逐日取用。
    """
    if df.empty:
        return
    dates = df['time'].dt.date.to_numpy()
    for d in pd.unique(dates):
        day = df.loc[dates == d]
        yield str(d), day


# ---------------- 数据健康检查 ----------------

def health_report(symbols=None, start=None, end=None, verbose=False):
    """每只标的的覆盖度与异常统计，返回 DataFrame（每行一只）。

    列：
      n_bars / n_days / first_date / last_date     覆盖度
      incomplete_days      根数 < MIN_DAY_BARS 的「缺口日」数量
      missing_bars_est     缺口日估计缺根数（相对 240 根/日）
      dup_times            合并后仍重复的时间戳数（应为 0）
      zero_vol_bars        volume==0 的 bar 数
      price_jumps          日内相邻 bar |收益| > JUMP_RET 的次数（跨日不计）
    """
    if symbols is None:
        symbols = pool_symbols()
    rows = []
    for i, s in enumerate(symbols):
        s = norm_symbol(s)
        df = load_minutes(s, start, end)
        rec = {'symbol': s, 'n_bars': len(df)}
        if df.empty:
            rec.update(n_days=0, first_date=None, last_date=None,
                       incomplete_days=0, missing_bars_est=0, dup_times=0,
                       zero_vol_bars=0, price_jumps=0)
            rows.append(rec)
            continue
        dkey = df['time'].dt.date
        per_day = df.groupby(dkey, observed=True).size()
        close = df['close'].to_numpy()
        same_day = dkey.to_numpy()[:-1] == dkey.to_numpy()[1:]
        with np.errstate(divide='ignore', invalid='ignore'):
            ret = np.abs(close[1:] / close[:-1] - 1.0)
        rec.update(
            n_days=int(per_day.size),
            first_date=str(df['time'].iloc[0].date()),
            last_date=str(df['time'].iloc[-1].date()),
            incomplete_days=int((per_day < MIN_DAY_BARS).sum()),
            missing_bars_est=int((240 - per_day[per_day < 240]).sum()),
            dup_times=int(df['time'].duplicated().sum()),
            zero_vol_bars=int((df['volume'] <= 0).sum()),
            price_jumps=int(((ret > JUMP_RET) & same_day).sum()),
        )
        rows.append(rec)
        if verbose:
            print(f'  [{i + 1}/{len(symbols)}] {s}: {rec["n_bars"]} 根 / '
                  f'{rec["n_days"]} 日 / 缺口日 {rec["incomplete_days"]}')
    return pd.DataFrame(rows)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print('=== minute_data 健康检查（全池）===')
    t0 = _time.perf_counter()
    rep = health_report(verbose=True)
    dt = _time.perf_counter() - t0
    print(f'\n全池加载+检查耗时: {dt:.1f}s')
    print(rep.to_string(index=False))
    out = ROOT / 't_io' / 'validation' / 'factor_mining' / 'minute_data_health.csv'
    rep.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'\n报告已写出: {out}')
