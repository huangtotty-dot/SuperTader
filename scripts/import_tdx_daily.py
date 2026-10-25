# -*- coding: utf-8 -*-
"""直接读通达信本地日线（vipdoc/*/lday/*.day）并导出，可自动补更早的历史。

通达信日线文件格式（每 32 字节一条，小端）：
    uint32 date(YYYYMMDD) / open / high / low / close  （价格 ×100）
    float32 amount（元） / uint32 volume（股） / uint32 reserved

两个要点：
1. **通达信 lday 是不复权原始价** —— 与新浪 `getKLineData` 同口径，两段可以直接拼接；
   所以「本地 5 年 + 新浪 2013 起」拼出来是一条连续的不复权序列，再统一用新浪 qfq 因子转前复权。
2. 本机实测：全部 9506 个 .day 都是 **2021-08-02 ~ 2026-10-23（1265 根）**，
   即通达信只保留了约 5 年。要更长历史需在客户端「盘后数据下载 → 日线 → 全部」再下。

用法：
    python scripts/import_tdx_daily.py 000988                      # 只导本地(2021-08 起)
    python scripts/import_tdx_daily.py 000988 --fill-before        # 用新浪补到更早(默认 2013-11-08)
    python scripts/import_tdx_daily.py 000988 --fill-before --start 2013-11-08 --compare
"""
import argparse
import csv
import json
import os
import struct
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

TDX_ROOT = os.environ.get("TDX_HOME", r"C:\new_tdx")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "t_io", "validation", "daily")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://finance.sina.com.cn/"}
REC = struct.Struct("<IIIIIfII")


def tdx_path(code: str) -> str:
    code = str(code).zfill(6)
    mkt = "sh" if code.startswith(("6", "5", "9")) else ("bj" if code.startswith(("4", "8", "92")) else "sz")
    return os.path.join(TDX_ROOT, "vipdoc", mkt, "lday", f"{mkt}{code}.day")


def read_tdx(code: str):
    fp = tdx_path(code)
    if not os.path.exists(fp):
        raise SystemExit(f"找不到通达信日线文件：{fp}")
    raw = open(fp, "rb").read()
    out = []
    for i in range(len(raw) // REC.size):
        d, o, h, l, c, amt, vol, _ = REC.unpack_from(raw, i * REC.size)
        out.append({"date": f"{d//10000:04d}-{d//100%100:02d}-{d%100:02d}",
                    "open": o / 100.0, "high": h / 100.0, "low": l / 100.0, "close": c / 100.0,
                    "volume": float(vol), "amount": float(amt), "src": "tdx"})
    return out, fp


def _get(url: str) -> str:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "ignore")


def read_sina(code: str, n: int = 6000):
    sym = ("sh" if str(code).zfill(6).startswith(("6", "5", "9")) else "sz") + str(code).zfill(6)
    js = json.loads(_get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                         f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}"))
    return [{"date": r["day"], "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]), "volume": float(r["volume"]), "amount": None, "src": "sina"} for r in js]


def qfq_factors(code: str):
    sym = ("sh" if str(code).zfill(6).startswith(("6", "5", "9")) else "sz") + str(code).zfill(6)
    js = _get(f"https://finance.sina.com.cn/realstock/company/{sym}/qfq.js")
    return json.loads(js[js.index("{"): js.rindex("}") + 1])["data"]      # 降序


def factor_on(arr, date: str) -> float:
    for x in arr:
        if x["d"] <= date:
            return float(x["f"])
    return float(arr[-1]["f"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--start", default="2013-11-08")
    ap.add_argument("--fill-before", action="store_true", help="用新浪补通达信起点之前的历史")
    ap.add_argument("--compare", action="store_true", help="与新浪重叠段逐日比对")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    rows, fp = read_tdx(a.code)
    print(f"通达信：{os.path.basename(fp)}  {len(rows)} 根  {rows[0]['date']} ~ {rows[-1]['date']}")

    sina = read_sina(a.code)
    if a.compare:
        tmap = {r["date"]: r for r in rows}
        diffs = []
        for s in sina:
            t = tmap.get(s["date"])
            if not t:
                continue
            for k in ("open", "high", "low", "close"):
                diffs.append(abs(t[k] - s[k]))
        if diffs:
            print(f"  与新浪重叠 {len([1 for s in sina if s['date'] in tmap])} 天："
                  f"OHLC 最大差 {max(diffs):.3f}，>0.01 的 {sum(1 for d in diffs if d > 0.01)} 个")

    if a.fill_before and sina and sina[0]["date"] < rows[0]["date"]:
        head = [s for s in sina if s["date"] < rows[0]["date"]]
        rows = head + rows
        print(f"  用新浪补 {len(head)} 根（{head[0]['date']} 起）")

    rows = [r for r in rows if r["date"] >= a.start]
    fac = qfq_factors(a.code)
    out = []
    for r in rows:
        f = factor_on(fac, r["date"]) or 1.0
        out.append({"日期": r["date"], "开盘": r["open"], "最高": r["high"], "最低": r["low"],
                    "收盘": r["close"], "成交量(股)": int(r["volume"]),
                    "成交额(元)": int(r["amount"]) if r.get("amount") else "",
                    "前复权收盘": round(r["close"] / f, 3), "复权因子": f, "来源": r["src"]})

    d = a.out or OUT_DIR
    os.makedirs(d, exist_ok=True)
    fp_out = os.path.join(d, f"{a.code}_{out[0]['日期'].replace('-','')}_{out[-1]['日期'].replace('-','')}_日线_TDX.csv")
    with open(fp_out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    n_tdx = sum(1 for r in out if r["来源"] == "tdx")
    print(f"导出 {len(out)} 行（通达信 {n_tdx} + 新浪 {len(out)-n_tdx}）→ {fp_out}")


if __name__ == "__main__":
    main()
