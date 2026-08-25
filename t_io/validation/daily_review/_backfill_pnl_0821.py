# -*- coding: utf-8 -*-
"""补记 2026-08-21 盈亏记录到 daily_pnl.jsonl（t_trader 14:52 消失错过 14:59 推送档）。
口径与 main.py:_maybe_push_daily_pnl_summary 一致；t0_realized=0（今日无虚拟成交）。
记录带 backfilled: true 标记。幂等：已存在 08-21 记录则跳过。"""
import json
import os
import urllib.request
from datetime import datetime

PNL = r"E:\superTrader\t_io\logs\daily_pnl.jsonl"

for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)


def close_of(code):
    sym = ("sh" + code if code[0] in "56" else "sz" + code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,5,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.qq.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    kl = d.get("data", {}).get(sym, {})
    rows = kl.get("day") or kl.get("qfqday") or []
    last = rows[-1]
    assert last[0] == "2026-08-21", f"{code} 最新K线 {last[0]} 非今日"
    return float(last[2])


def main():
    with open(PNL, encoding="utf-8") as f:
        existing = [json.loads(l) for l in f if l.strip()]
    if any(r.get("date") == "2026-08-21" for r in existing):
        print("08-21 记录已存在，跳过")
        return
    h = json.load(open(r"E:\superTrader\holdings.json", encoding="utf-8"))
    hs = h.get("holdings", h)
    stocks, total_value, total_cost = [], 0.0, 0.0
    for code, p in sorted(hs.items()):
        qty = int(p.get("qty", 0))
        cost = float(p.get("cost", 0))
        pre = float(p.get("pre_close", 0))
        price = close_of(code.replace("_B", ""))
        day_pnl = (price - pre) * qty if pre > 0 else 0.0
        day_pct = (price / pre - 1) * 100 if pre > 0 else 0.0
        mkt = price * qty
        total_value += mkt
        total_cost += cost * qty
        stocks.append({"code": code, "name": p.get("name", code), "qty": qty,
                       "price": round(price, 2), "day_pnl": round(day_pnl, 2),
                       "day_pct": round(day_pct, 2), "mkt_val": round(mkt, 0),
                       "t0_pnl": 0.0})
    rec = {"date": "2026-08-21",
           "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "total_value": round(total_value, 2),
           "total_cost": round(total_cost, 2),
           "total_pnl": round(total_value - total_cost, 2),
           "total_pnl_pct": round((total_value / total_cost - 1) * 100, 2) if total_cost else 0,
           "day_pnl_float": round(sum(s["day_pnl"] for s in stocks), 2),
           "t0_realized": 0.0, "backfilled": True, "stocks": stocks}
    with open(PNL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({k: rec[k] for k in ("date", "total_value", "total_pnl",
                                          "total_pnl_pct", "day_pnl_float")},
                     ensure_ascii=False))
    for s in stocks:
        print(" ", s["code"], s["qty"], "股 @", s["price"],
              f"{s['day_pct']:+.2f}%", "日浮", s["day_pnl"])


if __name__ == "__main__":
    main()
