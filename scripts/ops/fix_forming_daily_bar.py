# -*- coding: utf-8 -*-
"""修复「过去某天还挂着 forming」的日线缓存（2026-11-03 owner：「11月2日的K线明显不对」）。

事故：11-02 早盘 09:31 写下的**快照根**（close=当时价、量只有零头）带 `forming:["2026-11-02"]`
落盘；`is_unconfirmed_today_bar` 只拦"末行==今天"，到 11-03 不再命中 ⇒ 半成品被当收盘。
实测 2374 个缓存文件带 forming、2102 只收盘价与实际不符（001219：缓存 19.98，实际涨停 21.93）。

本脚本 = `core.daily_repair` 的薄 CLI：逐只重新拉腾讯前复权日线，覆盖该日并清 forming。
`main.py` 15:40 通达信校准之后也会调用同一套逻辑兜底。

用法：
  python scripts/ops/fix_forming_daily_bar.py --dry-run
  python scripts/ops/fix_forming_daily_bar.py --workers 4
  python scripts/ops/fix_forming_daily_bar.py --codes 001219,002086
  python scripts/ops/fix_forming_daily_bar.py --date 2026-11-02
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from core import daily_repair as _dr  # noqa: E402

# 兼容旧测试的入口（指向同一实现）
CACHE = _dr.CACHE
_targets = _dr.find_forming_tails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="", help="逗号分隔的 6 位代码（默认全量）")
    ap.add_argument("--date", default="", help="只修这个目标日（YYYY-MM-DD）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    targets = _dr.find_forming_tails(date=args.date or None)
    if codes:
        cs = {c.zfill(6) for c in codes}
        targets = [t for t in targets if t[0] in cs]
    if args.limit:
        targets = targets[:args.limit]
    print(f"[scan] 需要修复 {len(targets)} 只（目标日 {args.date or '任意过去日'}）")
    if args.dry_run:
        for code, _fp, target, old in targets[:50]:
            print(f"  - {code} {target} 现收 {old}")
        if len(targets) > 50:
            print(f"  … 其余 {len(targets) - 50} 只")
        return 0
    if not targets:
        return 0

    log = BASE / "t_io" / "logs" / f"fix_forming_{datetime.now().strftime('%Y%m%d')}.jsonl"
    stats = _dr.fix_forming_tails(codes=codes or None, date=args.date or None,
                                  workers=args.workers, limit=args.limit, log_fp=str(log))
    print(f"[done] {stats}；审计日志 {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
