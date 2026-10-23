# -*- coding: utf-8 -*-
"""「箱体突破」信号的前向收益实验（2026-10-23 owner：「准确度很差，深度研究一下」）。

## 要回答的三件事

1. **现在的信号到底有没有正向超额**？（口径原样复刻 `t_gui.Api._breakout_probe_one`：
   `昨收 ≤ 箱体上沿 < 今收` 且幅度 ∈ [0.3%, 8%]，上沿取 as-of 上一交易日的 rel==0 箱体）
2. 如果没有/很弱，**错在哪**：幅度带？量能？箱体宽度/时长？当日形态（上影线）？
3. 哪个子集（如果有）**显著更强**，可以拿来做过滤。

## 口径（刻意做成"可实现"）

* 进场：**次日开盘**（信号是收盘后才成立的，不可能按信号日收盘价成交）
* 出场：T+1 / T+3 / T+5 的**收盘**（相对次日开盘）
* 超额：减去**同一天全样本的均值**（横截面去市场，避免"牛市里什么都赚"）
* 显著性：按**日期聚类**的 t（同一天的多只票不独立）

## 用法

    python scripts/experiment/box_breakout_forward_20261023.py [--stocks 1200] [--days 250] [--procs 3]
    → 打印分组表 + 写 `doc/research/2026-10-23_箱体突破_前向收益实验.md`
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

DAILY = BASE / "t_io" / "cache" / "daily_kline"
OUT_MD = BASE / "doc" / "research" / "2026-10-23_箱体突破_前向收益实验.md"


def _load(code):
    try:
        rows = json.loads((DAILY / f"{code}.json").read_text(encoding="utf-8")).get("rows") or []
    except Exception:
        return None
    if len(rows) < 120:
        return None
    return rows


def _scan_one(code, lookback: int = 260):
    """单只：逐日复刻突破判定 + 记录特征与前向收益。返回样本列表（dict）。"""
    import pandas as pd
    import t_gui
    api = t_gui.Api()
    rows = _load(code)
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["date"] = df["date"].astype(str).str[:10]
    n = len(df)
    closes = df["close"].to_numpy(float)
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    vols = df["volume"].to_numpy(float)
    ma20 = pd.Series(closes).rolling(20).mean().to_numpy()
    ma60 = pd.Series(closes).rolling(60).mean().to_numpy()
    vol20 = pd.Series(vols).rolling(20).mean().to_numpy()

    out = []
    # 需要：as-of 昨日 ≥30 根（箱体） + 前向 6 根；只看最近 lookback 个交易日（控制耗时）
    start = max(35, n - int(lookback) - 6)
    for i in range(start, n - 6):
        try:
            boxes = api._detect_boxes(df.iloc[:i])
        except Exception:
            continue
        box = next((b for b in boxes if b.get("rel") == 0 and b.get("high")), None)
        if not box:
            continue
        top = float(box["high"])
        bot = float(box.get("low") or 0.0)
        prev_close, cur = closes[i - 1], closes[i]
        entry = opens[i + 1] if i + 1 < n else None
        if not entry or entry <= 0:
            continue
        r = {k: (closes[i + k] / entry - 1.0) if i + k < n else None for k in (1, 3, 5)}
        # 突破判定（与生产同口径）
        sig = bool(prev_close <= top < cur)
        pct_above = (cur - top) / top * 100.0 if top > 0 else None
        sig = bool(sig and pct_above is not None and 0.3 <= pct_above <= 8.0)
        chg = (cur / prev_close - 1.0) * 100.0 if prev_close else None
        vr = (vols[i] / vol20[i]) if vol20[i] and not np.isnan(vol20[i]) else None
        ushadow = ((highs[i] - max(opens[i], cur)) / max(cur, 1e-9) * 100.0)
        out.append({
            "code": code, "date": str(df["date"].iloc[i]), "sig": sig,
            "pct_above": round(pct_above, 3) if pct_above is not None else None,
            "chg": round(chg, 3) if chg is not None else None,
            "vr": round(float(vr), 3) if vr else None,
            "ushadow": round(float(ushadow), 3),
            "box_width": round((top - bot) / bot * 100.0, 2) if bot > 0 else None,
            "above_ma20": bool(ma20[i] == ma20[i] and cur > ma20[i]),
            "above_ma60": bool(ma60[i] == ma60[i] and cur > ma60[i]),
            "r1": r[1], "r3": r[3], "r5": r[5],
        })
    return out


def _worker(args):
    code, lookback = args
    try:
        return _scan_one(code, lookback)
    except Exception as e:      # 单只失败不影响整体
        return [{"code": code, "error": f"{type(e).__name__}: {str(e)[:80]}"}]


def _t_by_date(df, col):
    """按日期聚类的 t：先按日求均值，再对日均值序列做单样本 t。"""
    g = df.groupby("date")[col].mean().dropna()
    if len(g) < 5:
        return float("nan"), len(g)
    sd = g.std(ddof=1)
    if not sd:
        return float("nan"), len(g)
    return float(g.mean() / (sd / np.sqrt(len(g)))), len(g)


def _table(df, name, conds):
    rows = []
    for label, fn in conds:
        sub = df[fn(df)]
        if sub.empty:
            continue
        r = {"组": label, "n": len(sub)}
        for c in ("x1", "x3", "x5"):
            r[c] = round(sub[c].mean() * 100, 3) if c in sub else None
        for c in ("x1", "x3", "x5"):
            t, nd = _t_by_date(sub, c)
            r[c + "_t"] = None if t != t else round(t, 2)
        r["胜率5"] = round((sub["x5"] > 0).mean() * 100, 1) if "x5" in sub else None
        rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=1200)
    ap.add_argument("--procs", type=int, default=3)
    ap.add_argument("--days", type=int, default=250)
    a = ap.parse_args()

    codes = sorted(p.stem for p in DAILY.glob("*.json")
                   if p.stem.isdigit() and len(p.stem) == 6)
    if a.stocks and len(codes) > a.stocks:
        step = len(codes) / a.stocks
        codes = [codes[int(i * step)] for i in range(a.stocks)]

    print(f"样本股票 {len(codes)} 只，进程 {a.procs}", flush=True)
    from concurrent.futures import ProcessPoolExecutor
    all_rows = []
    with ProcessPoolExecutor(max_workers=a.procs) as ex:
        _jobs = [(c, a.days) for c in codes]
        for i, part in enumerate(ex.map(_worker, _jobs, chunksize=8)):
            all_rows.extend(part)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(codes)} 只，样本 {len(all_rows)}", flush=True)
    df = __import__("pandas").DataFrame([r for r in all_rows if "error" not in r])
    # 只保留最近 N 个交易日
    days = sorted(df["date"].unique())[-a.days:]
    df = df[df["date"].isin(days)].copy()

    # 横截面去市场：同一天减均值
    for c in ("r1", "r3", "r5"):
        df["x" + c[1:]] = df[c] - df.groupby("date")[c].transform("mean")

    base = df
    sig = df[df["sig"]]
    print(f"\n样本 {len(base)} 股票·日（{len(days)} 天），突破信号 {len(sig)} 条 "
          f"（{len(sig) / max(1, len(base)) * 100:.2f}%）", flush=True)

    def _summ(d, name):
        out = {"组": name, "n": len(d)}
        for c in ("x1", "x3", "x5"):
            out[c] = round(d[c].mean() * 100, 3) if len(d) else None
            t, nd = _t_by_date(d, c)
            out[c + "_t"] = None if t != t else round(t, 2)
        out["胜率5"] = round((d["x5"] > 0).mean() * 100, 1) if len(d) else None
        return out

    lines = []
    def W(s=""):
        print(s, flush=True)
        lines.append(s)

    W("## 一、信号 vs 全样本")
    W("")
    W("| 组 | n | T+1 | T+3 | T+5 | t(T+5) | 胜率(T+5) |")
    W("|---|---|---|---|---|---|---|")
    for row in [_summ(base, "全样本（含市场漂移已横截面扣除）"), _summ(sig, "箱体突破信号")]:
        W(f"| {row['组']} | {row['n']} | {row['x1']}% | {row['x3']}% | {row['x5']}% | "
          f"{row['x5_t']} | {row['胜率5']}% |")

    W("")
    W("## 二、按「突破幅度」分档（现行 0.3~8%）")
    W("")
    W("| 幅度带 | n | T+1 | T+3 | T+5 | t(T+5) | 胜率(T+5) |")
    W("|---|---|---|---|---|---|---|")
    bands = [(0.3, 1), (1, 3), (3, 5), (5, 8)]
    for lo, hi in bands:
        sub = sig[(sig["pct_above"] >= lo) & (sig["pct_above"] < hi)]
        r = _summ(sub, f"{lo}~{hi}%")
        W(f"| {lo}~{hi}% | {r['n']} | {r['x1']}% | {r['x3']}% | {r['x5']}% | {r['x5_t']} | {r['胜率5']}% |")

    W("")
    W("## 三、按量能（当日量 / 20 日均量）分档")
    W("")
    W("| 量比 | n | T+5 | t(T+5) | 胜率(T+5) |")
    W("|---|---|---|---|---|")
    for lo, hi in [(0, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 99)]:
        sub = sig[(sig["vr"] >= lo) & (sig["vr"] < hi)]
        r = _summ(sub, f"{lo}~{hi}")
        W(f"| {lo}~{hi} | {r['n']} | {r['x5']}% | {r['x5_t']} | {r['胜率5']}% |")

    W("")
    W("## 四、按箱体宽度 / 当日形态 / 均线位置")
    W("")
    W("| 条件 | n | T+5 | t(T+5) | 胜率(T+5) |")
    W("|---|---|---|---|---|")
    checks = [
        ("箱体宽 <8%", lambda d: (d["box_width"] > 0) & (d["box_width"] < 8)),
        ("箱体宽 8~15%", lambda d: (d["box_width"] >= 8) & (d["box_width"] < 15)),
        ("箱体宽 ≥15%", lambda d: d["box_width"] >= 15),
        ("当日涨幅 ≤3%（慢突破）", lambda d: d["chg"] <= 3),
        ("当日涨幅 3~7%", lambda d: (d["chg"] > 3) & (d["chg"] <= 7)),
        ("当日涨幅 >7%（近涨停）", lambda d: d["chg"] > 7),
        ("上影线 ≤1%（收在最高附近）", lambda d: d["ushadow"] <= 1),
        ("上影线 >2.5%（冲高回落）", lambda d: d["ushadow"] > 2.5),
        ("站上 MA20", lambda d: d["above_ma20"]),
        ("未站上 MA20", lambda d: ~d["above_ma20"]),
        ("站上 MA60", lambda d: d["above_ma60"]),
        ("未站上 MA60", lambda d: ~d["above_ma60"]),
    ]
    for label, fn in checks:
        sub = sig[fn(sig)]
        r = _summ(sub, label)
        W(f"| {label} | {r['n']} | {r['x5']}% | {r['x5_t']} | {r['胜率5']}% |")

    W("")
    W("## 五、组合：慢突破 + 放量 + 站上 MA20")
    W("")
    W("| 组合 | n | T+1 | T+3 | T+5 | t(T+5) | 胜率(T+5) |")
    W("|---|---|---|---|---|---|---|")
    combos = [
        ("幅度1~5% 且 量比≥1.5", lambda d: d["pct_above"].between(1, 5) & (d["vr"] >= 1.5)),
        ("幅度1~5% 且 站上MA20", lambda d: d["pct_above"].between(1, 5) & d["above_ma20"]),
        ("幅度1~5% 且 量比≥1.5 且 站上MA20",
         lambda d: d["pct_above"].between(1, 5) & (d["vr"] >= 1.5) & d["above_ma20"]),
        ("幅度1~5% 且 上影线≤1%",
         lambda d: d["pct_above"].between(1, 5) & (d["ushadow"] <= 1)),
    ]
    for label, fn in combos:
        sub = sig[fn(sig)]
        r = _summ(sub, label)
        W(f"| {label} | {r['n']} | {r['x1']}% | {r['x3']}% | {r['x5']}% | {r['x5_t']} | {r['胜率5']}% |")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n写入 {OUT_MD}")
    # 原始样本落盘，便于二次分析
    df.to_parquet(BASE / "t_io" / "cache" / "box_breakout_study.parquet", index=False)


if __name__ == "__main__":
    main()
