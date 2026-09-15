# -*- coding: utf-8 -*-
"""b7_shadow_report.py — B7 尾盘反T影子台账日报 + 影子期验收脚本（施工员_B7-2 · 2026-09-15）

只读 `t_io/logs/b7_shadow_{YYYY-MM-DD}.jsonl` 影子台账（绝不触碰 bridge/orders/任何生产状态），
产出两类东西：

1. 影子日报（默认模式）
   - 当日/区间信号数、b7_skip 按 reason 分布、已结算笔数、胜率、平均费后净收益、
     卖飞率（overnight_gap_pct > 0 占比）、逐笔明细表、待结算（缺接回）清单；
   - stdout 打印 + 写 `t_io/logs/b7_shadow_report_{date}.md`。

2. 影子期验收（--acceptance --pool-size 39 --trading-days N）
   - 调用 execution/auto/overnight_reverse_t.py 的 shadow_acceptance_check
     （sys.path 加 execution/auto 后 import；事件流原样传入，由该模块按 event 键分流统计）；
   - 打印验收表与 alarms；exit code 0 = pass，1 = alarm。

事件 schema（最终版，以此为准）：
  b7_signal          {event, chain_id, sig, pos_qty, virtual_qty, ts}
  b7_skip            {event, code, reason, tail30_pct|null, ts}
  b7_virtual_sell    {event, chain_id, code, qty, sell_px, sell_date, tail30_pct, ts}
  b7_virtual_buyback {event, chain_id, code, qty, buy_px, buy_date, sell_px, sell_date,
                      net_pct(费后净收益%, 4位), overnight_gap_pct|null, win, ts}

配对口径：按 chain_id 配对卖出/接回（跨日配对依赖 chain_id 稳定）。
兼容旧草案格式（无 chain_id）：退化为 (code, qty, sell_px) 复合键配对。

用法：
  python scripts/b7_shadow_report.py                          # 全部日志区间日报
  python scripts/b7_shadow_report.py --date 2026-09-16        # 单日日报
  python scripts/b7_shadow_report.py --from 2026-09-16 --to 2026-09-29
  python scripts/b7_shadow_report.py --log-dir D:/tmp/b7logs --no-write
  python scripts/b7_shadow_report.py --acceptance --pool-size 39 --trading-days 10

exit code：日报模式 0=正常 2=数据/目录错误；验收模式 0=pass 1=alarm 2=数据/目录错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "t_io" / "logs"
FILE_RE = re.compile(r"^b7_shadow_(\d{4}-\d{2}-\d{2})\.jsonl$")

KNOWN_EVENTS = ("b7_signal", "b7_skip", "b7_virtual_sell", "b7_virtual_buyback")


# ── 读取与解析 ──────────────────────────────────────────────────────────────
def _file_date(name: str) -> str | None:
    m = FILE_RE.match(os.path.basename(name))
    return m.group(1) if m else None


def load_events(log_dir: str, date_from: str | None = None,
                date_to: str | None = None) -> tuple[list, dict]:
    """读取 log_dir 下 b7_shadow_*.jsonl，按文件名日期过滤（闭区间）。

    返回 (events, stats)。每个事件附加 "_file_date"（来源文件日期，用于按日统计）。
    坏行（非 JSON / 非 dict / 缺 event 键）跳过并计数。
    stats = {files, lines, bad_lines, bad_files: {file: n_bad}}
    """
    stats = {"files": 0, "lines": 0, "bad_lines": 0, "bad_files": {}}
    events: list[dict] = []
    names = sorted(n for n in os.listdir(log_dir) if _file_date(n))
    for name in names:
        d = _file_date(name)
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        path = os.path.join(log_dir, name)
        stats["files"] += 1
        n_bad = 0
        with open(path, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                stats["lines"] += 1
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    n_bad += 1
                    continue
                if not isinstance(ev, dict) or "event" not in ev:
                    n_bad += 1
                    continue
                ev["_file_date"] = d
                ev["_line"] = ln
                events.append(ev)
        if n_bad:
            stats["bad_lines"] += n_bad
            stats["bad_files"][name] = n_bad
    return events, stats


# ── 配对 ────────────────────────────────────────────────────────────────────
def _chain_key(ev: dict) -> str:
    """配对键：优先 chain_id；旧草案格式无 chain_id 时退化为 code+qty+sell_px 复合键。"""
    cid = ev.get("chain_id")
    if cid:
        return f"cid:{cid}"
    return "legacy:{code}:{qty}:{px}".format(
        code=ev.get("code"), qty=ev.get("qty"), px=ev.get("sell_px"))


def pair_chains(events: list) -> dict:
    """按 chain_id 配对 b7_virtual_sell / b7_virtual_buyback（支持跨日）。

    返回 {signals, skips, sells, buybacks, settled, pending, orphan_buybacks}。
    settled 元素：{chain_id, code, qty, sell_px, sell_date, buy_px, buy_date,
                   net_pct, overnight_gap_pct, win, tail30_pct}
    pending 元素：未等到接回的卖出事件（原样）。
    """
    signals = [e for e in events if e.get("event") == "b7_signal"]
    skips = [e for e in events if e.get("event") == "b7_skip"]
    sells = [e for e in events if e.get("event") == "b7_virtual_sell"]
    buybacks = [e for e in events if e.get("event") == "b7_virtual_buyback"]

    waiting: dict[str, list[dict]] = defaultdict(list)
    for s in sells:
        waiting[_chain_key(s)].append(s)

    settled: list[dict] = []
    orphan: list[dict] = []
    for b in buybacks:
        key = _chain_key(b)
        if waiting.get(key):
            s = waiting[key].pop(0)
            settled.append({
                "chain_id": s.get("chain_id") or b.get("chain_id") or key,
                "code": b.get("code") or s.get("code"),
                "qty": b.get("qty", s.get("qty")),
                "sell_px": s.get("sell_px", b.get("sell_px")),
                "sell_date": s.get("sell_date") or b.get("sell_date")
                             or s.get("_file_date"),
                "buy_px": b.get("buy_px"),
                "buy_date": b.get("buy_date") or b.get("_file_date"),
                "net_pct": b.get("net_pct"),
                "overnight_gap_pct": b.get("overnight_gap_pct"),
                "win": b.get("win"),
                "tail30_pct": s.get("tail30_pct"),
            })
        else:
            orphan.append(b)

    pending = [s for q in waiting.values() for s in q]
    return {"signals": signals, "skips": skips, "sells": sells,
            "buybacks": buybacks, "settled": settled,
            "pending": pending, "orphan_buybacks": orphan}


# ── 统计 ────────────────────────────────────────────────────────────────────
def compute_stats(paired: dict) -> dict:
    """汇总统计：信号数 / skip 分布 / 结算笔数 / 胜率 / 平均费后净收益 / 卖飞率。"""
    settled = paired["settled"]
    n = len(settled)
    wins = sum(1 for r in settled if r.get("win"))
    nets = [float(r["net_pct"]) for r in settled if r.get("net_pct") is not None]
    gaps = [float(r["overnight_gap_pct"]) for r in settled
            if r.get("overnight_gap_pct") is not None]
    skip_by_reason = Counter(e.get("reason", "unknown") for e in paired["skips"])
    per_day = defaultdict(lambda: {"signals": 0, "skips": 0, "sells": 0, "buybacks": 0})
    for e in paired["signals"]:
        per_day[e["_file_date"]]["signals"] += 1
    for e in paired["skips"]:
        per_day[e["_file_date"]]["skips"] += 1
    for e in paired["sells"]:
        per_day[e["_file_date"]]["sells"] += 1
    for e in paired["buybacks"]:
        per_day[e["_file_date"]]["buybacks"] += 1
    return {
        "n_signals": len(paired["signals"]),
        "n_skips": len(paired["skips"]),
        "skip_by_reason": dict(skip_by_reason.most_common()),
        "n_sells": len(paired["sells"]),
        "n_settled": n,
        "n_pending": len(paired["pending"]),
        "n_orphan_buybacks": len(paired["orphan_buybacks"]),
        "win_rate": (wins / n) if n else None,
        "mean_net_pct": (sum(nets) / len(nets)) if nets else None,
        "fly_rate": (sum(1 for g in gaps if g > 0) / len(gaps)) if gaps else None,
        "per_day": {d: dict(v) for d, v in sorted(per_day.items())},
    }


# ── 日报渲染 ────────────────────────────────────────────────────────────────
def _pct(x, digits=2):
    return "—" if x is None else f"{x:.{digits}%}"


def _pct100(x, digits=3):
    """net_pct / gap 已是百分数单位（如 0.6381 表示 0.6381%）。"""
    return "—" if x is None else f"{x:+.{digits}f}%"


def render_report(date_label: str, stats: dict, paired: dict,
                  load_stats: dict) -> str:
    L = []
    L.append(f"# B7 影子日报 · {date_label}")
    L.append("")
    L.append("> 影子期只读台账统计，绝不下单。锚点（E1 离线）：费后 +0.638%/笔、"
             "胜率 64.1%、卖飞率 36%、频率 0.99 次/票/月。")
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 信号数 b7_signal | {stats['n_signals']} |")
    L.append(f"| 拦截数 b7_skip | {stats['n_skips']} |")
    L.append(f"| 虚拟卖出笔数 | {stats['n_sells']} |")
    L.append(f"| 已结算笔数 | {stats['n_settled']} |")
    L.append(f"| 待结算（缺接回） | {stats['n_pending']} |")
    L.append(f"| 胜率（费后净收益>0） | {_pct(stats['win_rate'], 1)} |")
    L.append(f"| 平均费后净收益 | {_pct100(stats['mean_net_pct'])} |")
    L.append(f"| 卖飞率（次日高开占比） | {_pct(stats['fly_rate'], 1)} |")
    if stats["n_orphan_buybacks"]:
        L.append(f"| ⚠️ 无对应卖出的接回事件 | {stats['n_orphan_buybacks']} |")
    if load_stats["bad_lines"]:
        L.append(f"| ⚠️ 坏行跳过 | {load_stats['bad_lines']} |")
    L.append("")

    if stats["skip_by_reason"]:
        L.append("## skip 按 reason 分布")
        L.append("")
        L.append("| reason | 次数 |")
        L.append("|---|---|")
        for reason, cnt in stats["skip_by_reason"].items():
            L.append(f"| {reason} | {cnt} |")
        L.append("")

    if stats["per_day"]:
        L.append("## 按日分布")
        L.append("")
        L.append("| 日期 | 信号 | skip | 虚拟卖出 | 虚拟接回 |")
        L.append("|---|---|---|---|---|")
        for d, v in stats["per_day"].items():
            L.append(f"| {d} | {v['signals']} | {v['skips']} "
                     f"| {v['sells']} | {v['buybacks']} |")
        L.append("")

    if paired["settled"]:
        L.append("## 逐笔明细（已结算）")
        L.append("")
        L.append("| chain_id | 代码 | 数量 | 卖出日 | 卖价 | 接回日 | 接回价 "
                 "| 费后净收益 | 隔夜gap | 胜 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in paired["settled"]:
            gap = r.get("overnight_gap_pct")
            L.append(f"| {r['chain_id']} | {r['code']} | {r['qty']} "
                     f"| {r['sell_date']} | {r['sell_px']} "
                     f"| {r['buy_date']} | {r['buy_px']} "
                     f"| {_pct100(r.get('net_pct'))} "
                     f"| {_pct100(gap) if gap is not None else '—'} "
                     f"| {'✅' if r.get('win') else '❌'} |")
        L.append("")

    if paired["pending"]:
        L.append("## 待结算（卖出后未接回）")
        L.append("")
        L.append("| chain_id | 代码 | 数量 | 卖出日 | 卖价 | tail30_pct |")
        L.append("|---|---|---|---|---|---|")
        for s in paired["pending"]:
            L.append(f"| {s.get('chain_id', _chain_key(s))} | {s.get('code')} "
                     f"| {s.get('qty')} "
                     f"| {s.get('sell_date') or s.get('_file_date')} "
                     f"| {s.get('sell_px')} | {s.get('tail30_pct')} |")
        L.append("")

    if load_stats["bad_lines"]:
        L.append("## 数据质量警告")
        L.append("")
        for name, n in load_stats["bad_files"].items():
            L.append(f"- `{name}`：{n} 行坏行已跳过")
        L.append("")
    return "\n".join(L)


# ── 验收模式 ────────────────────────────────────────────────────────────────
def run_acceptance(events: list, pool_size: int, trading_days: int) -> int:
    """调用 overnight_reverse_t.shadow_acceptance_check；事件流原样传入（按 event 键分流）。

    返回 exit code：0 = pass，1 = alarm。
    """
    sys.path.insert(0, str(REPO_ROOT / "execution" / "auto"))
    import overnight_reverse_t as ort  # noqa: E402

    res = ort.shadow_acceptance_check(events, pool_size=pool_size,
                                      trading_days=trading_days)
    print(f"\n=== B7 影子期验收（pool_size={pool_size}, trading_days={trading_days}）===")
    print(f"{'指标':<28}{'影子期':>12}{'离线锚点':>12}{'相对漂移':>12}")
    anchors = {
        "win_rate_vs_offline": ("胜率", res["win_rate"], ort.OFFLINE_WIN_RATE, _pct),
        "fly_rate_vs_offline": ("卖飞率", res["fly_rate"], ort.OFFLINE_FLY_RATE, _pct),
        "freq_vs_offline": ("频率(次/票/月)", res["freq_per_stock_month"],
                            ort.OFFLINE_FREQ_PER_STOCK_MONTH,
                            lambda x, d=2: "—" if x is None else f"{x:.2f}"),
        "mean_net_vs_offline": ("费后净均", res["mean_net_pct"],
                                ort.OFFLINE_MEAN_NET_PCT,
                                lambda x, d=3: "—" if x is None else f"{x:+.3%}"),
    }
    for key, (label, actual, expect, fmt) in anchors.items():
        drift = res["drift"].get(key)
        print(f"{label:<28}{fmt(actual):>12}{fmt(expect):>12}"
              f"{('—' if drift is None else f'{drift:.1%}'):>12}")
    print(f"\n信号数 {res['n_signals']} / 已结算 {res['n_settled']}"
          f"（预期信号 ≈ {ort.OFFLINE_FREQ_PER_STOCK_MONTH * pool_size * trading_days / 21.0:.0f}）")
    if res["alarms"]:
        print("\n⚠️ ALARMS：")
        for a in res["alarms"]:
            print(f"  - {a}")
    else:
        print("\n✅ 无告警")
    print(f"\n验收结论: {'PASS' if res['pass'] else 'ALARM'}")
    return 0 if res["pass"] else 1


# ── CLI ─────────────────────────────────────────────────────────────────────
def _reconfigure_io():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv: list | None = None) -> int:
    _reconfigure_io()
    ap = argparse.ArgumentParser(
        description="B7 尾盘反T影子台账日报 + 影子期验收（只读台账，绝不下单）")
    ap.add_argument("--date", help="单日日报 YYYY-MM-DD（等价 --from=--to）")
    ap.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD（闭区间）")
    ap.add_argument("--to", dest="date_to", help="截止日期 YYYY-MM-DD（闭区间）")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR),
                    help="影子台账目录（默认 t_io/logs）")
    ap.add_argument("--no-write", action="store_true",
                    help="只打印 stdout，不写 b7_shadow_report_{date}.md")
    ap.add_argument("--acceptance", action="store_true", help="影子期验收模式")
    ap.add_argument("--pool-size", type=int, default=39, help="股票池大小（默认 39）")
    ap.add_argument("--trading-days", type=int, default=10,
                    help="影子期交易日数（默认 10）")
    args = ap.parse_args(argv)

    date_from, date_to = args.date_from, args.date_to
    if args.date:
        date_from = date_to = args.date

    log_dir = args.log_dir
    if not os.path.isdir(log_dir):
        print(f"❌ 影子台账目录不存在：{log_dir}\n"
              f"   影子期尚未启动或路径有误。可用 --log-dir 指定目录。",
              file=sys.stderr)
        return 2

    events, load_stats = load_events(log_dir, date_from, date_to)
    if load_stats["files"] == 0:
        span = f"{date_from or '…'} ~ {date_to or '…'}"
        print(f"❌ 目录 {log_dir} 中未找到日期区间 {span} 内的 "
              f"b7_shadow_YYYY-MM-DD.jsonl 文件。", file=sys.stderr)
        return 2
    for name, n in load_stats["bad_files"].items():
        print(f"⚠️ {name}: {n} 行坏行已跳过", file=sys.stderr)

    if args.acceptance:
        return run_acceptance(events, args.pool_size, args.trading_days)

    paired = pair_chains(events)
    stats = compute_stats(paired)
    dates = [d for d in (date_from, date_to) if d]
    if not dates:
        dates = sorted(stats["per_day"]) or ["（无数据）"]
    date_label = dates[0] if len(set(dates)) == 1 else f"{dates[0]} ~ {dates[-1]}"
    report = render_report(date_label, stats, paired, load_stats)
    print(report)

    if not args.no_write:
        out_date = date_to or date_from or (sorted(stats["per_day"])[-1]
                                            if stats["per_day"] else "nodata")
        out_path = os.path.join(log_dir, f"b7_shadow_report_{out_date}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n📄 日报已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
