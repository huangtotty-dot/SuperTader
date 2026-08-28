# -*- coding: utf-8 -*-
"""P1-3 双源对照（合并实施方案 P1-3，2026-08-28）
每日收盘后对 watchlist 全部股票分别用 gm / 腾讯拉当日日线，比对 close 差异。
产出 t_io/validation/data_parity_{date}.json（差异率 = |gm_close/tx_close - 1| > 0.001 的股票占比）。
验收闸：连续 5 个交易日差异率 <0.1% 或差异全部可解释（分红除权/复权口径）。

用法: python scripts/data_source_parity.py [--date YYYY-MM-DD] [--days N]
"""
import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
WATCHLIST = os.path.join(BASE, "t_io", "state", "watchlist_buy.json")
OUT_DIR = os.path.join(BASE, "t_io", "validation")
THRESHOLD = 0.001  # |gm/tx - 1| > 0.001 视为差异


def load_watchlist():
    w = json.load(open(WATCHLIST, encoding="utf-8"))
    return [k for k in w.get("stocks", {}) if k.isdigit()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    from core.market_data.gm_provider import GmProvider
    from core.market_data.tencent_provider import TencentProvider
    gm, tx = GmProvider(), TencentProvider()
    if not getattr(gm, "_ready", False):
        print("[parity][FATAL] gm 终端/会话 token 不可用，无法对照")
        return 1

    codes = load_watchlist()
    total, diff, mism = 0, 0, []
    no_gm = no_tx = 0
    rows = []
    for code in codes:
        try:
            g = gm.daily(code, a.days)
            t = tx.daily(code, a.days)
        except Exception as e:
            print(f"  [WARN] {code} 拉取失败: {str(e)[:80]}")
            continue
        if g.empty or t.empty:
            if g.empty:
                no_gm += 1
            if t.empty:
                no_tx += 1
            continue
        gm_map = dict(zip(g["date"], g["close"]))
        tx_map = dict(zip(t["date"], t["close"]))
        common = sorted(set(gm_map) & set(tx_map))
        if not common:
            continue
        for d in common:
            total += 1
            gv, tv = float(gm_map[d]), float(tx_map[d])
            ratio = abs(gv / tv - 1) if tv else float("inf")
            if ratio > THRESHOLD:
                diff += 1
                mism.append({"code": code, "date": d, "gm": gv, "tx": tv, "ratio": round(ratio, 5)})
        rows.append(code)

    rate = diff / total if total else 0.0
    out = {
        "date": a.date, "days": a.days, "codes_checked": len(rows),
        "pairs_compared": total, "diff_pairs": diff, "divergence_rate": round(rate, 6),
        "threshold": THRESHOLD, "no_gm": no_gm, "no_tx": no_tx,
        "examples": mism[:15], "mism_count": len(mism),
        "pass": rate < 0.001,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    fp = os.path.join(OUT_DIR, f"data_parity_{a.date}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[parity] {a.date} 对照 {len(rows)} 只 / {total} 对，差异 {diff}，差异率 {rate:.4%}")
    print(f"[parity] 结论: {'通过(<0.1%)' if rate < 0.001 else '超阈值，需解释'}")
    if mism[:5]:
        print(f"[parity] 差异示例: {mism[:5]}")
    print(f"[parity] 已写入 {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
