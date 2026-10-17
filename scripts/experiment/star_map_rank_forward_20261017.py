# -*- coding: utf-8 -*-
"""星空图能不能当择板依据？—— 板块排名 / 排名变化 → 前向超额收益（2026-10-17）

owner 的问题：
    「直接追排名靠前的，很可能追高套牢；选中位有潜力的最合适。到底存在什么规律？」

本脚本只做**度量**，不改任何生产逻辑。口径：
  · 样本 = `stock_hunter/history/daily_summary.json` 的逐日板块排名（≥16 个板块的日子）
  · 板块成分 = 当前 watchlist 的「韭研分类」（⚠️ 用今天的成分回溯，见 doc 的局限）
  · 前向收益 = 板块内**等权**个股 close(t+H)/close(t)-1，H ∈ {1,3,5} 交易日
  · **超额** = 减去当日所有板块的横截面均值（去掉大盘 beta，只留"选哪个板块"的差异）
  · 统计 = 每个交易日的横截面 Spearman IC → 对日序列求均值与 t；分桶看均值与 t

用法：
    python -I scripts/experiment/star_map_rank_forward_20261017.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "stock_hunter"))

import numpy as np                                            # noqa: E402

CACHE = os.path.join(ROOT, "t_io", "cache", "daily_kline")
SUMMARY = os.path.join(ROOT, "stock_hunter", "history", "daily_summary.json")
HS = (1, 3, 5)
MIN_BOARDS = 16


# ---------- 1) 交易日历（以指数 bar 为准，周末/假日自动排除） ----------
def trading_days():
    rows = json.loads(open(os.path.join(CACHE, "index_sh000001.json"), encoding="utf-8")
                      .read())["rows"]
    return [str(r["date"])[:10] for r in rows]


# ---------- 2) 板块 → 成分股（与猎手同源：韭研分类） ----------
def board_members():
    from modules.data_loader import DataLoader
    df = DataLoader().load_watchlist()
    out = {}
    for _, r in df.iterrows():
        b = str(r.get("韭研分类") or "").strip()
        c = str(r.get("代码") or "").strip()
        if b and c.isdigit():
            out.setdefault(b, set()).add(c)
    return out


# ---------- 3) 个股收盘价表（只留需要的窗口） ----------
def close_table(codes, keep_dates):
    keep = set(keep_dates)

    def one(c):
        try:
            rows = json.loads(open(os.path.join(CACHE, c + ".json"), encoding="utf-8")
                              .read())["rows"]
        except Exception:
            return c, {}
        return c, {str(r["date"])[:10]: float(r["close"]) for r in rows
                   if str(r["date"])[:10] in keep and r.get("close")}

    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, d in ex.map(one, codes):
            if d:
                out[c] = d
    return out


def _spearman(x, y):
    """Spearman = Pearson on ranks（无 scipy）。"""
    if len(x) < 3:
        return float("nan")
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean(); ry -= ry.mean()
    den = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
    return float((rx * ry).sum() / den) if den else float("nan")


def _tstat(a):
    a = np.asarray([x for x in a if np.isfinite(x)], dtype=float)
    if len(a) < 3:
        return float("nan"), len(a)
    sd = a.std(ddof=1)
    return (float(a.mean() / (sd / np.sqrt(len(a)))) if sd > 0 else float("nan")), len(a)


def main():
    cal = trading_days()
    idx = {d: i for i, d in enumerate(cal)}

    summary = json.loads(open(SUMMARY, encoding="utf-8").read())
    days = [d for d in sorted(summary) if len(summary[d]) >= MIN_BOARDS]
    days = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days]
    days = [d for d in days if d in idx]
    # 前向收益需要 t+H 落在日历内
    hmax = max(HS)
    days = [d for d in days if idx[d] + hmax < len(cal)]
    print(f"样本日 {len(days)} 天：{days[0]} ~ {days[-1]}（≥{MIN_BOARDS} 个板块）")

    members = board_members()
    all_codes = sorted({c for s in members.values() for c in s})
    need = set()
    for d in days:
        i = idx[d]
        for h in HS:
            need.add(cal[i]); need.add(cal[i + h])
    print(f"板块 {len(members)} 个 · 成分股 {len(all_codes)} 只 · 取价日期 {len(need)} 个；读缓存…")
    px = close_table(all_codes, need)
    print(f"  取到价表的股票 {len(px)} 只")

    # ---------- 4) 逐日：每板块的前向收益 ----------
    panel = []      # [{date, board, rank, score, rets:{h:r}, ...}]
    for d in days:
        i = idx[d]
        items = summary[d.replace("-", "")]
        n_board = len(items)
        raw = {}
        for it in items:
            b = str(it.get("板块") or "").strip()
            mem = members.get(b) or set()
            rec = {"rank": int(it.get("排名") or 0), "score": float(it.get("平均分") or 0.0),
                   "n": int(it.get("股票数") or 0)}
            for h in HS:
                d0, d1 = cal[i], cal[i + h]
                rs = []
                for c in mem:
                    t = px.get(c) or {}
                    a, b2 = t.get(d0), t.get(d1)
                    if a and b2:
                        rs.append(b2 / a - 1.0)
                rec[f"r{h}"] = float(np.mean(rs)) if len(rs) >= 5 else float("nan")
                rec[f"n{h}"] = len(rs)
            raw[b] = rec
        # 横截面去均值（超额）
        for h in HS:
            vals = [v[f"r{h}"] for v in raw.values() if np.isfinite(v[f"r{h}"])]
            m = float(np.mean(vals)) if vals else float("nan")
            for v in raw.values():
                v[f"x{h}"] = v[f"r{h}"] - m if np.isfinite(v[f"r{h}"]) else float("nan")
        # 排名变化（用过去 3 个样本日的名次差；>0 = 名次前进）
        for b, v in raw.items():
            prev = None
            j = days.index(d)
            if j >= 3:
                p = panel[-3] if len(panel) >= 3 else None
                p = None
                for old in panel[-3:]:
                    if old["date"] == days[j - 3]:
                        p = old
                        break
                if p:
                    prev = p["by_board"].get(b, {}).get("rank")
            v["drank3"] = (prev - v["rank"]) if prev else None
        panel.append({"date": d, "by_board": raw, "n": n_board})

    # ---------- 5) 分析 ----------
    print("\n" + "=" * 78)
    print("A. 按当日排名分桶 —— 前向**超额**收益（%），括号内为 t（对日序列）")
    print("=" * 78)
    buckets = [(1, 1, "第1名"), (2, 3, "2-3名"), (4, 6, "4-6名"),
               (7, 10, "7-10名"), (11, 15, "11-15名"), (16, 99, "16名以后")]
    print(f"{'桶':<10}" + "".join(f"{'H=' + str(h):>16}" for h in HS))
    for lo, hi, label in buckets:
        cells = []
        for h in HS:
            vals = []
            for p in panel:
                for v in p["by_board"].values():
                    if lo <= v["rank"] <= hi and np.isfinite(v[f"x{h}"]):
                        vals.append(v[f"x{h}"])
            m = float(np.mean(vals)) * 100 if vals else float("nan")
            # t 用按日均值序列（避免同一天多个板块互相膨胀样本量）
            per_day = []
            for p in panel:
                xs = [v[f"x{h}"] for v in p["by_board"].values()
                      if lo <= v["rank"] <= hi and np.isfinite(v[f"x{h}"])]
                if xs:
                    per_day.append(float(np.mean(xs)))
            t, n = _tstat(per_day)
            cells.append(f"{m:+6.2f}% (t{t:+5.2f},n{n})")
        print(f"{label:<10}" + "".join(f"{c:>16}" for c in cells))

    print("\n" + "=" * 78)
    print("B. 横截面 Rank IC：排名(1=最强) 与 前向超额收益 的 Spearman（负=排名越靠前越好）")
    print("=" * 78)
    for h in HS:
        ics = []
        for p in panel:
            xs = [(v["rank"], v[f"x{h}"]) for v in p["by_board"].values()
                  if np.isfinite(v[f"x{h}"])]
            if len(xs) >= 5:
                ics.append(_spearman([a for a, _ in xs], [b for _, b in xs]))
        m, n = _tstat(ics)
        print(f"  H={h}: IC 均值 {np.nanmean(ics):+.4f} · t {m:+.2f} · n={n} 天")

    print("\n" + "=" * 78)
    print("C. 「中位有潜力」假说：4-10 名 vs 1-3 名 vs 11 名以后（H=3）")
    print("=" * 78)
    groups = {"1-3名(追强)": (1, 3), "4-10名(中位)": (4, 10), "11名以后": (11, 99)}
    for name, (lo, hi) in groups.items():
        per_day = []
        for p in panel:
            xs = [v["x3"] for v in p["by_board"].values()
                  if lo <= v["rank"] <= hi and np.isfinite(v["x3"])]
            if xs:
                per_day.append(float(np.mean(xs)))
        m, n = _tstat(per_day)
        print(f"  {name:<14} 均值 {np.mean(per_day)*100:+.2f}% · t {m:+.2f} · n={n} 天")

    print("\n" + "=" * 78)
    print("D. 星轨的「排名持续提升」有没有用：过去 3 日名次前进 vs 退步（H=3）")
    print("=" * 78)
    for name, fn in [("名次前进(Δrank≥2)", lambda x: x >= 2),
                     ("基本不变(-1..1)", lambda x: -1 <= x <= 1),
                     ("名次退步(Δrank≤-2)", lambda x: x <= -2)]:
        per_day = []
        for p in panel:
            xs = [v["x3"] for v in p["by_board"].values()
                  if v.get("drank3") is not None and fn(v["drank3"]) and np.isfinite(v["x3"])]
            if xs:
                per_day.append(float(np.mean(xs)))
        m, n = _tstat(per_day)
        print(f"  {name:<18} 均值 {np.mean(per_day)*100:+.2f}% · t {m:+.2f} · n={n} 天")

    # ---------- 稳健性 ----------
    print("\n" + "=" * 78)
    print("F. 稳健性 1：剔除 2025 那个孤点，只用 2026-06-04 起的密集期")
    print("=" * 78)
    dense = [p for p in panel if p["date"] >= "2026-06-04"]
    print(f"  期间 {dense[0]['date']} ~ {dense[-1]['date']}（{len(dense)} 天）")
    for h in (3, 5):
        ics = []
        for p in dense:
            xs = [(v["rank"], v[f"x{h}"]) for v in p["by_board"].values()
                  if np.isfinite(v[f"x{h}"])]
            if len(xs) >= 5:
                ics.append(_spearman([a for a, _ in xs], [b for _, b in xs]))
        m, n = _tstat(ics)
        print(f"  H={h}: IC {np.nanmean(ics):+.4f} · t {m:+.2f} · n={n} 天")

    print("\n" + "=" * 78)
    print("G. 稳健性 2：**非重叠**采样（每 5 个交易日取一次，H=5）")
    print("=" * 78)
    ics = []
    for i, p in enumerate(panel):
        if i % 5:
            continue
        xs = [(v["rank"], v["x5"]) for v in p["by_board"].values() if np.isfinite(v["x5"])]
        if len(xs) >= 5:
            ics.append(_spearman([a for a, _ in xs], [b for _, b in xs]))
    m, n = _tstat(ics)
    print(f"  H=5 非重叠: IC {np.nanmean(ics):+.4f} · t {m:+.2f} · n={n} 个独立起点")
    for lo, hi, lab in [(1, 3, "1-3名"), (4, 10, "4-10名"), (11, 99, "11名以后")]:
        vals = []
        for i, p in enumerate(panel):
            if i % 5:
                continue
            xs = [v["x5"] for v in p["by_board"].values()
                  if lo <= v["rank"] <= hi and np.isfinite(v["x5"])]
            if xs:
                vals.append(float(np.mean(xs)))
        m2, n2 = _tstat(vals)
        print(f"  {lab:<10} 均值 {np.mean(vals)*100:+.2f}% · t {m2:+.2f} · n={n2}")

    print("\n" + "=" * 78)
    print("H. 稳健性 3：控制**板块大小**（按当日成分股中位数分组，H=5 超额%）")
    print("=" * 78)
    for tag, small in [("小板块(≤中位)", True), ("大板块(>中位)", False)]:
        line = f"  {tag:<14}"
        for lo, hi, lab in [(1, 3, "1-3名"), (4, 10, "4-10名"), (11, 99, "11名后")]:
            per_day = []
            for p in panel:
                ns = [v["n"] for v in p["by_board"].values() if v["n"] > 0]
                med = float(np.median(ns)) if ns else 0
                xs = [v["x5"] for v in p["by_board"].values()
                      if lo <= v["rank"] <= hi and np.isfinite(v["x5"])
                      and ((v["n"] <= med) if small else (v["n"] > med))]
                if xs:
                    per_day.append(float(np.mean(xs)))
            m, n = _tstat(per_day)
            line += f" | {lab} {np.mean(per_day)*100:+5.2f}% (t{m:+5.2f},n{n})"
        print(line)

    print("\n" + "=" * 78)
    print("I. **绝对**收益（不是超额）：H=5 各桶均值 —— 超额为正不等于能赚钱")
    print("=" * 78)
    for lo, hi, lab in [(1, 3, "1-3名"), (4, 10, "4-10名"), (11, 99, "11名以后")]:
        vals = [v["r5"] for p in panel for v in p["by_board"].values()
                if lo <= v["rank"] <= hi and np.isfinite(v["r5"])]
        per_day = []
        for p in panel:
            xs = [v["r5"] for v in p["by_board"].values()
                  if lo <= v["rank"] <= hi and np.isfinite(v["r5"])]
            if xs:
                per_day.append(float(np.mean(xs)))
        m, n = _tstat(per_day)
        print(f"  {lab:<10} 绝对 {np.mean(vals)*100:+.2f}% · t {m:+.2f} · n={n} 天 · 样本 {len(vals)}")

    print("\n" + "=" * 78)
    print("E. 交叉：排名位置 × 名次变化（H=3，均值%）")
    print("=" * 78)
    for lo, hi, lab in [(1, 3, "1-3名"), (4, 10, "4-10名"), (11, 99, "11名以后")]:
        line = f"  {lab:<10}"
        for name, fn in [("前进", lambda x: x >= 2), ("不变", lambda x: -1 <= x <= 1),
                         ("退步", lambda x: x <= -2)]:
            per_day = []
            for p in panel:
                xs = [v["x3"] for v in p["by_board"].values()
                      if lo <= v["rank"] <= hi and v.get("drank3") is not None
                      and fn(v["drank3"]) and np.isfinite(v["x3"])]
                if xs:
                    per_day.append(float(np.mean(xs)))
            m, n = _tstat(per_day)
            line += f" | {name} {np.mean(per_day)*100:+5.2f}% (t{m:+5.2f},n{n})"
        print(line)


if __name__ == "__main__":
    main()
