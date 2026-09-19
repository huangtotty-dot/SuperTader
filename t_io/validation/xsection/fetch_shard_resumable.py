# -*- coding: utf-8 -*-
"""单片断点续拉器：fetch_panel.py 的子片粒度补充。

fetch_panel.py 只在整片完成后写 shard parquet，单片耗时超过 Bash 290s 上限时
进度全丢。本脚本按 50 只一批落 .partial.parquet，被杀后重跑从断点继续，
完成后改名标准 shard_XXXX.parquet（与 fetch_panel.py 输出格式完全一致）。

用法：python fetch_shard_resumable.py <shard_idx> [--shard 500] [--count 2000] [--adjust prev]
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
    ap.add_argument('shard_idx', type=int)
    ap.add_argument('--shard', type=int, default=500)
    ap.add_argument('--count', type=int, default=2000)
    ap.add_argument('--adjust', choices=('prev', 'none', 'post'), default='prev')
    args = ap.parse_args()
    adj = {'prev': 'ADJUST_PREV', 'none': 'ADJUST_NONE', 'post': 'ADJUST_POST'}[args.adjust]

    from utils.gm_token import load_token
    import gm.api as gm
    gm.set_token(load_token())
    adjust_mode = getattr(gm, adj)

    uni = pd.read_parquet(os.path.join(PANEL, 'universe.parquet'))
    syms = list(uni['symbol'])
    si = args.shard_idx
    fp = os.path.join(SHARDS, f'shard_{si:04d}.parquet')
    pp = os.path.join(SHARDS, f'shard_{si:04d}.partial.parquet')
    if os.path.exists(fp):
        print(f'[done] shard_{si:04d} 已存在，跳过', flush=True)
        return
    batch = syms[si * args.shard:(si + 1) * args.shard]
    if not batch:
        print('[err] 分片索引超出 universe', flush=True)
        sys.exit(1)

    frames, fails = [], 0
    start = 0
    if os.path.exists(pp):
        old = pd.read_parquet(pp)
        frames.append(old)
        done_syms = set(old['symbol'].unique())
        start = sum(1 for s in batch if s in done_syms)
        print(f'[resume] 断点 {start}/{len(batch)}', flush=True)

    t0 = time.time()
    for i in range(start, len(batch)):
        sym = batch[i]
        try:
            df = gm.history_n(symbol=sym, frequency='1d', count=args.count,
                              fields=FIELDS, adjust=adjust_mode, df=True)
            if df is not None and len(df):
                frames.append(df)
        except Exception:
            fails += 1
        if (i + 1) % 50 == 0:
            pd.concat(frames, ignore_index=True).to_parquet(pp, index=False)
            frames = [pd.read_parquet(pp)]
            print(f'  [{i + 1}/{len(batch)}] 失败={fails} '
                  f'用时={time.time() - t0:.0f}s', flush=True)

    all_df = pd.concat(frames, ignore_index=True)
    all_df.to_parquet(fp, index=False)
    if os.path.exists(pp):
        os.remove(pp)
    print(f'[done] shard_{si:04d} {len(batch)} 只 失败={fails} '
          f'行数={len(all_df)} 用时={time.time() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
