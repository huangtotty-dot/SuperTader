# -*- coding: utf-8 -*-
"""
validate_divergence_daily.py — 日线顶背离(持仓体检口径) vs 驻顶 有效性验证（2026-08-19）

背景：t_gui 持仓体检（load_ob_analysis）用日线四指标复合顶背离（MACD/RSI/KDJ/量价，
      近60日窗口、≥2 指标 → "严重顶背离" 飞书告警）。本次验证该告警逻辑是否有效，
      复用分钟线验证的"极值后市确认"方法论，不直接套用分钟线结论（周期不同）。

X = 日线顶背离事件：局部高点(2根邻域)与前一高点(间距≤60日)比较，
    价创新高时各指标未创新高 → 记 count(0~4)。与 t_gui 判定同口径。
Y = 驻顶：高点后 K=5 交易日内 max(high)<H 且 min(close)<=H*(1-5%)。
    尾部不足 K 交易日者剔除（unconfirmed）。

输出：summary_divergence_daily.json + divergence_验证报告_daily.md（同目录）。
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent.parent  # e:/superTrader
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

OUT_DIR = BASE / "t_io" / "validation" / "w35_divergence"
WATCHLIST_FILE = BASE / "watchlist_buy.json"

K_DAYS = 5        # 驻顶确认窗口（交易日）
R_PCT = 0.05      # 回落阈值
MAX_GAP = 60      # 相邻高点最大间距（对应 t_gui 近60日窗口）


def _load_watchlist():
    d = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    stocks = d.get("stocks", {})
    return {k: v.get("name", k) for k, v in stocks.items()
            if isinstance(v, dict) and v.get("status") in ("monitoring", "signal")
            and not k.startswith("_")}


def _indicators(df):
    """复现 t_gui 口径：dif(EMA12-26)、rsi(14)、kdj(9,3,3)。返回 numpy 数组。"""
    c = df["close"].astype(float)
    dif = (c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()).values
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = (100 - 100 / (1 + rs)).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    n = len(c)
    k_arr, d_arr, j_arr = [50.0], [50.0], [50.0]
    for i in range(1, n):
        hh = max(highs[max(0, i - 8):i + 1])
        ll = min(lows[max(0, i - 8):i + 1])
        rsv = (c.iloc[i] - ll) / (hh - ll) * 100 if hh != ll else 50
        k = 2 / 3 * k_arr[-1] + 1 / 3 * rsv
        dd = 2 / 3 * d_arr[-1] + 1 / 3 * k
        k_arr.append(k); d_arr.append(dd); j_arr.append(3 * k - 2 * dd)
    return dif, rsi, np.array(j_arr)


def _per_stock(df):
    n = len(df)
    highs = df["high"].astype(float).values
    closes = df["close"].astype(float).values
    volumes = df["volume"].astype(float).values
    dif, rsi, j_arr = _indicators(df)
    dates = df["date"].astype(str).values

    # 局部高点（t_gui 口径：2根邻域）
    peaks = [i for i in range(2, n - 2)
             if highs[i] >= highs[i - 1] and highs[i] >= highs[i - 2]
             and highs[i] >= highs[i + 1] and highs[i] >= highs[i + 2]]

    events = []      # {index, count, date, high}
    top_idx = set()
    unconfirmed = set()
    for k in range(1, len(peaks)):
        p2, p1 = peaks[k - 1], peaks[k]
        if p1 - p2 > MAX_GAP:
            continue
        if not (highs[p1] > highs[p2]):  # 价未创新高不算顶背离事件
            continue
        div = {}
        if dif[p1] is not None and dif[p2] is not None and dif[p1] < dif[p2]:
            div["macd"] = True
        if rsi[p1] is not None and rsi[p2] is not None and rsi[p1] < rsi[p2]:
            div["rsi"] = True
        if j_arr[p1] < j_arr[p2]:
            div["kdj"] = True
        if volumes[p1] < volumes[p2] * 0.9:
            div["vol"] = True
        events.append({"index": int(p1), "date": dates[p1], "high": float(highs[p1]),
                       "count": sum(1 for v in div.values())})

    # 驻顶判定：高点后 K 交易日未创新高且回落≥R%
    for p in peaks:
        if p + K_DAYS >= n:
            unconfirmed.add(p)
            continue
        win_h = highs[p + 1:p + K_DAYS + 1]
        win_c = closes[p + 1:p + K_DAYS + 1]
        if len(win_h) == K_DAYS and max(win_h) < highs[p] and min(win_c) <= highs[p] * (1 - R_PCT):
            top_idx.add(p)

    # 事件后市收益
    fwd_ret = {}
    for e in events:
        i = e["index"]
        if i + K_DAYS < n:
            fwd_ret[i] = closes[i + K_DAYS] / highs[i] - 1

    return {"events": events, "top_idx": top_idx, "unconfirmed": unconfirmed,
            "peaks": peaks, "fwd_ret": fwd_ret, "n": n}


def _agg_all(rows):
    agg = {"counts": {0: [0, 0], 1: [0, 0], 2: [0, 0], 3: [0, 0], 4: [0, 0]},  # [hit, total] 按count
           "base": [0, 0], "fwd": defaultdict(list)}
    for code, r in rows.items():
        if r is None:
            continue
        ev_valid = [e for e in r["events"] if e["index"] not in r["unconfirmed"]]
        for e in ev_valid:
            c = min(e["count"], 4)
            agg["counts"][c][1] += 1
            if e["index"] in r["top_idx"]:
                agg["counts"][c][0] += 1
        # 基线：价创新高但 0 指标背离的相邻高点（自然基线）
        # fwd 收益按 count 分组
        for i, ret in r["fwd_ret"].items():
            ev = next((e for e in r["events"] if e["index"] == i), None)
            if ev is not None:
                agg["fwd"][ev["count"]].append(ret)
    out = {}
    for c in range(5):
        hit, tot = agg["counts"][c]
        out[c] = {"hit": hit, "total": tot, "rate": (hit / tot) if tot else None}
    out["base_rate"] = out[0]["rate"]
    out["fwd"] = {c: round(float(np.mean(v)), 4) for c, v in agg["fwd"].items() if v}
    return out


def _render_md(rows, agg):
    L = []
    L.append("# 日线顶背离(持仓体检口径) vs 驻顶 验证报告(2026-08-19)\n")
    L.append("> 方法：极值后市确认 | 数据：腾讯日线 qfq（含当日）| 样本：37 只候选股")
    L.append(f"> X=日线顶背离(count:MACD/RSI/KDJ/量价满足数，近60日窗口、2根邻域，与 t_gui 同口径)")
    L.append(f"> Y=驻顶=高点后 {K_DAYS} 交易日未创新高且回落≥{R_PCT*100:.0f}%；尾部不足判定窗口者剔除\n")
    L.append("## 1. 总指标(按背离指标数 count)")
    L.append("| count | 事件数 | 命中驻顶 | 命中率 | 后市5日均收益 |")
    L.append("|---|---|---|---|---|")
    for c in range(5):
        a = agg[c]
        f = agg["fwd"].get(c)
        label = {0: "0(价新高无指标背离=基线)", 1: "1", 2: "2(严重告警阈值)", 3: "3", 4: "4"}[c]
        L.append(f"| {label} | {a['total']} | {a['hit']} | {_pct(a['rate'])} | {_pct2(f)} |")
    L.append("")
    L.append("## 2. 分股明细")
    L.append("| 代码 | 名称 | count≥2事件 | 命中 | count=1事件 | 命中 |")
    L.append("|---|---|---|---|---|---|")
    for code, r in rows.items():
        if r is None:
            continue
        ev = [e for e in r["events"] if e["index"] not in r["unconfirmed"]]
        c2 = [e for e in ev if e["count"] >= 2]
        c1 = [e for e in ev if e["count"] == 1]
        h2 = sum(1 for e in c2 if e["index"] in r["top_idx"])
        h1 = sum(1 for e in c1 if e["index"] in r["top_idx"])
        L.append(f"| {code} | {rows[code]['name']} | {len(c2)} | {h2} | {len(c1)} | {h1} |")
    L.append("\n## 3. 样本概况")
    L.append("- 数据：腾讯日线 qfq，约 3 年（含当日 forming bar）；指标口径与 t_gui load_ob_analysis 一致")
    L.append("- 幸存者偏差：37 只候选股为人工挑选池，结论仅池内有效")
    L.append("- 尾部剔除：距数据末尾不足 5 交易日的峰值已剔除")
    L.append("## 4. 结论")
    a2, a1, a0 = agg[2], agg[1], agg[0]
    for label, a, base in (("count≥2(严重告警)", a2, a0), ("count=1", a1, a0)):
        if a["rate"] is not None and base["rate"] is not None:
            d = a["rate"] - base["rate"]
            v = "有区分度" if d > 0.05 else ("无区分度" if d <= 0 else "区分度弱")
            L.append(f"- {label}：命中 {_pct(a['rate'])} vs 基线(0指标) {_pct(base['rate'])} → {v}（样本 {a['total']}）")
    L.append(f"- count≥2 后市5日均收益 {_pct2(agg['fwd'].get(2))} vs count=0 {_pct2(agg['fwd'].get(0))}："
             "负值说明'严重顶背离后确偏跌'")
    return "\n".join(L)


def _pct(x):
    return f"{x*100:.1f}%" if x is not None else "—"


def _pct2(x):
    return f"{x*100:+.2f}%" if x is not None else "—"


def main():
    import argparse
    ap = argparse.ArgumentParser(description="日线顶背离 vs 驻顶 验证")
    ap.add_argument("--codes", default=None, help="逗号分隔指定代码（默认 watchlist 全部）")
    args = ap.parse_args()
    import position_builder as pb
    stocks = _load_watchlist()
    if args.codes:
        stocks = {c: stocks[c] for c in args.codes.split(",") if c in stocks}
    print(f"候选股 {len(stocks)} 只")
    rows = {}
    for code, name in stocks.items():
        try:
            df = pb.fetch_daily_kline(code)
            if df is None or df.empty or len(df) < 80:
                rows[code] = None
                continue
            df = df.reset_index(drop=True)
            rows[code] = {"name": name, **_per_stock(df)}
        except Exception as e:
            rows[code] = None
            print(f"  {code} 失败: {e}")
    agg = _agg_all(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"method": "极值后市确认(日线持仓体检口径)", "params": {"K_DAYS": K_DAYS,
               "R_PCT": R_PCT, "MAX_GAP": MAX_GAP}, "stocks": len(stocks), "agg": agg,
               "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    (OUT_DIR / "summary_divergence_daily.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "divergence_验证报告_daily.md").write_text(
        _render_md(rows, agg), encoding="utf-8")
    print("已写:", OUT_DIR / "summary_divergence_daily.json")


if __name__ == "__main__":
    main()
