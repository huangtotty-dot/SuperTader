"""实测：给选股猎手加「站上5日线」打分项，到底有没有区分度。

思路：打分器的强势项（D1/D2/D5/D6）都基于「今日最高 > 近N日最高」。若这些票**必然**已在
MA5 上方，则「站上5日线」在候选池里近乎常数 ⇒ 对排序零影响（只给所有人加同一个分）。

用 stock_hunter/data/market_*.csv（打分器输入就是这个）+ t_io/cache/daily_kline 算 MA5。
用法：python tmp/probe_hunter_ma5_redundancy.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, BASE)

from core.ma_reclaim import ma5_state  # noqa: E402

BASE = Path(BASE)
DAILY = BASE / "t_io" / "cache" / "daily_kline"

# 取最新的 market 快照（就是喂给打分器的那份）
snaps = sorted((BASE / "stock_hunter" / "data").glob("market_*.csv"))
fp = snaps[-1]
print(f"快照: {fp.name}")

rows = []
with open(fp, encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        rows.append(r)
print(f"候选池: {len(rows)} 只\n")


def f(v):
    try:
        return float(v)
    except Exception:
        return None


stats = {"total": 0, "above": 0, "below": 0, "nodata": 0}
# 「会得分」= 满足任一强势项（今日最高 > 近N日最高）
scoring = {"n": 0, "above": 0, "below": 0}
only_above_ma5 = {"n": 0}
devs = []

for r in rows:
    code = (r.get("代码") or "").strip()
    if not code:
        continue
    dfp = DAILY / f"{code}.json"
    if not dfp.exists():
        stats["nodata"] += 1
        continue
    try:
        rec = json.loads(dfp.read_text(encoding="utf-8"))
        bars = rec.get("rows") or rec.get("data") or rec
        closes = [float(b["close"]) for b in bars if b.get("close") is not None]
    except Exception:
        stats["nodata"] += 1
        continue
    if len(closes) < 6:
        stats["nodata"] += 1
        continue

    st = ma5_state(closes)
    if not st:
        stats["nodata"] += 1
        continue
    stats["total"] += 1
    is_above = bool(st["above"])
    stats["above" if is_above else "below"] += 1
    devs.append(st["dev5_pct"])

    high = f(r.get("最高"))
    h5, h10, h20, h150 = (f(r.get("近5日最高")), f(r.get("近10日最高")),
                          f(r.get("近20日最高")), f(r.get("近150日最高")))
    would_score = False
    for hh in (h5, h10, h20, h150):
        if high is not None and hh is not None and hh > 0 and high > hh:
            would_score = True
            break
    # D2：近5日涨幅>20% 且 最高>近20日最高
    chg5 = f(r.get("近5日涨幅"))
    if chg5 is not None and chg5 > 20 and h20 and high and high > h20:
        would_score = True

    if would_score:
        scoring["n"] += 1
        scoring["above" if is_above else "below"] += 1
    if is_above:
        only_above_ma5["n"] += 1

print("=== 全池 ===")
t = stats["total"]
print(f"  有数据 {t} 只（无数据 {stats['nodata']}）")
print(f"  现价 > MA5 : {stats['above']} 只 ({stats['above']/t*100:.1f}%)")
print(f"  现价 < MA5 : {stats['below']} 只 ({stats['below']/t*100:.1f}%)")

print("\n=== 只看得分器真正会加分的票（今日最高 > 近N日最高）===")
n = scoring["n"]
if n:
    print(f"  {n} 只 ({n/t*100:.1f}% of 池)")
    print(f"  其中 现价 > MA5 : {scoring['above']} 只 ({scoring['above']/n*100:.1f}%)")
    print(f"  其中 现价 < MA5 : {scoring['below']} 只 ({scoring['below']/n*100:.1f}%)")
    print(f"\n  ⇒ 「站上5日线」在这一层的**区分度**：只有 "
          f"{scoring['below']/n*100:.1f}% 的得分票会被它判为0分（其余给同分=不改排序）")
else:
    print("  无")

if devs:
    devs.sort()
    k = len(devs)
    print(f"\n=== 偏离5日线 dev5_pct 分布（{k} 只）===")
    for q, lab in ((0, "min"), (k // 10, "P10"), (k // 4, "P25"), (k // 2, "中位"),
                   (k * 3 // 4, "P75"), (k * 9 // 10, "P90"), (k - 1, "max")):
        print(f"  {lab:>4}: {devs[q]:+7.2f}%")
