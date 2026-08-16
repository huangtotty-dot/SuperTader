# -*- coding: utf-8 -*-
"""
w34_index_resonance.py — 指数共振周聚合（2026-08-14 新增，喂每周复盘）

读取每日 daily_review_{date}.json 中已算好的 resonance.groups（daily_review.py 与回测
共用同一套 settle 口径），把一周的"共振通过 vs 共振拦截 vs 数据缺失"命中率汇总成一张表，
供粘贴进 doc/每周复盘/ 周报，支撑共振阈值/口径调整决策。

用法：
    python w34_index_resonance.py --start 2026-08-10 --end 2026-08-14
    python w34_index_resonance.py            # 默认取最近 5 个交易日
输出：
    t_io/validation/weekly_review/w34_index_resonance.md + 控制台打印
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[3]  # 仓库根（本文件在 t_io/validation/weekly_review/ 下）
OUT_DIR = BASE / "t_io" / "validation" / "daily_review"


def _load_groups(date: str) -> dict:
    fp = OUT_DIR / f"daily_review_{date}.json"
    if not fp.exists():
        return None
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        return (d.get("resonance") or {}).get("groups") or {}
    except Exception:
        return {}


def _recent_trading_days(n: int = 5) -> list:
    days, cur = [], datetime.now().date()
    while len(days) < n:
        if cur.weekday() < 5:  # 周一~周五
            days.append(cur.strftime("%Y-%m-%d"))
        cur -= timedelta(days=1)
    return sorted(days)


def _merge_group(agg: dict, g: str, groups: dict) -> None:
    v = groups.get(g) or {}
    agg.setdefault(g, {"n": 0, "wins": 0, "fails": 0, "void": 0})
    agg[g]["n"] += int(v.get("n") or 0)
    agg[g]["wins"] += int(v.get("wins") or 0)
    agg[g]["fails"] += int(v.get("fails") or 0)
    agg[g]["void"] += int(v.get("void") or 0)


def main():
    ap = argparse.ArgumentParser(description="指数共振周聚合")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认最近5个交易日）")
    args = ap.parse_args()

    if args.start and args.end:
        days = []
        cur = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
        while cur <= end:
            if cur.weekday() < 5:
                days.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
    else:
        days = _recent_trading_days()

    agg, daily_rows = {}, []
    for d in days:
        g = _load_groups(d)
        if not g:
            continue
        daily_rows.append({"date": d, "groups": g})
        for grp in ("pass", "block", "data_missing"):
            _merge_group(agg, grp, g)

    if not daily_rows:
        print("近段无指数共振数据（daily_review 需先运行新版 daily_review.py 生成 resonance 段）")
        return

    def wr(a):
        return a["wins"] / (a["wins"] + a["fails"]) if (a["wins"] + a["fails"]) else None

    L = ["# 指数共振周聚合", ""]
    L.append("| 日期 | 共振通过(n/命中) | 共振拦截(n/命中) | 数据缺失(n) |")
    L.append("|---|---|---|---|")
    for r in daily_rows:
        g = r["groups"]
        p, b, m = g.get("pass", {}), g.get("block", {}), g.get("data_missing", {})
        L.append(f"| {r['date']} | {p.get('n',0)} / {wr(p):.0%} | {b.get('n',0)} / {wr(b):.0%} | {m.get('n',0)} |")
    L.append("")
    pw, bw = wr(agg.get("pass", {})), wr(agg.get("block", {}))
    L.append("## 合计")
    L.append(f"- 共振通过：**{agg.get('pass',{}).get('n',0)}** 条，命中率 **{pw:.0%}**"
             f"（wins {agg.get('pass',{}).get('wins',0)} / fails {agg.get('pass',{}).get('fails',0)}）")
    L.append(f"- 共振拦截：**{agg.get('block',{}).get('n',0)}** 条，命中率 **{bw:.0%}**"
             f"（wins {agg.get('block',{}).get('wins',0)} / fails {agg.get('block',{}).get('fails',0)}）")
    L.append(f"- 数据缺失拦截：**{agg.get('data_missing',{}).get('n',0)}** 条")
    if pw is not None and bw is not None:
        gap = pw - bw
        if gap > 0.05:
            verdict = "共振过滤有效，维持/收紧"
        elif gap < -0.05:
            verdict = "共振过滤有害，放宽或换口径"
        else:
            verdict = "暂无明显差异，继续积累样本"
        L.append(f"- **命中率差 = 通过 − 拦截 = {gap:+.1%}** → {verdict}")
    L.append("")
    L.append("> 口径：+0.5%/-0.4%/30tick（与 daily_review settle 一致）；样本少时结论仅供参考。")

    md = "\n".join(L)
    out_fp = BASE / "t_io" / "validation" / "weekly_review" / "w34_index_resonance.md"
    out_fp.write_text(md + "\n", encoding="utf-8")
    print(md)
    print(f"\n[OK] 已写入 {out_fp}")


if __name__ == "__main__":
    main()
