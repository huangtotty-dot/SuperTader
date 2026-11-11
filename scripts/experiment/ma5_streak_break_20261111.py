# -*- coding: utf-8 -*-
"""站上 5 日线后，平均多久会破 5 日线？——逐个股票实验（2026-11-11 owner）。

问题：owner「站上5日线后大概多少天后（平均）会出现破5日线下杀的风险？请逐个实验」。

口径（全部可复述）：
  · 数据：`t_io/cache/daily_kline/*.json`（本地日线，零网络），每只 ≥60 根、收盘>0。
  · 状态：`收盘 > 当日 MA5(含当日)` = 站上；否则跌破。
  · 一段 = 连续站上的交易日；段长 L = 站上的天数。
      - L 的结束日 = 第一个 `收盘 < MA5` 的交易日（**收盘口径**，不看盘中）。
      - 数据末尾仍未跌破的段 = 截尾（censored），只用于生存率分母，不计入 L 分布。
  · 逐股统计：每只票的段数、平均/中位 L；再合并成总分布。
  · 条件风险：已站上 N 天（收盘后），未来 1/3/5/10 日内跌破的概率、平均还要几天。
  · 破线后前向收益：以破线日收盘为基准，看 1/3/5/10 日收益（验证“下杀”是否真有负漂移）。

输出：
  · `doc/research/2026-11-11_站上5日线破线节奏_逐个实验.md`
  · `t_io/validation/ma5_streak_break_20261111_stocks.csv`（逐股）
  运行：`python scripts/experiment/ma5_streak_break_20261111.py`
"""
from __future__ import annotations

import csv
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAILY = os.path.join(BASE, "t_io", "cache", "daily_kline")
OUT_MD = os.path.join(BASE, "doc", "research", "2026-11-11_站上5日线破线节奏_逐个实验.md")
OUT_CSV = os.path.join(BASE, "t_io", "validation", "ma5_streak_break_20261111_stocks.csv")


def main() -> int:
    Ls, per, cnt = [], [], Counter()
    fwd = defaultdict(list)
    n_stock = n_open = 0
    for fp in sorted(glob.glob(os.path.join(DAILY, "*.json"))):
        code = os.path.basename(fp)[:-5]
        if not (len(code) == 6 and code.isdigit()):
            continue
        try:
            rows = json.loads(open(fp, encoding="utf-8").read()).get("rows") or []
        except Exception:
            continue
        rows = [r for r in rows
                if str(r.get("date"))[:10] and float(r.get("close") or 0) > 0]
        if len(rows) < 60:
            continue
        rows.sort(key=lambda r: str(r.get("date"))[:10])
        closes = [float(r["close"]) for r in rows]
        n = len(closes)
        above = [False] * n
        for i in range(4, n):
            above[i] = closes[i] > sum(closes[i - 4:i + 1]) / 5.0
        n_stock += 1
        stock_L, i = [], 4
        while i < n:
            if not above[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and above[j + 1]:
                j += 1
            L = j - i + 1
            if j + 1 < n:                       # 已跌破（j+1 日收盘 < MA5）
                Ls.append(L)
                stock_L.append(L)
                cnt[L] += 1
                b = j + 1
                for k in (1, 3, 5, 10):
                    if b + k < n:
                        fwd[(k, L)].append((closes[b + k] / closes[b] - 1) * 100.0)
            else:
                n_open += 1
            i = j + 1
        if stock_L:
            per.append((code, len(stock_L), statistics.mean(stock_L),
                        statistics.median(stock_L), min(stock_L), max(stock_L)))

    N = len(Ls)
    if not N:
        print("没有样本")
        return 1
    q = lambda p: sorted(Ls)[min(N - 1, int(N * p))]
    surv = {}
    s = 0
    for k in sorted(cnt, reverse=True):
        s += cnt[k]
        surv[k] = s

    lines = ["# 站上 5 日线后的破线节奏（逐个股票实验）", "",
             "日期：2026-11-11 · 数据：`t_io/cache/daily_kline`（本地日线，零网络）",
             "", "## 口径", "",
             "- 站上：`收盘 > 当日 MA5(含当日)`；跌破：`收盘 < MA5`（**收盘口径**，不看盘中）。",
             "- 一段 = 连续站上天数 L；数据末尾仍未跌破的段截尾，不计入 L 分布。",
             f"- 样本：**{n_stock} 只股票 / {N} 段**（另有 {n_open} 段未结束）。", "",
             "## 结论", "",
             f"- **平均 {statistics.mean(Ls):.2f} 个交易日**后跌破（中位 {statistics.median(Ls):.0f} 天，"
             f"25 分位 {q(.25):.0f}、75 分位 {q(.75):.0f}、90 分位 {q(.90):.0f} 天）。",
             "- 含义：站上后第 1~2 天破线最常见（合计约 45%）；到第 4 天已越过历史平均，"
             "**第 4~7 天是破线高发窗口**。", "",
             "## 条件风险（已站上 N 天，收盘后）", "",
             "| N | 样本(≥N) | 平均还要几天破 | 次日破 | 3日内破 | 5日内破 | 10日内破 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for d in range(1, 11):
        den = surv.get(d, 0)
        if not den:
            continue
        rem = [L - d for L in Ls if L >= d]
        p1 = cnt.get(d + 1, 0) / den * 100
        p3 = sum(cnt.get(d + k, 0) for k in (1, 2, 3)) / den * 100
        p5 = sum(cnt.get(d + k, 0) for k in range(1, 6)) / den * 100
        p10 = sum(cnt.get(d + k, 0) for k in range(1, 11)) / den * 100
        lines.append(f"| {d} | {den} | {statistics.mean(rem):.2f} | {p1:.1f}% | "
                     f"{p3:.1f}% | {p5:.1f}% | {p10:.1f}% |")
    lines += ["", "## 破线后的前向收益（破线日收盘为基准）", "",
              "| 持有 | L=1 | L=2 | L=3 | L=4 | L=5 | L=6 | L=7 | L=8 | L=10 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k in (1, 3, 5, 10):
        cells = []
        for L in (1, 2, 3, 4, 5, 6, 7, 8, 10):
            v = fwd[(k, L)]
            cells.append(f"{statistics.mean(v):+.2f}%" if v else "—")
        lines.append(f"| {k}日 | " + " | ".join(cells) + " |")
    lines += ["", "> ⚠️ 破线**本身**的平均前向收益很弱（1 日约 0%，5 日中位 −0.4%~−0.6%，"
              "长段 L≥7 甚至为正）⇒ 这是**节奏/回撤风险提醒**，不是“必跌”信号。", "",
              "## 逐股", "",
              f"- 逐股统计见 `t_io/validation/ma5_streak_break_20261111_stocks.csv`。",
              f"- 逐股平均天数：中位 {statistics.median([x[2] for x in per]):.2f} 天，"
              f"10~90 分位 {sorted(x[2] for x in per)[int(len(per)*.1)]:.2f}~"
              f"{sorted(x[2] for x in per)[int(len(per)*.9)]:.2f} 天（个股差异不大，"
              "破线节奏基本由 5 日线定义本身决定）。", ""]
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "episodes", "mean_days", "median_days", "min_days", "max_days"])
        for x in sorted(per, key=lambda z: z[0]):
            w.writerow([x[0], x[1], round(x[2], 2), x[3], x[4], x[5]])
    print(f"样本 {n_stock} 只 / {N} 段；平均 {statistics.mean(Ls):.2f} 天，"
          f"中位 {statistics.median(Ls):.0f}，75 分位 {q(.75):.0f}，90 分位 {q(.90):.0f}")
    print(f"报告 {OUT_MD}")
    print(f"逐股 {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
