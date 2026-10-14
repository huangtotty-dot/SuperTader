# -*- coding: utf-8 -*-
"""GUI 卡死回归看板（只读）——回答「869063c 那个修复到底有没有生效」。

背景：2026-10-10 21:18:46 提交 869063c（UI 线程 Thread.start()×线程爆炸的死亡螺旋）
落地，但当天最后一次卡死记录是 21:04:13（早于修复 14 分钟）⇒ **修复未经任何交易日验证**。
本脚本把 t_io/logs/gui_freeze.log 解析成「按日 / 按时段 / 按事件段」的看板，
用于：周一收盘后（或任何一天）回答「修复后还有没有卡、卡多久」。

⚠️ 关键口径：guI_freeze 看门狗在**冻结持续期间每 ~30s 重复记一条**，所以
   「日志行数」≠「独立卡死次数」。本脚本按事件间隔归并成**事件段(episode)**——
   同一段内相邻记录间隔 ≤ EPISODE_GAP_SEC 视为同一次连续冻结。

用法：
    python scripts/experiment/gui_freeze_regression_20261011.py
    python scripts/experiment/gui_freeze_regression_20261011.py --since 2026-10-10T21:18:46
    python scripts/experiment/gui_freeze_regression_20261011.py --log <路径>

只读：不写任何文件、不碰网络、不 import 业务模块。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, time as dtime

sys.stdout.reconfigure(encoding="utf-8")

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOG = os.path.join(_BASE, "t_io", "logs", "gui_freeze.log")

# 869063c 的提交时间（修复基线）——默认统计「这之后还有没有卡」
FIX_COMMIT_TS = "2026-10-10T21:18:46"

EPISODE_GAP_SEC = 45          # 相邻记录间隔 ≤ 此值 → 归为同一次连续冻结
TRADING_OPEN = dtime(9, 30)
TRADING_CLOSE = dtime(15, 0)

# ── header 解析（**两种顺序都收**）──────────────────────────────────────────
# 历史形状：`===== 疑似界面卡死 <日期> <时间>（主线程心跳停 Ns）线程栈 =====`
# 2026-10-14 一度被改成「心跳停在前、时间在后」（加哨兵时重排了 header），
# 而当时的正则只认第一种 ⇒ **当天下午起所有卡死段对本看板静默不可见**（仍报 39 段、看着正常）。
# ⇒ 两条正则并列，任何一边命中即可；写入侧（t_gui._dump）已恢复历史形状。
_HDR_A = re.compile(
    r"疑似界面卡死\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s*"
    r"[（(]\s*主线程心跳停\s*(\d+)\s*s"
)
_HDR_B = re.compile(
    r"疑似界面卡死\s*[（(]\s*主线程心跳停\s*(\d+)\s*s\s*[）)]\s*"
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})"
)


def _match_hdr(line: str):
    """→ (日期, 时间, 心跳停秒数) 或 None。两种 header 顺序都接受。"""
    m = _HDR_A.search(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _HDR_B.search(line)
    if m:
        return m.group(2), m.group(3), m.group(1)     # B 形：秒数在前，日期时间在后
    return None


def parse_events(path: str) -> list[tuple[datetime, int]]:
    """→ [(时间戳, 心跳停秒数)]，按时间升序。"""
    events: list[tuple[datetime, int]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "疑似界面卡死" not in line:
                    continue
                g = _match_hdr(line)
                if not g:
                    continue
                try:
                    ts = datetime.strptime(f"{g[0]} {g[1]}", "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                events.append((ts, int(g[2])))
    except FileNotFoundError:
        return []
    events.sort(key=lambda x: x[0])
    return events


def group_episodes(events: list[tuple[datetime, int]]) -> list[dict]:
    """把重复记录归并成连续冻结事件段。"""
    eps: list[dict] = []
    for ts, stall in events:
        if eps and (ts - eps[-1]["last_ts"]).total_seconds() <= EPISODE_GAP_SEC:
            e = eps[-1]
            e["last_ts"] = ts
            e["n_records"] += 1
            e["max_stall"] = max(e["max_stall"], stall)
        else:
            eps.append({"start": ts, "last_ts": ts, "n_records": 1, "max_stall": stall})
    for e in eps:
        # 真实冻结时长 ≈ 段内最大"心跳停"值（它本身是累计量）
        e["duration_sec"] = e["max_stall"]
    return eps


def in_trading_hours(ts: datetime) -> bool:
    return ts.weekday() < 5 and TRADING_OPEN <= ts.time() <= TRADING_CLOSE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--since", default=FIX_COMMIT_TS,
                    help="只统计此时间之后的事件（默认=修复提交时间）")
    args = ap.parse_args()

    print(f"日志: {args.log}")
    if not os.path.exists(args.log):
        print("⛔ 日志不存在——GUI 从未触发过卡死看门狗，或路径不对。")
        return 0

    events = parse_events(args.log)
    if not events:
        print("⛔ 未解析到任何「疑似界面卡死」记录。")
        return 0

    eps = group_episodes(events)
    since = datetime.strptime(args.since, "%Y-%m-%dT%H:%M:%S")

    print(f"总记录行数: {len(events)}   归并后独立冻结事件段: {len(eps)}")
    print(f"时间跨度: {events[0][0]:%Y-%m-%d %H:%M} ~ {events[-1][0]:%Y-%m-%d %H:%M}")
    print(f"修复基线(--since): {since:%Y-%m-%d %H:%M:%S}")
    print()

    # ── 按日 ──
    by_date: dict[str, list[tuple[datetime, int]]] = {}
    for ts, s in events:
        by_date.setdefault(ts.strftime("%Y-%m-%d"), []).append((ts, s))
    print("=== 按日 ===")
    print("%-12s %-6s %-8s %-10s %-10s" % ("日期", "记录", "事件段", "交易日?", "段内最久(s)"))
    for d in sorted(by_date):
        ev = by_date[d]
        deps = [e for e in eps if e["start"].strftime("%Y-%m-%d") == d]
        is_td = datetime.strptime(d, "%Y-%m-%d").weekday() < 5
        mx = max((s for _, s in ev), default=0)
        print("%-12s %-6d %-8d %-10s %-10d"
              % (d, len(ev), len(deps), "是" if is_td else "否", mx))
    print()

    # ── 交易时段内（仅工作日 09:30-15:00）──
    td_events = [(ts, s) for ts, s in events if in_trading_hours(ts)]
    td_eps = [e for e in eps if in_trading_hours(e["start"])]
    print("=== 交易时段内（工作日 09:30-15:00）===")
    print(f"记录 {len(td_events)} 条 / 独立事件段 {len(td_eps)} 个")
    if td_eps:
        worst = max(td_eps, key=lambda e: e["duration_sec"])
        print(f"最长一次: {worst['start']:%Y-%m-%d %H:%M:%S} 持续约 {worst['duration_sec']}s "
              f"(段内 {worst['n_records']} 条记录)")
        durs = sorted(e["duration_sec"] for e in td_eps)
        print(f"时长分布(s): 最小 {durs[0]} / 中位 {durs[len(durs)//2]} / 最大 {durs[-1]}")
    print()

    # ── 修复基线之后 ──
    post_events = [(ts, s) for ts, s in events if ts > since]
    post_eps = [e for e in eps if e["start"] > since]
    print("=== 修复基线之后 ===")
    print(f"记录 {len(post_events)} 条 / 独立事件段 {len(post_eps)} 个")
    for e in post_eps:
        tag = "【交易时段】" if in_trading_hours(e["start"]) else ""
        print(f"  {e['start']:%Y-%m-%d %H:%M:%S}  持续约 {e['duration_sec']}s "
              f"(记录 {e['n_records']} 条) {tag}")
    print()

    # ── 判词 ──
    log_mtime = datetime.fromtimestamp(os.path.getmtime(args.log))
    print("=" * 62)
    if not post_events:
        print(f"✅ 修复基线之后**零**卡死记录。")
        print(f"   ⚠️ 但请注意日志最后修改时间 {log_mtime:%Y-%m-%d %H:%M}——")
        print(f"   若它仍早于你最近一次跑 GUI 的时间，说明是「没跑」而非「没卡」，不可当结论。")
    else:
        post_td = [e for e in post_eps if in_trading_hours(e["start"])]
        print(f"❌ 修复基线之后仍有 {len(post_eps)} 个独立卡死事件段"
              f"（其中交易时段 {len(post_td)} 个）。")
        print("   → 修复未生效或另有新根因，需重跑线程栈解析定位。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
