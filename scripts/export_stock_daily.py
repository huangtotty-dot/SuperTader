# -*- coding: utf-8 -*-
"""导出单只股票的长历史日线（新浪源，含前复权列）。

用途：本地 `t_io/cache/daily_kline` 里的日线是**腾讯 qfq、最多 ~800 根**（2023-07 起），
更早的历史要靠新浪：`CN_MarketData.getKLineData`（不复权，最多 6000 根 ≈ 24 年）
+ `qfq.js`（新浪自己的前复权因子）。

⚠️ 两个口径差异要说清：新浪前复权与腾讯 qfq 在**最新一日一致**（因子=1），
但历史端因分红口径不同会差 0~3%（实测 000988 在 2023-07-04 差 2.7%）。
所以本脚本同时输出**原始不复权**与**新浪前复权**两组价，用哪组由调用方决定。

用法：
    python scripts/export_stock_daily.py 000988 --start 2013-11-08
    python scripts/export_stock_daily.py 600519 --start 2013-11-08 --out D:/tmp
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "t_io", "validation", "daily")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://finance.sina.com.cn/"}


def _sina_symbol(code: str) -> str:
    code = str(code).zfill(6)
    return ("sh" if code.startswith(("6", "5", "9")) else "sz") + code


def _get(url: str) -> str:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "ignore")


def fetch_raw(symbol: str, n: int = 6000):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
           f"?symbol={symbol}&scale=240&ma=no&datalen={n}")
    rows = json.loads(_get(url))
    return [{"date": r["day"], "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}
            for r in rows]


def fetch_qfq_factors(symbol: str):
    js = _get(f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js")
    return json.loads(js[js.index("{"): js.rindex("}") + 1])["data"]   # 降序


def factor_on(arr, date: str) -> float:
    for x in arr:                       # 降序：第一条 d<=date 就是该日适用的因子
        if x["d"] <= date:
            return float(x["f"])
    return float(arr[-1]["f"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--start", default="2013-11-08")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--rows", type=int, default=6000)
    a = ap.parse_args()

    sym = _sina_symbol(a.code)
    raw = fetch_raw(sym, a.rows)
    fac = fetch_qfq_factors(sym)
    end = a.end or raw[-1]["date"]
    rows = [r for r in raw if a.start <= r["date"] <= end]
    if not rows:
        raise SystemExit(f"没有 {a.start}~{end} 的数据（新浪只给到 {raw[0]['date']}）")

    out = []
    for r in rows:
        f = factor_on(fac, r["date"])
        out.append({
            "日期": r["date"],
            "开盘": r["open"], "最高": r["high"], "最低": r["low"], "收盘": r["close"],
            "成交量(股)": int(r["volume"]),
            "前复权开盘": round(r["open"] / f, 3), "前复权最高": round(r["high"] / f, 3),
            "前复权最低": round(r["low"] / f, 3), "前复权收盘": round(r["close"] / f, 3),
            "复权因子": f,
        })

    d = a.out or OUT_DIR
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, f"{a.code}_{a.start.replace('-','')}_{end.replace('-','')}_日线.csv")
    with open(fp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"{a.code} {len(out)} 行  {out[0]['日期']} ~ {out[-1]['日期']}  →  {fp}")
    print(f"  收盘：首 {out[0]['收盘']}（前复权 {out[0]['前复权收盘']}）· "
          f"末 {out[-1]['收盘']}（前复权 {out[-1]['前复权收盘']}）")


if __name__ == "__main__":
    main()
