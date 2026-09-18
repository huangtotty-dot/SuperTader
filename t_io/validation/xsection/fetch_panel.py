# -*- coding: utf-8 -*-
"""截面选股 · 数据面板拉取（掘金 gm）。

## 为什么走掘金

tushare 本 token **只有 `stk_mins` 权限**（`daily`/`stock_basic`/`trade_cal` 全无）；
akshare 在本机被拦；腾讯日线只给 640 根。掘金是唯一同时满足三条的源：
  · 全市场枚举：`get_symbols(sec_type1=1010, skip_suspended=False, skip_st=False)` → 5,674 只
  · **含已退市股**（374 只 delisted_date 已过期）⇒ **幸存者偏差可处理**
  · 日线 2000 根 / 8+ 年，0.30s/只
且与生产系统同源。

## 输出

  panel/universe.parquet   标的元数据（symbol/sec_name/exchange/listed_date/delisted_date）
  panel/shards/*.parquet   按批分片的长表（symbol,eob,open,high,low,close,volume,amount）
分片的用途：**崩了不用从头拉**——已存在的分片直接跳过。

用法：python fetch_panel.py [--limit N] [--shard 500] [--count 2000]
"""
import argparse
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
for _p in (ROOT, os.path.join(ROOT, 'execution', 'auto'),
           os.path.join(ROOT, 'execution', 'auto', '_gm')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PANEL = os.path.join(HERE, 'panel')
SHARDS = os.path.join(PANEL, 'shards')
FIELDS = 'symbol,eob,open,high,low,close,volume,amount'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='只拉前 N 只（调试用）')
    ap.add_argument('--shard', type=int, default=500)
    ap.add_argument('--count', type=int, default=2000)
    ap.add_argument('--sleep', type=float, default=0.05)
    args = ap.parse_args()

    os.makedirs(SHARDS, exist_ok=True)
    from utils.gm_token import load_token
    from gm.api import set_token, get_symbols, history_n
    set_token(load_token())

    uni = get_symbols(sec_type1=1010, skip_suspended=False, skip_st=False, df=True)
    keep = [c for c in ('symbol', 'sec_name', 'exchange', 'listed_date', 'delisted_date')
            if c in uni.columns]
    uni = uni[keep].reset_index(drop=True)
    uni.to_parquet(os.path.join(PANEL, 'universe.parquet'), index=False)
    syms = list(uni['symbol'])
    if args.limit:
        syms = syms[:args.limit]
    print(f'[fetch] 全市场 {len(uni)} 只，本轮拉 {len(syms)} 只，每只 {args.count} 根', flush=True)

    n_shard = (len(syms) + args.shard - 1) // args.shard
    t0 = time.time()
    done = 0
    for si in range(n_shard):
        fp = os.path.join(SHARDS, f'shard_{si:04d}.parquet')
        if os.path.exists(fp):
            done += min(args.shard, len(syms) - si * args.shard)
            print(f'  [skip] {os.path.basename(fp)} 已存在（{done}/{len(syms)}）', flush=True)
            continue
        batch = syms[si * args.shard:(si + 1) * args.shard]
        frames, fails = [], 0
        for sym in batch:
            try:
                df = history_n(symbol=sym, frequency='1d', count=args.count,
                               fields=FIELDS, df=True)
                if df is not None and len(df):
                    frames.append(df)
            except Exception:
                fails += 1
            time.sleep(args.sleep)
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(fp, index=False)
        done += len(batch)
        el = time.time() - t0
        rate = done / max(el, 1e-9)
        print(f'  [{si + 1}/{n_shard}] {done}/{len(syms)} 失败={fails} '
              f'用时={el:.0f}s 预计剩 {(len(syms) - done) / max(rate, 1e-9) / 60:.0f}min', flush=True)
    print(f'[fetch] 完成 {done} 只，用时 {(time.time() - t0) / 60:.1f}min → {SHARDS}', flush=True)


if __name__ == '__main__':
    main()
