# -*- coding: utf-8 -*-
"""修复 e5 涨跌停缓存被「14:30 盘中数」钉死的历史行（2026-11-22）。

背景：`_ir_fetch_limit_counts_ths` 的缓存是 append-only，14:30 尾盘快照先把**盘中数**
写进去（例：11-20 涨停26/跌停25），15:30 eod 与之后所有读取都直接命中缓存 ⇒ 市场情绪面板
的日频涨跌停数与同花顺收盘值对不上（收盘真值 28/58）。

代码侧已修：当天且 ≥15:00 时无视缓存重取。本脚本把**已有历史行**按同花顺收盘值重刷一遍
（只改数值不同的行；接口返回 0/0 或失败的不动）。

用法：
    python scripts/ops/repair_e5_close_counts.py            # 全量
    python scripts/ops/repair_e5_close_counts.py --days 60  # 只重刷最近 60 个有记录的日期
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

import analysis.index_regime as ir  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只重刷最近 N 个日期（0=全部）")
    ap.add_argument("--sleep", type=float, default=0.35, help="每次请求后的间隔秒数（防反爬）")
    args = ap.parse_args()

    fp = ir._ir_e5_cache_path({})
    cache = json.loads(Path(fp).read_text(encoding="utf-8"))
    dates = sorted(cache)
    if args.days:
        dates = dates[-args.days:]

    bak = BASE / "backup" / f"e5_ths_cache_before_repair_{datetime.now():%Y%m%d_%H%M%S}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fp, bak)

    changed, same, skipped = [], 0, 0
    failed = []
    for d in dates:
        old = cache.get(d) or {}
        try:
            rec = ir._ir_fetch_limit_counts_ths(d, timeout=10.0, retries=1, cache_path=None)
        except Exception:
            rec = None
        if not rec:
            failed.append(d)
            time.sleep(args.sleep)
            continue
        zt, dt = rec.get("zt_count"), rec.get("dt_count")
        if zt is None or dt is None or (zt == 0 and dt == 0):
            skipped += 1
            time.sleep(args.sleep)
            continue
        if (old.get("zt_count"), old.get("dt_count")) == (zt, dt):
            same += 1
        else:
            changed.append((d, (old.get("zt_count"), old.get("dt_count")), (zt, dt)))
            cache[d] = rec
        time.sleep(args.sleep)

    Path(fp).write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"重刷 {len(dates)} 天：变更 {len(changed)} / 一致 {same} / 跳过(0或空) {skipped} / 失败 {len(failed)}")
    if failed:
        print("失败日期:", " ".join(failed))
    for d, a, b in changed:
        print(f"  {d}: 涨停/跌停 {a} → {b}")
    print(f"[backup] {bak}")


if __name__ == "__main__":
    main()
