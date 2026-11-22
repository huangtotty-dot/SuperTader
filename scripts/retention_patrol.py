# -*- coding: utf-8 -*-
"""t_io 保留策略巡检（2026-11-22，诊断 doc/solutions/2026-11-22_UI实时性深度诊断与架构优化.md P2-2）
================================================================================
背景：t_io 无界增长 —— cache/tushare_mins 4.0G/10,337 文件、minute_snapshots 135M/4,146 文件、
bridge/heartbeat_*.jsonl 全留存（1.5-2.8MB/交易日）、cache/breakout_*.json 自 08-07 全留存。
1 万+ 目录项拖慢一切 glob/杀软扫描，反噬高频小文件写（P2-1 同诊断）。

**默认 dry-run**：只打印将删什么、释放多少；`--apply` 才真删。

删除白名单（按文件 mtime 滚动，只删这些 glob 命中的文件，别的一概不碰）：
  · t_io/cache/tushare_mins/**/*      mtime > 180 天
  · t_io/minute_snapshots/**/*.json   mtime > 180 天
  · t_io/bridge/heartbeat_*.jsonl     mtime >  90 天
  · t_io/cache/breakout_*.json        mtime > 180 天

只报告不删（研究产物，处置权留给 owner）：
  · t_io/validation/**（实测 9.4G）
  · t_io/traces/**（实测 181M）

用法：
  python scripts/retention_patrol.py            # dry-run（默认）
  python scripts/retention_patrol.py --apply    # 真删（逐类先打印确认行再动手）
  python scripts/retention_patrol.py --base <目录>   # 指向别的 t_io 根（测试用）

接入建议（本次**不实际改调度**，仅记录）：
  参照 `core/freshness_patrol.py` 的现有接法 —— 它由 main.py 主循环 → `core/scan.py`
  → `core/eod_review._maybe_run_freshness_patrol(now)` 驱动：交易日 15:40 后时间闸 +
  进程内当日占位 + daemon 线程 + `t_io/state/freshness_patrol_{date}.json` 幂等标记 +
  飞书卡片。本脚本可按同款接入：在 `core/eod_review.py` 加一个 `_maybe_run_retention_patrol`
  （建议**周六** 15:50 后每周一次、后台线程跑 dry-run 并把报告推飞书/落 t_io/state），
  `--apply` 仍留给人工或 Windows 任务计划程序每周执行（删除不可逆，不挂全自动链）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

# 白名单删除规则：只有命中 glob 且 mtime 超龄的文件才进删除候选。
DELETE_RULES = [
    {"name": "cache/tushare_mins", "glob": "cache/tushare_mins/**/*",        "keep_days": 180},
    {"name": "minute_snapshots",   "glob": "minute_snapshots/**/*.json",     "keep_days": 180},
    {"name": "bridge/heartbeat",   "glob": "bridge/heartbeat_*.jsonl",       "keep_days": 90},
    {"name": "cache/breakout",     "glob": "cache/breakout_*.json",          "keep_days": 180},
]
# 研究产物：只报体积，永不删。
REPORT_ONLY = [
    {"name": "validation", "glob": "validation/**/*"},
    {"name": "traces",     "glob": "traces/**/*"},
]

_SEC_PER_DAY = 86400


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def _fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _iter_files(base: Path, pattern: str):
    """白名单 glob 命中文件（只文件不目录）；解析后必须在 base 内，防越界。"""
    try:
        root = base.resolve()
    except OSError:
        return
    for p in sorted(base.glob(pattern)):
        try:
            if not p.is_file():
                continue
            rp = p.resolve()
            if rp != root and root not in rp.parents:
                continue   # 符号链接/越界一律不碰
            yield p
        except OSError:
            continue


def _class_stats(files, now: float, keep_days: float = None):
    """→ dict(命中数/总体积/最老/最新 [+ 超龄子集])。"""
    total_n, total_b = 0, 0
    oldest, newest = None, None
    expired = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        total_n += 1
        total_b += st.st_size
        oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
        if keep_days is not None and st.st_mtime < now - keep_days * _SEC_PER_DAY:
            expired.append((p, st.st_size, st.st_mtime))
    out = {
        "count": total_n,
        "bytes": total_b,
        "oldest": oldest,
        "newest": newest,
    }
    if keep_days is not None:
        out["expired"] = {
            "files": expired,
            "count": len(expired),
            "bytes": sum(b for _, b, _ in expired),
            "oldest": min((m for _, _, m in expired), default=None),
            "newest": max((m for _, _, m in expired), default=None),
        }
    return out


def survey(base, now: float = None) -> dict:
    """扫描 base（t_io 根）→ 报告 dict。**零删除**，纯 stat。"""
    base = Path(base)
    now = now or time.time()
    rep = {"base": str(base), "ts": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
           "rules": [], "reports": []}
    for rule in DELETE_RULES:
        st = _class_stats(_iter_files(base, rule["glob"]), now, rule["keep_days"])
        rep["rules"].append({**rule, **st})
    for rule in REPORT_ONLY:
        st = _class_stats(_iter_files(base, rule["glob"]), now, None)
        rep["reports"].append({**rule, **st})
    return rep


def _print_report(rep: dict, apply_mode: bool) -> None:
    mode = "APPLY（真删）" if apply_mode else "DRY-RUN（只报告，加 --apply 才真删）"
    print(f"=== t_io 保留策略巡检 · {rep['ts']} · {mode} ===")
    print(f"根目录: {rep['base']}")
    tot_n, tot_b = 0, 0
    for r in rep["rules"]:
        ex = r["expired"]
        tot_n += ex["count"]
        tot_b += ex["bytes"]
        age = f">{r['keep_days']}d"
        rng = f"{_fmt_day(r['oldest'])} ~ {_fmt_day(r['newest'])}" if r["oldest"] else "-"
        print(f"· {r['name']}: 命中 {r['count']} 个 / {_fmt_bytes(r['bytes'])}（{rng}）")
        if ex["count"]:
            erng = f"{_fmt_day(ex['oldest'])} ~ {_fmt_day(ex['newest'])}"
            print(f"    超龄({age}) 将删 {ex['count']} 个 / 释放 {_fmt_bytes(ex['bytes'])}（{erng}）")
        else:
            print(f"    超龄({age}) 0 个，无需删除")
    for r in rep["reports"]:
        rng = f"{_fmt_day(r['oldest'])} ~ {_fmt_day(r['newest'])}" if r["oldest"] else "-"
        print(f"· {r['name']}: {r['count']} 个 / {_fmt_bytes(r['bytes'])}（{rng}）"
              f"  [只报告不删：研究产物，处置权留 owner]")
    print(f"合计可释放: {tot_n} 个文件 / {_fmt_bytes(tot_b)}")


def apply_deletions(rep: dict) -> dict:
    """按报告逐类二次确认打印后真删。返回 {deleted, bytes, errors}。"""
    out = {"deleted": 0, "bytes": 0, "errors": 0}
    for r in rep["rules"]:
        ex = r["expired"]
        if not ex["count"]:
            continue
        print(f"[CONFIRM] {r['name']}: 即将删除 {ex['count']} 个文件，"
              f"释放 {_fmt_bytes(ex['bytes'])}（规则 mtime>{r['keep_days']}d，"
              f"最老 {_fmt_day(ex['oldest'])}）")
        dn, db = 0, 0
        for p, size, _mt in ex["files"]:
            try:
                os.remove(p)
                dn += 1
                db += size
            except OSError as e:
                out["errors"] += 1
                print(f"    [WARN] 删除失败 {p}: {type(e).__name__}: {e}")
        out["deleted"] += dn
        out["bytes"] += db
        print(f"    → 已删 {dn}/{ex['count']}，释放 {_fmt_bytes(db)}")
    print(f"=== 删除完成：共 {out['deleted']} 个 / {_fmt_bytes(out['bytes'])}，"
          f"失败 {out['errors']} ===")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="t_io 保留策略巡检（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    ap.add_argument("--base", default=str(BASE / "t_io"),
                    help="t_io 根目录（默认本仓 t_io；测试可指向假目录树）")
    args = ap.parse_args(argv)
    base = Path(args.base)
    if not base.is_dir():
        print(f"[ERROR] 根目录不存在: {base}")
        return 2
    rep = survey(base)
    _print_report(rep, args.apply)
    if args.apply:
        apply_deletions(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
