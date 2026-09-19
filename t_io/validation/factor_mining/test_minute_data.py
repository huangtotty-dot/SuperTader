# -*- coding: utf-8 -*-
"""minute_data 配套测试（脚本式断言，直接 python 运行，不依赖 pytest）。

覆盖：
  1. norm_symbol / pool_symbols 归一化与池发现
  2. merge_frames 合成小数据：同日两源取根数多者、平手取 CSV、去重、排序
  3. merge_frames 真实数据抽样（000988）：重叠日根数 == max(csv, snap)
  4. resample_bars 合成：不跨午休/不跨日、聚合正确、量额守恒
  5. resample_bars 真实抽样：每日 volume/amount 守恒、标签全部落在时段内
  6. iter_days：合成多日切片完整、升序、拼回等于原帧
  7. load_minutes 真实抽样：time 为 Timestamp、严格升序、无重复
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from t_io.validation.factor_mining.minute_data import (
    BAR_COLS, SESSIONS, iter_days, load_csv, load_minutes, load_pool,
    load_snapshots, merge_frames, norm_symbol, pool_symbols, resample_bars,
    snapshot_symbols,
)

sys.stdout.reconfigure(encoding='utf-8')

PASS = []


def check(name, ok, detail=''):
    PASS.append((name, bool(ok)))
    print(('  [PASS] ' if ok else '  [FAIL] ') + name + ((' | ' + detail) if detail else ''))


def _mk_rows(date, times, price=10.0):
    """合成某日若干根 1min bar。times 为 'HH:MM' 列表。"""
    rows = []
    for i, t in enumerate(times):
        rows.append({'time': pd.Timestamp(f'{date} {t}:00'),
                     'open': price + i * 0.01, 'high': price + i * 0.01 + 0.02,
                     'low': price + i * 0.01 - 0.02, 'close': price + i * 0.01 + 0.01,
                     'volume': 100.0 + i, 'amount': (100.0 + i) * price})
    return pd.DataFrame(rows, columns=BAR_COLS)


def _full_day_times(date):
    """A股完整交易日的 1min 标签：09:30-11:30, 13:00-15:00（共 242 根）。"""
    ts = pd.date_range(f'{date} 09:30', f'{date} 11:30', freq='1min')
    ts = ts.append(pd.date_range(f'{date} 13:00', f'{date} 15:00', freq='1min'))
    return ts


# ---------------- 1. 代码归一化与池发现 ----------------
print('== 1. 归一化与池 ==')
check('norm_symbol 000988.SZ', norm_symbol('000988.SZ') == '000988')
check('norm_symbol SHSE.600519 提取数字', norm_symbol('SHSE.600519') == '600519')
pool = pool_symbols()
check('CSV 池 = 39 只', len(pool) == 39, f'实际 {len(pool)}')
check('池内均为 6 位数字', all(len(s) == 6 and s.isdigit() for s in pool))
snaps = snapshot_symbols()
check('快照源代码 >= 30 只', len(snaps) >= 30, f'实际 {len(snaps)}')

# ---------------- 2. merge_frames 合成 ----------------
print('== 2. merge_frames 合成 ==')
day1_csv = _mk_rows('2026-09-01', ['09:30', '09:31', '09:32'])          # 3 根
day1_snap = _mk_rows('2026-09-01', ['09:30', '09:31'])                   # 2 根 -> 取 csv
day2_csv = _mk_rows('2026-09-02', ['09:30', '09:31'])                    # 2 根
day2_snap = _mk_rows('2026-09-02', ['09:30', '09:31', '09:32', '09:33']) # 4 根 -> 取 snap
day3_csv = _mk_rows('2026-09-03', ['09:30', '09:31'])                    # 平手 -> csv
day3_snap = _mk_rows('2026-09-03', ['09:30', '09:31'])
csv_df = pd.concat([day1_csv, day2_csv, day3_csv], ignore_index=True)
snap_df = pd.concat([day1_snap, day2_snap, day3_snap], ignore_index=True)
mg = merge_frames(csv_df, snap_df)
check('合并总行数 3+4+2=9', len(mg) == 9, f'实际 {len(mg)}')
check('time 严格升序', mg['time'].is_monotonic_increasing)
check('time 无重复', not mg['time'].duplicated().any())
# 平手日取 CSV：09:30 根 open 应等于 csv 版的 10.0（两源该值相同，用 09:31 的 open 区分）
d3 = mg[mg['time'].dt.date.astype(str) == '2026-09-03']
check('平手日取 CSV（数值一致）', np.isclose(d3.iloc[1]['open'], day3_csv.iloc[1]['open']))
# snap 胜日取 snap 版数值
d2 = mg[mg['time'].dt.date.astype(str) == '2026-09-02']
check('根数多者胜（snap 4 根入选）', len(d2) == 4 and np.isclose(d2.iloc[3]['open'], day2_snap.iloc[3]['open']))
# 空帧稳健
check('双空合并返回空帧', merge_frames(pd.DataFrame(columns=BAR_COLS),
                                       pd.DataFrame(columns=BAR_COLS)).empty)

# ---------------- 3. merge 真实数据抽样 ----------------
print('== 3. merge 真实抽样（000988）==')
csv_r = load_csv('000988')
snap_r = load_snapshots('000988')
mg_r = merge_frames(csv_r, snap_r)
check('真实合并非空', len(mg_r) > 50000, f'{len(mg_r)} 根')
check('真实合并 time 升序无重复',
      mg_r['time'].is_monotonic_increasing and not mg_r['time'].duplicated().any())
# 重叠日校验：逐日根数 == max(csv, snap)
csv_cnt = csv_r.groupby(csv_r['time'].dt.date, observed=True).size()
snap_cnt = snap_r.groupby(snap_r['time'].dt.date, observed=True).size()
mg_cnt = mg_r.groupby(mg_r['time'].dt.date, observed=True).size()
overlap = csv_cnt.index.intersection(snap_cnt.index)
expect = pd.DataFrame({'csv': csv_cnt, 'snap': snap_cnt}).fillna(0).max(axis=1)
ok_overlap = all(mg_cnt[d] == max(csv_cnt[d], snap_cnt[d]) for d in overlap)
ok_all = mg_cnt.sort_index().equals(expect.sort_index().astype(mg_cnt.dtype))
check(f'重叠日 {len(overlap)} 天逐日取根数多者', ok_overlap)
check('全日历根数 == 两源逐日 max', ok_all)

# ---------------- 4. resample 合成 ----------------
print('== 4. resample 合成 ==')
ts = _full_day_times('2026-09-07')
one_day = pd.DataFrame({
    'time': ts, 'open': 10.0, 'high': 10.1, 'low': 9.9, 'close': 10.05,
    'volume': 1.0, 'amount': 10.0,
})
r5 = resample_bars(one_day, '5min')
check('5min 全日 48 桶', len(r5) == 48, f'实际 {len(r5)}')
check('首桶标签 09:30', r5['time'].iloc[0] == pd.Timestamp('2026-09-07 09:30'))
check('末桶标签 14:55', r5['time'].iloc[-1] == pd.Timestamp('2026-09-07 14:55'))
labels = set(r5['time'].dt.strftime('%H:%M'))
check('不存在跨午休桶（11:30/12:xx 无标签）',
      '11:30' not in labels and not any(l.startswith('12:') for l in labels))
check('5min 量守恒 242', r5['volume'].sum() == 242.0)
check('5min 额守恒', np.isclose(r5['amount'].sum(), 2420.0))
check('每桶 1min 根数 <=6（时段终点标签并入末桶）', (r5['n_1min'] <= 6).all())
check('仅末桶可能 6 根', int((r5['n_1min'] == 6).sum()) == 2)  # 11:25 与 14:55 两桶
check('open=首根 high=max close=末根',
      np.isclose(r5['open'].iloc[0], 10.0) and np.isclose(r5['close'].iloc[0], 10.05)
      and np.isclose(r5['high'].iloc[0], 10.1))
r15 = resample_bars(one_day, '15min')
check('15min 全日 16 桶', len(r15) == 16, f'实际 {len(r15)}')
check('15min 末桶 14:45', r15['time'].iloc[-1] == pd.Timestamp('2026-09-07 14:45'))
# 不跨日：两日数据重采样后按日分组，各 48 桶
two_days = pd.concat([one_day, one_day.assign(
    time=one_day['time'] + pd.Timedelta(days=1))], ignore_index=True)
r5b = resample_bars(two_days, '5min')
per_day = r5b.groupby(r5b['time'].dt.date, observed=True).size()
check('两日各自 48 桶（不跨日）', list(per_day) == [48, 48])

# ---------------- 5. resample 真实抽样守恒 ----------------
print('== 5. resample 真实抽样（000988 近 20 日）==')
recent = mg_r[mg_r['time'] >= mg_r['time'].max() - pd.Timedelta(days=40)]
r5r = resample_bars(recent, '5min')
src_day = recent.groupby(recent['time'].dt.date, observed=True)[['volume', 'amount']].sum()
dst_day = r5r.groupby(r5r['time'].dt.date, observed=True)[['volume', 'amount']].sum()
merged = src_day.join(dst_day, lsuffix='_src', rsuffix='_dst')
check('真实 5min 逐日量守恒',
      bool(np.allclose(merged['volume_src'], merged['volume_dst'])))
check('真实 5min 逐日额守恒',
      bool(np.allclose(merged['amount_src'], merged['amount_dst'], rtol=1e-9)))
mins = r5r['time'].dt.hour * 60 + r5r['time'].dt.minute
in_sess = np.zeros(len(r5r), dtype=bool)
for lo, hi in SESSIONS:
    in_sess |= (mins >= lo) & (mins < hi)
check('真实 5min 标签全部落在时段内', bool(in_sess.all()))

# ---------------- 6. iter_days ----------------
print('== 6. iter_days ==')
days = list(iter_days(two_days))
check('迭代出 2 个交易日', len(days) == 2)
check('日期字符串升序', [d for d, _ in days] == ['2026-09-07', '2026-09-08'])
check('每片 242 根', all(len(g) == 242 for _, g in days))
rebuilt = pd.concat([g for _, g in days]).sort_values('time').reset_index(drop=True)
check('切片拼回 == 原帧（量额一致）',
      np.isclose(rebuilt['volume'].sum(), two_days['volume'].sum())
      and rebuilt['time'].equals(two_days.sort_values('time').reset_index(drop=True)['time']))
check('空帧迭代为零片', len(list(iter_days(pd.DataFrame(columns=BAR_COLS)))) == 0)

# ---------------- 7. load_minutes / load_pool ----------------
print('== 7. load_minutes / load_pool 真实抽样 ==')
lm = load_minutes('000988', start='2026-08-20', end='2026-09-15')
check('区间裁剪生效', str(lm['time'].min().date()) >= '2026-08-20'
      and str(lm['time'].max().date()) <= '2026-09-15')
check('time 为 datetime64', pd.api.types.is_datetime64_any_dtype(lm['time']))
check('列结构正确', list(lm.columns) == BAR_COLS)
sub = load_pool(['000988', '600481'], start='2026-09-01', end='2026-09-10')
check('load_pool 子集两只', set(sub) == {'000988', '600481'})
check('子集两只均非空', all(not df.empty for df in sub.values()))
only_csv = load_minutes('000506')  # 无快照覆盖的纯 CSV 标的
check('纯 CSV 标的加载非空', len(only_csv) > 40000, f'{len(only_csv)} 根')

# ---------------- 汇总 ----------------
n_fail = sum(1 for _, ok in PASS if not ok)
print(f'\n===== {len(PASS) - n_fail}/{len(PASS)} 通过 =====')
sys.exit(1 if n_fail else 0)
