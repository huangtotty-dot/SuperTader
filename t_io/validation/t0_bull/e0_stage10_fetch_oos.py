# -*- coding: utf-8 -*-
"""E0 Stage10：拉样本外 30min 历史（2019-01-01 ~ 2025-03-28）—— 预注册见同名 .md。

只存**日级归约**（每票每日一行），不留 30min 明细 —— 既够用，又避免把大文件塞进仓库
（.gitignore 对 t_io/** 整体忽略，但历史上出过 `git add -f` 把 292MB parquet 强加进
提交、导致 push 被拒的事故）。

用法：
    export TUSHARE_TOKEN=<token>
    python e0_stage10_fetch_oos.py            # 全量 981 只，可续跑
    python e0_stage10_fetch_oos.py --limit 3  # 小样本试跑
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from ts_http import stk_mins_30, retry, TushareError  # noqa: E402

SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
OUT = HERE / 'ts_oos'
START, END = '2019-01-01', '2025-03-28'      # 现有面板起点 2025-03-31 之前
CHUNK_DAYS = 850                             # ≈615 交易日 ≈5535 行 < 8000 上限


def pool() -> list[str]:
    """与现有面板同一批票（从 d540 文件名取），转成 tushare ts_code。"""
    out = []
    for f in sorted(SRC.iterdir()):
        m = re.match(r'^(\d{6})\.(SH|SZ)_30min_d540\.json$', f.name)
        if m:
            out.append(f'{m.group(1)}.{m.group(2)}')
    return out


def chunks(start: str, end: str) -> list[tuple[str, str]]:
    s, e = datetime.strptime(start, '%Y-%m-%d'), datetime.strptime(end, '%Y-%m-%d')
    out = []
    while s <= e:
        c = min(s + timedelta(days=CHUNK_DAYS), e)
        out.append((s.strftime('%Y-%m-%d 09:00:00'), c.strftime('%Y-%m-%d 15:00:00')))
        s = c + timedelta(days=1)
    return out


def reduce_day(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """30min 明细 → 日级归约。

    ⚠️ 标签是**区间结束**时刻：`09:30` bar = 集合竞价（开盘价来源），
    `10:00` bar = 09:30–10:00 区间（其 close 即 10:00 价），`15:00` bar = 收盘。
    """
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    if 'vol' not in df.columns and 'volume' in df.columns:
        df['vol'] = df['volume']          # d540 缓存用 volume，tushare 接口用 vol
    df['tt'] = df['trade_time'].astype(str)
    df['date'] = df['tt'].str[:10]
    df['hhmm'] = df['tt'].str[11:16]
    for c in ('open', 'high', 'low', 'close', 'vol', 'amount'):
        df[c] = pd.to_numeric(df[c], errors='coerce')

    auc = df[df['hhmm'] == '09:30'].set_index('date')['open']
    t10 = df[df['hhmm'] == '10:00'].set_index('date')['close']
    c15 = df[df['hhmm'] == '15:00'].set_index('date')['close']
    amt = df.groupby('date')['amount'].sum()
    rows = pd.DataFrame({'op_auc': auc, 'cl_1000': t10, 'cl_1500': c15,
                         'amt': amt}).dropna(subset=['op_auc', 'cl_1500'])
    if rows.empty:
        return pd.DataFrame()
    rows = rows.reset_index()[['date', 'op_auc', 'cl_1000', 'cl_1500', 'amt']]
    rows['code'] = code
    return rows.sort_values('date').reset_index(drop=True)


def fetch_one(code: str, verbose: bool = False) -> pd.DataFrame:
    parts = []
    for a, b in chunks(START, END):
        d = retry(stk_mins_30, code, a, b)
        if not d.empty:
            parts.append(d)
        time.sleep(0.12)                     # 温和限速
    if not parts:
        return pd.DataFrame()
    raw = pd.concat(parts, ignore_index=True)
    if verbose:
        print(f'    {code}: 明细 {len(raw)} 行  '
              f'{raw["trade_time"].min()} ~ {raw["trade_time"].max()}')
    return reduce_day(raw, code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=6, help='并发票数')
    ap.add_argument('--sleep', type=float, default=0.0, help='每票之间的额外休眠（秒）')
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    codes = pool()
    if args.limit:
        codes = codes[:args.limit]
    todo = [c for c in codes
            if not (OUT / f'{c.replace(".", "_")}.parquet').exists()]
    print(f'目标 {len(codes)} 只   区间 {START} ~ {END}   '
          f'{len(chunks(START, END))} 块/票   待拉 {len(todo)}   workers={args.workers}'
          f'   → {OUT}')

    t0 = time.perf_counter()
    ok = fail = empty = 0
    done = 0
    lock = threading.Lock()

    def work(code: str):
        try:
            d = fetch_one(code)
        except TushareError as e:
            with lock:
                print(f'  {code} 失败: {str(e)[:100]}')
            return code, 'fail', None
        if d.empty:
            return code, 'empty', None
        return code, 'ok', d

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in cf.as_completed(futs):
            code, st, d = fut.result()
            if st == 'ok':
                (OUT / f'{code.replace(".", "_")}.parquet').parent.mkdir(parents=True, exist_ok=True)
                d.to_parquet(OUT / f'{code.replace(".", "_")}.parquet', index=False)
                ok += 1
            elif st == 'empty':
                empty += 1
            else:
                fail += 1
            done += 1
            if done % 25 == 0 or done == len(todo):
                el = time.perf_counter() - t0
                print(f'  [{done}/{len(todo)}] ok={ok} 空={empty} 失败={fail}  '
                      f'{el:.0f}s  均 {el/max(done,1):.2f}s/票', flush=True)
            if args.sleep:
                time.sleep(args.sleep)

    print(f'\n完成：新拉 {ok}  空 {empty}  失败 {fail}  跳过 {len(codes)-len(todo)}  '
          f'用时 {time.perf_counter()-t0:.0f}s')


if __name__ == '__main__':
    main()
