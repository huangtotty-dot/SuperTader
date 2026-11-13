# -*- coding: utf-8 -*-
"""用本地日线缓存自算「每日涨停 / 跌停 / 炸板」家数 → t_io/cache/sentiment_local.json

## 为什么

「市场情绪」面板要能看 360 个交易日（owner 2026-11-13），但同花顺涨停池的
`limit_up_pool` 接口**只回 2025-07-08 以后**（实测更早的日期 status_code=-1，全是空），
本地缓存里只有 328 个交易日。⇒ 用全池日线自算，把更早的历史补出来。

## 口径（与同花顺互验过）

按板块涨跌幅限制判：主板 10%（60/00）、创业板/科创板 20%（30/68）、北交所 30%（43/83/87/92）。
- **涨停**：`close >= 涨停价` 且 `|close/涨停价-1| < 0.5%`
  （后半条是排除新股首日 +60% 这种"远超涨停"的误判）。
- **炸板**：`high >= 涨停价` 但 `close < 涨停价`（盘中摸过板、收盘没封住）。
- **触板** = 涨停 + 炸板；**跌停**同构（用 low/close 对跌停价）。

2026-11-13 在 328 个重叠日上互验：涨停家数与同花顺相关 **0.944**（均值 67.2 vs 65.5），
跌停 0.853（15.4 vs 12.3）。⇒ 只用来补历史，**近年仍以同花顺为准**（后端按日期合并）。

## 用法

    python scripts/ops/build_sentiment_local.py            # 全量重算（约 1~2 分钟）
    python scripts/ops/build_sentiment_local.py --days 800 # 每只只读最后 N 根

产出**静态历史档**：只用于同花顺没有的早期日期（那段不会再变），不需要每天重跑；
只有想再往深挖历史时才重跑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
DAILY = BASE / "t_io" / "cache" / "daily_kline"
OUT = BASE / "t_io" / "cache" / "sentiment_local.json"

_BOARD_10 = ("60", "00")
_BOARD_20 = ("30", "68")


def is_stock(code: str) -> bool:
    c = str(code)
    if len(c) != 6 or not c.isdigit() or c[0] in "15":
        return False
    if c[:2] in ("51", "56", "58", "50", "52", "11", "12", "13"):
        return False
    return c[:2] in _BOARD_10 + _BOARD_20 or c[0] in "48" or c[:3] == "920"


def limit_rate(code: str) -> float:
    c = str(code)
    if c[:3] == "920" or c[:2] in ("43", "83", "87") or c[0] in "48":
        return 0.30
    if c[:2] in _BOARD_20:
        return 0.20
    return 0.10


def scan_one(fp: Path, days: int):
    """→ (events, dates)

    - `events`: {date: (zt, zb, dt)} 只装当天有涨停/炸板/跌停的日；
    - `dates`：该股**全部有效交易日**（用于算每天的全市场覆盖家数，样本闸用）。
    """
    code = fp.stem
    rate = limit_rate(code)
    try:
        rows = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
    except Exception:
        return None
    if days and len(rows) > days:
        rows = rows[-days:]
    events, dates = {}, []
    prev = None
    for r in rows:
        d = str(r.get("date"))[:10]
        try:
            c = float(r.get("close") or 0)
            h = float(r.get("high") or 0)
        except (TypeError, ValueError):
            prev = None
            continue
        if prev and prev > 0 and c > 0:
            dates.append(d)
            up_lim = round(prev * (1 + rate), 2)
            dn_lim = round(prev * (1 - rate), 2)
            # 容差说明（实测校准过）：
            #  · 下沿放到 −0.3%：缓存是**前复权**价，`prev*1.1` 四舍五入后与真实涨停价
            #    常差一两分钱 ⇒ 卡死 `c >= up_lim` 会漏掉约 10% 的真涨停（实测比同花顺少 7 家/日）；
            #  · 上沿卡 2%：排除新股首日/长期停牌复牌那种"远超涨停"的假信号。
            zt = (c >= up_lim * 0.997) and (c <= up_lim * 1.02)
            zb = (h >= up_lim * 0.997) and (c < up_lim * 0.997)
            dt = (c <= dn_lim * 1.003) and (c >= dn_lim * 0.98)
            if zt or zb or dt:
                events[d] = (1 if zt else 0, 1 if (zb and not zt) else 0, 1 if dt else 0)
        prev = c
    return events, dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="每只只读最后 N 根（0=全部）")
    ap.add_argument("--workers", type=int, default=4, help="线程数（本机 4 物理核，别超）")
    args = ap.parse_args()

    files = [p for p in DAILY.glob("*.json") if is_stock(p.stem)]
    print(f"扫描 {len(files)} 只（days={args.days or '全部'}，workers={args.workers}）", flush=True)
    t0 = time.time()
    agg = defaultdict(lambda: [0, 0, 0])          # date -> [zt, zb, dt]
    cover = defaultdict(int)                       # date -> 当日有有效价的家数（样本闸）
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for res in ex.map(lambda p: scan_one(p, args.days), files):
            done += 1
            if not res:
                continue
            events, dates = res
            for d, (a, b, c) in events.items():
                v = agg[d]
                v[0] += a
                v[1] += b
                v[2] += c
            for d in dates:
                cover[d] += 1
            if done % 1000 == 0:
                print(f"  {done}/{len(files)}  已覆盖 {len(agg)} 个交易日", flush=True)

    series = {}
    MIN_COVER = 300          # <300 只的日期是早期/数据缺口（如停牌多年的股凑出来的 1995 年），丢
    # ⚠️ **排除今天**：盘中跑会拿到半成品 bar（量/涨停都只到此刻），而本档是静态历史，
    #    留着就会把"上午的涨停家数"冻成今天的收盘值。今天的数据由同花顺 14:30 那轮负责。
    today = datetime.now().strftime("%Y-%m-%d")
    for d in sorted(agg):
        if cover.get(d, 0) < MIN_COVER or d >= today:
            continue
        zt, zb, dt = agg[d]
        touch = zt + zb
        series[d] = {"zt": zt, "dt": dt, "touch": touch, "zb": zb, "n": cover.get(d, 0),
                     "zb_rate": (round(zb / touch, 4) if touch else None)}
    out = {"computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "universe": len(files), "rows_per_stock": args.days,
           "source": "local_daily_kline",
           "note": "本地日线自算（前复权；涨停判定含 0.5% 容差以排除新股首日）",
           "start": (sorted(series)[0] if series else None),
           "end": (sorted(series)[-1] if series else None),
           "series": series}
    os.makedirs(OUT.parent, exist_ok=True)
    tmp = str(OUT) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"done: {len(series)} 个交易日 {out['start']} → {out['end']}，"
          f"耗时 {time.time()-t0:.1f}s → {OUT}")


if __name__ == "__main__":
    sys.exit(main())
