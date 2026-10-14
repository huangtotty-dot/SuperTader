# -*- coding: utf-8 -*-
r"""osc_fill_stats.py — OSC（超跌跳空确认）买侧成交率/滑点/资金冻结统计（纯统计口径，2026-10-14）。

背景：2026-10-14 上线首日，4 只确认候选中 gap 最大的两只（600097/000823）封死涨停，
市价单排全天未成交、19.8 万冻结到 15:00。owner 要求先量化「gap → 成交率」曲线，
为「未成交顺移下一名候选」等替补机制的阈值设计提供数据依据。

数据源（只读，不写任何生产状态）：
  t_io/state/weak_strong_book/fills_YYYY-MM-DD.json   — 执行器留痕（参考价/计划量/状态）
  t_io/bridge/events_YYYYMMDD.jsonl                   — 事件桥（fill/reject 真实成交回报）

口径：
  · 成交 = bridge 里存在该 code 当日 BUY fill 事件（真源）；fills.json 的 status 仅参考
    （10:00 只轮询 3×2s，之后成交/过期靠事件桥）。
  · 滑点 = fill_vwap / 参考价 - 1（参考价 = 10:00 棒收盘，fills.json 的 price 字段）。
  · 冻结名义 = 下单未成交的 qty_placed × 参考价（到 15:00 status=12 过期才释放）。
  · gap 分桶：[0,3%) [3%,5%) [5%,8%) [8%,10%) [10%,∞)（10cm 板 gap≥9.9% ≈ 必然涨停位）。

用法：
  python scripts/osc_fill_stats.py            # 扫描全部 fills_*.json
  python scripts/osc_fill_stats.py 2026-10-14 # 只看单日

产物：t_io/metrics/osc_fill_stats_latest.json（幂等覆盖）+ stdout 汇总表。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

_ROOT = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
BOOK_DIR = os.path.join(_ROOT, "t_io", "state", "weak_strong_book")
BRIDGE_DIR = os.path.join(_ROOT, "t_io", "bridge")
OUT_DIR = os.path.join(_ROOT, "t_io", "metrics")

GAP_BUCKETS = [(0.0, 0.03, "0~3%"), (0.03, 0.05, "3~5%"), (0.05, 0.08, "5~8%"),
               (0.08, 0.10, "8~10%"), (0.10, 9.9, ">=10%")]


def _bucket(gap):
    g = abs(float(gap or 0))
    for lo, hi, name in GAP_BUCKETS:
        if lo <= g < hi:
            return name
    return ">=10%"


def _load_bridge_events(date: str) -> dict:
    """date='2026-10-14' → {code: {'filled_qty','fill_vwap','rejected','reject_time','fill_time'}}"""
    fp = os.path.join(BRIDGE_DIR, "events_%s.jsonl" % date.replace("-", ""))
    out = {}
    if not os.path.exists(fp):
        return out
    with open(fp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("side") != "BUY":
                continue
            code = str(ev.get("code") or "")
            if not code:
                continue
            rec = out.setdefault(code, {"filled_qty": 0, "fill_vwap": None,
                                        "rejected": False, "reject_time": None,
                                        "fill_time": None})
            if ev.get("event") == "fill":
                rec["filled_qty"] += int(ev.get("qty") or 0)
                rec["fill_vwap"] = ev.get("fill_vwap") or ev.get("price")
                rec["fill_time"] = rec["fill_time"] or ev.get("time")
            elif ev.get("event") == "reject":
                rec["rejected"] = True
                rec["reject_time"] = ev.get("time")
    return out


def collect(only_date: str | None = None) -> list:
    days = []
    pattern = os.path.join(BOOK_DIR, "fills_*.json")
    for fp in sorted(glob.glob(pattern)):
        m = re.search(r"fills_(\d{4}-\d{2}-\d{2})\.json$", fp)
        if not m:
            continue
        date = m.group(1)
        if only_date and date != only_date:
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                fills = json.load(f)
        except Exception:
            continue
        if fills.get("strategy") != "oversold_gap_confirm":
            continue
        events = _load_bridge_events(date)
        rows = []
        for b in fills.get("buys") or []:
            if not b.get("qty_placed"):
                continue  # skipped/deferred 不算下单
            code = b["code"]
            ev = events.get(code) or {}
            ref_px = float(b.get("price") or 0)
            filled_qty = int(ev.get("filled_qty") or b.get("filled_qty") or 0)
            fill_vwap = ev.get("fill_vwap") or b.get("filled_price")
            filled = filled_qty > 0
            slippage = (float(fill_vwap) / ref_px - 1.0) if (filled and fill_vwap and ref_px > 0) else None
            frozen = 0.0 if filled else float(b.get("qty_placed") or 0) * ref_px
            rows.append({"code": code, "gap": b.get("gap"), "ref_price": ref_px,
                         "qty_placed": int(b.get("qty_placed") or 0),
                         "filled": filled, "filled_qty": filled_qty,
                         "fill_vwap": fill_vwap, "slippage": slippage,
                         "frozen_notional": round(frozen, 2),
                         "reject_time": ev.get("reject_time"),
                         "gap_bucket": _bucket(b.get("gap"))})
        n = len(rows)
        nf = sum(1 for r in rows if r["filled"])
        slips = [r["slippage"] for r in rows if r["slippage"] is not None]
        days.append({
            "date": date, "mode": fills.get("mode"),
            "orders": n, "filled": nf,
            "fill_rate": (round(nf / n, 4) if n else None),
            "frozen_notional": round(sum(r["frozen_notional"] for r in rows), 2),
            "avg_slippage": (round(sum(slips) / len(slips), 6) if slips else None),
            "rows": rows})
    return days


def aggregate(days: list) -> dict:
    by_bucket = {name: {"orders": 0, "filled": 0} for _, _, name in GAP_BUCKETS}
    tot_o = tot_f = 0
    tot_frozen = 0.0
    slips = []
    for d in days:
        for r in d["rows"]:
            tot_o += 1
            tot_frozen += r["frozen_notional"]
            b = by_bucket.setdefault(r["gap_bucket"], {"orders": 0, "filled": 0})
            b["orders"] += 1
            if r["filled"]:
                tot_f += 1
                b["filled"] += 1
                if r["slippage"] is not None:
                    slips.append(r["slippage"])
    for name, b in by_bucket.items():
        b["fill_rate"] = round(b["filled"] / b["orders"], 4) if b["orders"] else None
    return {"orders": tot_o, "filled": tot_f,
            "fill_rate": (round(tot_f / tot_o, 4) if tot_o else None),
            "frozen_notional_total": round(tot_frozen, 2),
            "avg_slippage": (round(sum(slips) / len(slips), 6) if slips else None),
            "by_gap_bucket": by_bucket}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    days = collect(only)
    agg = aggregate(days)
    print("=== OSC 买侧成交统计（bridge 事件为真源）===")
    for d in days:
        print(f"{d['date']} [{d['mode']}] 下单 {d['orders']} 成交 {d['filled']} "
              f"成交率 {d['fill_rate']} 冻结 {d['frozen_notional']:.0f} 滑点 {d['avg_slippage']}")
        for r in d["rows"]:
            mark = "✓成交" if r["filled"] else "✗未成交"
            slip = ("滑点 %+.2f%%" % (r["slippage"] * 100)) if r["slippage"] is not None else "-"
            print(f"    {r['code']} gap={float(r['gap'] or 0) * 100:.2f}% "
                  f"[{r['gap_bucket']}] {r['qty_placed']}股@{r['ref_price']:.2f} "
                  f"{mark} {slip} 冻结={r['frozen_notional']:.0f} {r['reject_time'] or ''}")
    print("--- 汇总 ---")
    print(f"总下单 {agg['orders']} 总成交 {agg['filled']} 成交率 {agg['fill_rate']} "
          f"累计冻结 {agg['frozen_notional_total']:.0f} 平均滑点 {agg['avg_slippage']}")
    for name, b in agg["by_gap_bucket"].items():
        if b["orders"]:
            print(f"  gap {name}: {b['filled']}/{b['orders']} 成交率 {b['fill_rate']}")
    os.makedirs(OUT_DIR, exist_ok=True)
    fp = os.path.join(OUT_DIR, "osc_fill_stats_latest.json")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"days": days, "aggregate": agg}, ensure_ascii=False, indent=2))
    print(f"[stats] 落盘 {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
