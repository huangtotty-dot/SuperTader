# -*- coding: utf-8 -*-
"""全池日收益矩阵 → `t_io/cache/linkage_matrix.npz`（给「个股联动」功能用）。

## 为什么要有这个档

「搜一只股票 → 找它的联动股」本质是**全池收益相关性**：目标 vs 5300 只股票各算一次相关。
直接用 `daily_close_series` 逐只取要**冷解析 5300 个 80KB JSON（实测 32.5s）**，
放进 GUI 的请求路径会卡死。改成一个扁平的 float32 矩阵后：
  加载 ~10MB npz ≈ 30ms，60 日相关一次向量化 ≈ 1ms。

## 口径

- 股票池：6 位 A 股正股（排除 15x/5x 基金、指数）。
- 日期：最近 `--days` 个交易日；**覆盖率 <80% 的日期丢掉**（缓存早期不全 / 停牌潮）。
- **排除当天**：盘中各票缓存的"今天那根"有的一半有一半没有（半成品），
  混进来会把相关性算歪；联动看的是"昨天为止的节奏"，晚一天完全够用。
- 收益 = close/prev_close-1（前复权序列，比例不受复权影响）；|收益|>50% 视为伪影置 NaN。

## 用法

    python scripts/ops/build_linkage_matrix.py            # 默认 250 个交易日
    python scripts/ops/build_linkage_matrix.py --days 400

产出是**可重建的派生物**（不落 git），后端发现它缺失/过期会自动后台重算一次。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
DAILY = BASE / "t_io" / "cache" / "daily_kline"
OUT = BASE / "t_io" / "cache" / "linkage_matrix.npz"


def is_stock(c: str) -> bool:
    c = str(c)
    if len(c) != 6 or not c.isdigit() or c[0] in "15":
        return False
    if c[:2] in ("51", "56", "58", "50", "52", "11", "12", "13"):
        return False
    return c[:2] in ("60", "00", "30", "68") or c[:3] == "920"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--min-cover", type=float, default=0.70)   # 近期缓存不一致时会掉到 ~80%，别卡太死
    args = ap.parse_args()

    t0 = time.time()
    px = {}
    for p in DAILY.glob("*.json"):
        if not is_stock(p.stem):
            continue
        try:
            rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
        except Exception:
            continue
        m = {}
        for r in rows:
            try:
                c = float(r.get("close") or 0)
            except (TypeError, ValueError):
                continue
            if c > 0:
                m[str(r.get("date"))[:10]] = c
        if len(m) >= 60:
            px[p.stem] = m
    print(f"读 {len(px)} 只  {time.time()-t0:.1f}s", flush=True)

    all_dates = sorted({d for m in px.values() for d in m})
    # 覆盖率闸：某天只有少数票有价 ⇒ 那天不可比
    cover = {}
    for m in px.values():
        for d in m:
            cover[d] = cover.get(d, 0) + 1
    today = datetime.now().strftime("%Y-%m-%d")
    keep = [d for d in all_dates
            if cover.get(d, 0) >= args.min_cover * len(px) and d < today]
    dates = keep[-args.days:]
    if len(dates) < 30:
        print("有效交易日不足 30 天，放弃")
        return 1
    di = {d: i for i, d in enumerate(dates)}

    codes = sorted(px)
    M = np.full((len(dates), len(codes)), np.nan, dtype=np.float32)
    for j, c in enumerate(codes):
        for d, v in px[c].items():
            i = di.get(d)
            if i is not None:
                M[i, j] = v
    R = np.full_like(M, np.nan)
    R[1:] = M[1:] / M[:-1] - 1.0
    R[np.abs(R) > 0.5] = np.nan

    os.makedirs(OUT.parent, exist_ok=True)
    # 原子写：GUI 随时可能在读，半个 npz 会解码失败（读到就判"档坏"、退回"生成中"）
    tmp = OUT.with_name(OUT.stem + ".tmp.npz")
    np.savez_compressed(tmp, dates=np.array(dates[1:]), codes=np.array(codes), R=R[1:])
    os.replace(tmp, OUT)
    print(f"done: {R.shape[0]} 日 × {len(codes)} 只 → {OUT} "
          f"({OUT.stat().st_size/1e6:.1f}MB, {time.time()-t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
