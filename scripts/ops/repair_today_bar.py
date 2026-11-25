# -*- coding: utf-8 -*-
"""候选㊴：校验/修复「今日行」快照污染（盘后 ≥15:10 用）。

用法：
  python scripts/ops/repair_today_bar.py                 # 修今天被写动过的全部缓存
  python scripts/ops/repair_today_bar.py --dry-run       # 只报告不写盘
  python scripts/ops/repair_today_bar.py --codes 300508 300354
  python scripts/ops/repair_today_bar.py --date 2026-11-25 --all   # 指定日+全量扫（不限 mtime）
"""
from __future__ import annotations

import argparse
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="目标日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--codes", nargs="*", default=None, help="只校验这些代码")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="全量扫（不按 mtime 过滤）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from datetime import datetime
    from core import daily_repair as dr

    target = args.date or datetime.now().strftime("%Y-%m-%d")
    log_fp = os.path.join(_BASE, "t_io", "logs",
                          f"repair_today_bar_{target.replace('-', '')}.jsonl")
    st = dr.fix_today_rows(codes=args.codes, date=target, workers=args.workers,
                           limit=args.limit, log_fp=None if args.dry_run else log_fp,
                           dry_run=args.dry_run,
                           mtime_today_only=not args.all)
    print(f"[repair_today_bar] {target} dry_run={args.dry_run} -> {st}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
