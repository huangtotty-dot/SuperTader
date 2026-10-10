# -*- coding: utf-8 -*-
"""离线生成「历史成交」台账并落盘，供 GUI 直接读取（owner 2026-10-10 指定流程）。

需求背景：券商导出的 xls 解析口径多变、且不适合在 GUI 主线程里反复试；改为
**离线算好 → 写进 t_io/state/trade_history_ledger.json → GUI 只读配置**。

用法：
    # 一个文件一个账户，账户名取文件名
    python scripts/build_trade_ledger.py A.xls B.xls

    # 显式指定账户名（推荐，账户名会直接显示在 GUI 上）
    python scripts/build_trade_ledger.py --account 东方=D:\\path\\a.xls --account 东莞=D:\\path\\b.xls

    # 追加/更新：默认会与已落盘的台账合并去重（券商逐期导出，只导新那几天也不会冲掉历史）
    python scripts/build_trade_ledger.py --append A.xls

    # 只看结果不写盘
    python scripts/build_trade_ledger.py --dry-run A.xls

产物：t_io/state/trade_history_ledger.json —— GUI「图表分析 → 历史成交盈亏」直接读它。
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import trade_history as th  # noqa: E402

LEDGER = os.path.join(_ROOT, "t_io", "state", "trade_history_ledger.json")


def _load_prev():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="离线生成历史成交台账（GUI 直接读）")
    ap.add_argument("files", nargs="*", help="历史成交文件（xls/xlsx/伪xls/pdf）")
    ap.add_argument("--account", action="append", default=[],
                    metavar="名称=路径", help="指定账户名（可多次）；不指定则用文件名")
    ap.add_argument("--append", action="store_true",
                    help="与已落盘台账合并（默认只重导本次给的文件）")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不写盘")
    args = ap.parse_args()

    named = []
    for spec in args.account:
        if "=" not in spec:
            print(f"✗ --account 需写成 名称=路径：{spec}")
            return 2
        name, path = spec.split("=", 1)
        named.append((name.strip(), path.strip()))
    plain = [(None, p) for p in args.files]
    items = named + plain
    if not items:
        ap.print_help()
        return 1

    for name, path in items:
        if not os.path.exists(path):
            print(f"✗ 文件不存在：{path}")
            return 2

    lists, sources, errors = [], [], []
    for name, path in items:
        try:
            rows = th.trades_of(path, diag=errors)
        except Exception:
            continue
        if not rows:
            errors.append(f"{os.path.basename(path)}: 未解析到成交记录")
            continue
        # 账户名：显式指定 > 导出里的资金账号 > 文件名
        if name:
            for t in rows:
                t["account"] = name
        elif not any((t.get("account") or "").strip() for t in rows):
            stem = os.path.splitext(os.path.basename(path))[0]
            for t in rows:
                t["account"] = stem
        lists.append(rows)
        sources.append(name or os.path.basename(path))

    prev = _load_prev() if args.append else None
    prev_trades = (prev or {}).get("trades") if isinstance(prev, dict) else None
    if prev_trades:
        lists.insert(0, prev_trades)

    if not lists:
        print("✗ 没有解析出任何成交：")
        for e in errors:
            print("   ", e)
        return 2

    trades, dup = th.merge_trades(lists)
    led = th.build_ledger(trades, price_fn=th.daily_last_close)
    led["trades"] = trades
    led["sources"] = sources
    led["dup_dropped"] = dup
    led["imported"] = True
    led["data_through"] = (led.get("range") or {}).get("end")
    led["prev_data_through"] = (prev or {}).get("data_through") if isinstance(prev, dict) else None
    led["source"] = " + ".join(sources)
    led["imported_at"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if errors:
        led["parse_warnings"] = (led.get("parse_warnings") or []) + errors

    r = led["range"]
    print(f"区间 {r['start']} ~ {r['end']} · {r['n_trades']} 笔 / {r['n_stocks']} 只"
          + (f" · 去重 {dup} 笔" if dup else ""))
    print(f"{'账户':<14}{'已实现':>13}{'浮动':>13}{'合计':>13}{'收益率':>9}{'做T':>12}")
    for a in led["accounts"]:
        print(f"{a['account'] or '(未标注)':<14}{a['realized']:>13,.2f}{a['unrealized']:>13,.2f}"
              f"{a['total_pnl']:>13,.2f}{a['total_pct']:>8.2f}%{a['t_pnl']:>12,.2f}")
    print(f"{'合计':<14}{led['total_realized']:>13,.2f}{led['total_unrealized']:>13,.2f}"
          f"{led['total_pnl']:>13,.2f}      {'':>7}{led['total_t_pnl']:>12,.2f}")
    if errors:
        print("\n⚠ 部分文件有问题：")
        for e in errors:
            print("   ", e)

    if args.dry_run:
        print("\n(--dry-run：未写盘)")
        return 0
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False)
    os.replace(tmp, LEDGER)
    print(f"\n✓ 已写入 {LEDGER}")
    print("  重开 GUI（或切到「图表分析」页）即可看到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
