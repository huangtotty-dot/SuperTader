# -*- coding: utf-8 -*-
"""补记 2026-09-10 盈亏记录到 daily_pnl.jsonl（W37 P2 遗留；09-10 当日 14:59 档未落盘）。

口径与 main.py:_maybe_record_daily_pnl 一致（字段顺序/round 规则照抄）；数据源与
`_backfill_pnl_0821.py` 相同（腾讯日K），已交叉验证：10 票的 2026-09-09 收盘 == 09-10 快照
pre_close，全部吻合 → 数据源可信。

t0_realized 记 0.0：与生产口径一致，而非"09-10 无做T"——14:50 主审计会 clear VIRTUAL_TRADES，
14:59 读到的恒为空（历史记录除 09-01 外全为 0.0）。09-10 实际有做T（closed_loop：588170
29100/29100、600176 1200/600、600481 1400/6100），其盈亏见 daily_review_2026-09-10.json。
持仓基准取 t_io/state/holdings_2026-09-10.json（当日 14:50 快照）。

记录带 backfilled: true 标记。幂等：已存在该日记录则跳过。
运行：python t_io/validation/daily_review/_backfill_pnl_0910.py [--date 2026-09-10] [--write]
不加 --write 只做 dry-run 打印，不落盘。
"""
import argparse
import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PNL = os.path.join(_ROOT, "t_io", "logs", "daily_pnl.jsonl")

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)


def closes(code: str, date: str) -> dict:
    sym = ("sh" if code[0] in "56" else "sz") + code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,40,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.qq.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    kl = d.get("data", {}).get(sym, {})
    rows = kl.get("day") or kl.get("qfqday") or []
    m = {r[0]: float(r[2]) for r in rows}
    if date not in m:
        raise RuntimeError(f"{code} 无 {date} 日K")
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-10")
    ap.add_argument("--write", action="store_true", help="落盘（缺省仅 dry-run 打印）")
    args = ap.parse_args()
    date = args.date

    with open(PNL, encoding="utf-8") as f:
        existing = [json.loads(l) for l in f if l.strip()]
    if any(r.get("date") == date for r in existing):
        print(f"{date} 记录已存在，跳过")
        return 0

    snap_fp = os.path.join(_ROOT, "t_io", "state", f"holdings_{date}.json")
    if not os.path.exists(snap_fp):
        print(f"[ERROR] 无当日持仓快照: {snap_fp}")
        return 1
    h = json.load(open(snap_fp, encoding="utf-8"))
    held = {c: p for c, p in h.items()
            if isinstance(p, dict) and (p.get("qty") or p.get("base") or p.get("t_qty"))}
    if not held:
        print(f"[ERROR] {snap_fp} 无持仓")
        return 1

    stocks, total_value, total_cost, total_t0 = [], 0.0, 0.0, 0.0
    for code, p in sorted(held.items()):
        qty = int(p.get("qty", 0) or 0)
        cost = float(p.get("cost", 0) or 0)
        pre = float(p.get("pre_close", 0) or 0)
        m = closes(code, date)
        prev_day = sorted(d for d in m if d < date)[-1]
        if abs(m[prev_day] - pre) > 1e-6:
            print(f"[WARN] {code} {prev_day} 收盘 {m[prev_day]} != 快照 pre_close {pre}（基准存疑）")
        price = m[date]
        day_pnl = (price - pre) * qty if pre > 0 else 0.0
        day_pct = (price / pre - 1) * 100 if pre > 0 else 0.0
        mkt_val = price * qty
        total_value += mkt_val
        total_cost += cost * qty
        stocks.append({"code": code, "name": p.get("name", code), "qty": qty,
                       "price": round(price, 2), "day_pnl": round(day_pnl, 2),
                       "day_pct": round(day_pct, 2), "mkt_val": round(mkt_val, 0),
                       "t0_pnl": 0.0})

    rec = {"date": date,
           "pushed_at": f"{date} 14:59:00",
           "total_value": round(total_value, 2),
           "total_cost": round(total_cost, 2),
           "total_pnl": round(total_value - total_cost, 2),
           "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0.0,
           "day_pnl_float": round(sum(s["day_pnl"] for s in stocks), 2),
           "t0_realized": total_t0,
           "backfilled": True,
           "backfill_note": "09-10 14:59 档未落盘，2026-09-14 按腾讯日K收盘 + 当日 14:50 快照补记",
           "stocks": stocks}

    for s in stocks:
        print(f"  {s['code']} {s['qty']:>6} 股 @ {s['price']:>7}  {s['day_pct']:+6.2f}%  日浮 {s['day_pnl']:>10.2f}")
    print(json.dumps({k: rec[k] for k in ("date", "total_value", "total_pnl",
                                          "total_pnl_pct", "day_pnl_float")}, ensure_ascii=False))

    if not args.write:
        print("\n[dry-run] 未落盘；加 --write 追加到 daily_pnl.jsonl")
        return 0
    with open(PNL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n[OK] 已补记 {date} → {PNL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
