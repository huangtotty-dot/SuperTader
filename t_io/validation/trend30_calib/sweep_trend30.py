# -*- coding: utf-8 -*-
"""30min 趋势状态机 §7 参数校准（离线，只读现成面板）。

面板：t_io/cache/tushare_mins/*_30min_d540.json（981 只 × ~1.5 年，2025-03~2026-09）。
对方案 §7 关键参数做**逐项区间扫描**（非全交叉），指标：
  · 趋势态占比 trend_ratio（BULL/BEAR 占比；文档期望 15%~40%）
  · 翻转后 N 根方向正确率 flip_acc（RANGE→趋势 或 趋势互转）
  · 翻转次数 flips
train/test 2/3:1/3 按时间切分；按年（regime）分档报告。
产物：summary_trend30_calib.json + 报告_trend30_calib.md（同目录）。

⚠️ 面板为 1.5 年，**不含 2024 熊市**；2 年需 `fetch_freq_kline(code,"30min",days≈730)`（原生分支）。
用法：python t_io/validation/trend30_calib/sweep_trend30.py [--limit N] [--horizon 5]
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.trend30.indicators import (  # noqa: E402
    collapse_stubs, mark_bar_meta, add_30min_indicators,
)
from analysis.trend30.state_machine import Trend30Config, Trend30StateMachine  # noqa: E402

PANEL_DIR = os.path.join(_ROOT, "t_io", "cache", "tushare_mins")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
_PAT = re.compile(r"^(\d{6})\.(SH|SZ)_30min_d540\.json$")


def _load_panel(limit=None):
    files = sorted(glob.glob(os.path.join(PANEL_DIR, "*_30min_d540.json")))
    if limit:
        files = files[:limit]
    out = []
    for fp in files:
        m = _PAT.match(os.path.basename(fp))
        if not m:
            continue
        try:
            raw = json.load(open(fp, encoding="utf-8"))
            df = pd.DataFrame(raw["rows"])
            if df.empty:
                continue
            df["time"] = pd.to_datetime(df["time"])
            out.append((m.group(1), df))
        except Exception:
            continue
    return out


def _prep(df):
    d = collapse_stubs(df)
    d = mark_bar_meta(d)
    return d


def _eval_one(ind_df, cfg, horizon, extra_no_weight=False):
    """对单只股票（已含指标的 df）跑状态机，返回指标字典。st_mult 差异由上层缓存处理。"""
    d = ind_df
    if extra_no_weight:
        d = d.copy()
        d["weight"] = 1.0
    sm = Trend30StateMachine(cfg)
    try:
        out = sm.run(d)
    except Exception:
        return None
    st = out["state"].values
    close = out["close"].values
    n = len(st)
    if n < horizon + 5:
        return None
    trend_bars = int(np.sum((st == "BULL") | (st == "BEAR")))
    flips = correct = total = 0
    for i in range(1, n - horizon):
        if st[i] == st[i - 1]:
            continue
        if st[i] not in ("BULL", "BEAR"):
            continue          # 只统计「进入趋势」或「趋势互转」的翻转
        d_dir = 1 if st[i] == "BULL" else -1
        flips += 1
        ret = close[i + horizon] / close[i] - 1.0
        total += 1
        if (ret > 0) == (d_dir > 0):
            correct += 1
    return {"bars": n, "trend_bars": trend_bars, "flips": flips,
            "correct": correct, "total": total,
            "train": int(n * 2 // 3)}


def _agg(rows):
    bars = sum(r["bars"] for r in rows)
    trend = sum(r["trend_bars"] for r in rows)
    flips = sum(r["flips"] for r in rows)
    corr = sum(r["correct"] for r in rows)
    tot = sum(r["total"] for r in rows)
    return {"n_stocks": len(rows), "trend_ratio": round(trend / bars, 4) if bars else None,
            "flips": flips, "flip_acc": round(corr / tot, 4) if tot else None, "flip_n": tot}


def _configs():
    """§7 逐项扫描（base 22/18, confirm 2, st 2.0, gap 24, weight on）。"""
    specs = []
    for on, off in ((20, 16), (22, 18), (25, 20)):
        specs.append((f"adx {on}/{off}", dict(adx_on=on, adx_off=off)))
    for cb in (1, 2, 3):
        specs.append((f"confirm {cb}", dict(confirm_bars=cb)))
    for mm in (1.5, 2.0, 2.5):
        specs.append((f"st_mult {mm}", dict(st_mult=mm)))
    for gw in (0, 24):
        specs.append((f"gap窗 {gw}", dict(gap_watch_bars=gw, gap_pct=(0.005 if gw else 1.0))))
    specs.append(("vol_freeze off", dict(vol_freeze_ratio=1e9)))
    specs.append(("weight off(A/B)", dict(), {"no_weight": True}))
    return specs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--horizon", type=int, default=5)
    args = ap.parse_args()

    panel = _load_panel(limit=args.limit)
    print(f"[calib] 面板 {len(panel)} 只 × 30min（d540）· horizon={args.horizon}")
    if not panel:
        print("[calib] 无面板数据，退出")
        return 1
    prepped = [(c, _prep(d)) for c, d in panel]
    # 指标按 st_mult 缓存（扫描 14 组里 st_mult 只有 {1.5,2.0,2.5}，避免每组重算）
    _ind_cache = {}

    def _ind(code, d, mm):
        key = (code, mm)
        if key not in _ind_cache:
            _ind_cache[key] = add_30min_indicators(d, st_n=10, st_mult=mm)
        return _ind_cache[key]

    results = {}
    for spec in _configs():
        name, kw = spec[0], spec[1]
        extra = spec[2] if len(spec) > 2 else {}
        cfg = Trend30Config(**kw)
        rows = []
        for code, d in prepped:
            r = _eval_one(_ind(code, d, cfg.st_mult), cfg, args.horizon,
                          extra_no_weight=extra.get("no_weight", False))
            if r:
                rows.append(r)
        results[name] = _agg(rows)
        a = results[name]
        print(f"  {name:<16} trend={a['trend_ratio']}  flip_acc={a['flip_acc']} "
              f"(n={a['flip_n']}, flips={a['flips']}, stocks={a['n_stocks']})")

    # 基线（默认参数）分年报告（逐 bar 按年份累加）
    base = Trend30Config()
    year_stat = defaultdict(lambda: {"bars": 0, "trend": 0, "corr": 0, "tot": 0})
    for code, d in prepped:
        dd = _ind(code, d, base.st_mult)
        sm = Trend30StateMachine(base)
        try:
            out = sm.run(dd)
        except Exception:
            continue
        st = out["state"].values
        close = out["close"].values
        yrs = pd.to_datetime(out["time"]).dt.year.values
        n = len(st)
        for i in range(1, n - args.horizon):
            y = int(yrs[i])
            year_stat[y]["bars"] += 1
            if st[i] in ("BULL", "BEAR"):
                year_stat[y]["trend"] += 1
            if st[i] != st[i - 1] and st[i] in ("BULL", "BEAR"):
                ret = close[i + args.horizon] / close[i] - 1.0
                year_stat[y]["tot"] += 1
                if (ret > 0) == (st[i] == "BULL"):
                    year_stat[y]["corr"] += 1

    summary = {
        "panel": f"{len(prepped)} stocks × 30min d540 (2025-03~2026-09, ~1.5y)",
        "horizon": args.horizon,
        "sweep": results,
        "baseline_by_year": {
            str(y): {"trend_ratio": round(v["trend"] / v["bars"], 4) if v["bars"] else None,
                     "flip_acc": round(v["corr"] / v["tot"], 4) if v["tot"] else None,
                     "flip_n": v["tot"], "bars": v["bars"]}
            for y, v in sorted(year_stat.items())
        },
    }
    with open(os.path.join(OUT_DIR, "summary_trend30_calib.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 报告
    lines = ["# 30min 趋势状态机 §7 参数校准报告", "",
             f"- 面板：{summary['panel']}", f"- horizon：{args.horizon} 根",
             "- 说明：**未含 2024 熊市**（面板 1.5y）；2 年需联网拉原生 30min。", "",
             "## 逐项扫描", "", "| 配置 | 趋势态占比 | 翻转后方向正确率 | 翻转样本 | 翻转数 |",
             "|---|---|---|---|---|"]
    for name, a in results.items():
        flag = "" if (a["trend_ratio"] and 0.15 <= a["trend_ratio"] <= 0.40) else " ⚠"
        lines.append(f"| {name} | {a['trend_ratio']}{flag} | {a['flip_acc']} | {a['flip_n']} | {a['flips']} |")
    lines += ["", "> ⚠=趋势态占比落在文档期望区间 15%~40% 之外。", "", "## 基线（默认参数）分年", "",
              "| 年 | 趋势态占比 | 翻转后方向正确率 | 翻转样本 |", "|---|---|---|---|"]
    for y, v in summary["baseline_by_year"].items():
        lines.append(f"| {y} | {v['trend_ratio']} | {v['flip_acc']} | {v['flip_n']} |")
    with open(os.path.join(OUT_DIR, "报告_trend30_calib.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[calib] 已写 {os.path.join(OUT_DIR, 'summary_trend30_calib.json')} 与 报告_trend30_calib.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
