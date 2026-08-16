# -*- coding: utf-8 -*-
"""
w34_entry_timing_experiment.py — 建仓/加仓时机判定实验（2026-08-15 新增）

目标：用历史数据验证"何时建仓/加仓"——用户判断：多头 + 风险已释放时进场可增收益，
否则降频。本实验对候选股逐日计算【大势代理 + 个股技术时机特征】与前向收益(1/3/5日)，
分桶/组合找出哪些时机特征预测正收益，形成 go/no-go 判定。

数据（全部无未来函数）：
  - 大势代理：上证指数(sh000001)日线 → 站上MA20/MA60、20日动量 → 多头/空头/震荡
  - 个股日线：腾讯 qfq（fetch_daily_kline 本地缓存）→ 多头结构/回踩MA20/风险释放/超卖/量能
  - 前向收益：t+1/t+3/t+5 收盘相对 t 收盘

用法：
    python t_io/validation/w34_entry_timing_experiment.py
    python t_io/validation/w34_entry_timing_experiment.py --start 2025-08-15 --end 2026-08-15
输出：t_io/replay/entry_timing_{start}_{end}/  features.csv + report.md
"""
import argparse
import csv
import json
import sys
import time
import urllib.request as _ur
import os as _os
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from position_builder import fetch_daily_kline  # noqa: E402
from w34_resonance_backtest_year import _trading_days, WATCHLIST_FILE, HOLDINGS_FILE  # noqa: E402

INDEX_CACHE = BASE / "t_io" / "cache" / "daily_kline" / "index_sh000001.json"


def _fetch_index_daily():
    """上证指数日线（腾讯 qfq，缓存）。返回 {date: close} 升序列表。"""
    if INDEX_CACHE.exists():
        try:
            d = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
            if d.get("rows"):
                return d["rows"]
        except Exception:
            pass
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]:
        _os.environ.pop(_k, None)
    _os.environ["NO_PROXY"] = "*"
    url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,800,qfq"
    req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"})
    raw = _ur.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
    data = json.loads(raw)
    kline = data.get("data", {}).get("sh000001", {}).get("day") or \
            data.get("data", {}).get("sh000001", {}).get("qfqday") or []
    rows = [{"date": i[0], "close": float(i[2])} for i in kline if len(i) >= 3]
    INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_CACHE.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    return rows


def _stock_ma(s, n):
    return s.rolling(n, min_periods=n).mean()


def _build_features(df, idx_close_by_date, date_str):
    """对某只股票截至 date_str 的日线计算时机特征（无未来函数）+ 前向收益。返回 dict 或 None。"""
    sub = df[df["date"] <= date_str]
    if len(sub) < 61:
        return None
    i = len(sub) - 1
    if i + 10 >= len(df):  # 需要 t+10 数据
        return None
    c = sub["close"].astype(float)
    h = sub["high"].astype(float)
    v = sub["volume"].astype(float)
    price = float(c.iloc[-1])
    ma20 = float(_stock_ma(c, 20).iloc[-1])
    ma60 = float(_stock_ma(c, 60).iloc[-1])
    rec_high = float(h.tail(20).max())
    # RSI(14)
    delta = c.diff()
    g = delta.clip(lower=0).rolling(14).mean()
    l = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = float((100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1]) if l.iloc[-1] and l.iloc[-1] > 0 else 50.0
    # MACD 金叉（近5日）
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False).mean()
    cross = ((dif > dea) & (dif.shift(1) <= dea.shift(1))).tail(5).any()
    # 量比
    vol_ma5 = float(v.tail(5).mean())
    vol_today = float(v.iloc[-1])
    # 大势（用 date_str 当日或之前最近指数收盘）
    idx_cl = None
    for d in sorted(idx_close_by_date.keys(), reverse=True):
        if d <= date_str:
            idx_cl = idx_close_by_date[d]
            break
    f = {
        "date": date_str, "code": None, "price": round(price, 3),
        "trend_multihead": bool(price > ma20 and price > ma60),
        "pullback_ma20": abs(price / ma20 - 1) <= 0.02 if ma20 > 0 else False,
        "pullback_ma60": abs(price / ma60 - 1) <= 0.03 if ma60 > 0 else False,
        "risk_released": bool(price <= rec_high * 0.95) if rec_high > 0 else False,
        "drawdown": round(price / rec_high - 1, 4) if rec_high > 0 else 0.0,
        "rsi": round(rsi, 1),
        "macd_golden_5d": bool(cross),
        "vol_shrink": bool(vol_today < vol_ma5 * 0.8) if vol_ma5 > 0 else False,
        "above_ma20": bool(price > ma20),
        "above_ma60": bool(price > ma60),
        "idx_close": idx_cl,
        "fwd1": round(float(df.iloc[i + 1]["close"]) / price - 1, 5),
        "fwd3": round(float(df.iloc[i + 3]["close"]) / price - 1, 5),
        "fwd5": round(float(df.iloc[i + 5]["close"]) / price - 1, 5),
        "fwd7": round(float(df.iloc[i + 7]["close"]) / price - 1, 5),
        "fwd10": round(float(df.iloc[i + 10]["close"]) / price - 1, 5),
    }
    return f


def main():
    ap = argparse.ArgumentParser(description="建仓/加仓时机判定实验")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--codes", nargs="*", default=None)
    args = ap.parse_args()

    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    if args.codes:
        codes = list(args.codes)
    else:
        wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")) if WATCHLIST_FILE.exists() else {}
        codes = [c for c, v in (wl.get("stocks", {}) or {}).items()
                 if isinstance(v, dict) and not c.startswith("_example") and v.get("status") in ("monitoring", "signal")]
    codes = sorted(set(codes))
    days = _trading_days(start, end)
    print(f"[universe] {len(codes)} 候选 ｜ {start}~{end}（{len(days)} 工作日）")

    # 大势代理
    idx_rows = _fetch_index_daily()
    idx_close_by_date = {r["date"]: r["close"] for r in idx_rows}
    idx_df = pd.DataFrame(idx_rows)
    idx_df["close"] = pd.to_numeric(idx_df["close"])
    idx_df["ma20"] = _stock_ma(idx_df["close"], 20)
    idx_df["ma60"] = _stock_ma(idx_df["close"], 60)
    idx_df["mom20"] = idx_df["close"] / idx_df["close"].shift(20) - 1
    print(f"[index] 上证日线 {len(idx_rows)} 根")

    rows = []
    for code in codes:
        df = fetch_daily_kline(code)
        if df.empty or len(df) < 66:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = df["date"].astype(str)
        for day in days:
            f = _build_features(df, idx_close_by_date, day)
            if f is None:
                continue
            f["code"] = code
            # 大势特征
            m20 = idx_df.loc[idx_df["date"] == day, "ma20"]
            m60 = idx_df.loc[idx_df["date"] == day, "ma60"]
            icl = idx_close_by_date.get(day)
            f["idx_above_ma20"] = bool(icl and not m20.empty and icl > float(m20.iloc[0]))
            f["idx_above_ma60"] = bool(icl and not m60.empty and icl > float(m60.iloc[0]))
            rows.append(f)
        print(f"  {code} 特征行 {sum(1 for r in rows if r['code']==code)}")

    # 保存
    out_dir = BASE / "t_io" / "replay" / f"entry_timing_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if rows:
        pd.DataFrame(rows).to_csv(out_dir / "features.csv", index=False, encoding="utf-8-sig")
    print(f"特征总行数 {len(rows)} ｜ 输出 → {out_dir}")

    if not rows:
        return

    # ── 分桶报告 ──
    def _mean_fwd(sub, key):
        vals = [r[key] for r in sub if r.get(key) is not None]
        return (len(vals), round(float(np.mean(vals)), 4) if vals else None)

    lines = [f"# 建仓/加仓时机判定（{start}~{end}，{len(rows)} 行）", "",
             "口径：每候选股逐日，前向收益=收盘相对当日收盘；大势=上证指数日线。", ""]
    print(f"\n{'特征':24s} {'条件':20s} {'n':>7} {'fwd1均值':>9} {'fwd3均值':>9} {'fwd5均值':>9}")
    print("-" * 78)

    def _bucket(label, key, specs, fmt=lambda x: x):
        lines.append(f"## {label}")
        lines.append("| 分桶 | n | fwd1 | fwd3 | fwd5 |")
        lines.append("|---|---|---|---|---|")
        for lo, hi, name in specs:
            sub = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
            n, m1 = _mean_fwd(sub, "fwd1")
            _, m3 = _mean_fwd(sub, "fwd3")
            _, m5 = _mean_fwd(sub, "fwd5")
            f = lambda x: f"{x:.2%}" if x is not None else "—"  # noqa: E731
            print(f"{label:24s} {name:20s} {n:7d} {f(m1):>9} {f(m3):>9} {f(m5):>9}")
            lines.append(f"| {name} | {n} | {f(m1)} | {f(m3)} | {f(m5)} |")
        lines.append("")

    _bucket("大势(指数站上MA20)", "idx_above_ma20",
            [(False, True, "否(空头)"), (True, 2, "是(多头)")], lambda b: b)
    _bucket("个股多头结构(价>MA20&MA60)", "trend_multihead",
            [(False, True, "否"), (True, 2, "是")])
    _bucket("回踩MA20(±2%)", "pullback_ma20", [(False, True, "否"), (True, 2, "是")])
    _bucket("风险释放(价≤20日高×0.95)", "risk_released", [(False, True, "否"), (True, 2, "是")])
    _bucket("回撤幅度(drawdown)", "drawdown",
            [(-9, -0.10, "<-10%深回撤"), (-0.10, -0.03, "-10~-3%"), (-0.03, 0, "-3~0%"), (0, 9, ">0%新高")])
    _bucket("RSI", "rsi", [(-9, 30, "<30超卖"), (30, 50, "30~50"), (50, 70, "50~70"), (70, 100, ">70超买")])
    _bucket("MACD金叉近5日", "macd_golden_5d", [(False, True, "否"), (True, 2, "是")])
    _bucket("缩量(量<5日均×0.8)", "vol_shrink", [(False, True, "否"), (True, 2, "是")])

    # 组合：多头 + 风险释放
    print("\n== 组合：多头 + 风险释放 ==")
    lines.append("## 组合：多头结构 + 风险释放 + 回踩")
    lines.append("| 组合 | n | fwd1 | fwd3 | fwd5 |")
    lines.append("|---|---|---|---|---|")
    combos = [
        ("多头+风险释放", lambda r: r["trend_multihead"] and r["risk_released"]),
        ("多头+风险释放+回踩MA20", lambda r: r["trend_multihead"] and r["risk_released"] and r["pullback_ma20"]),
        ("多头+回踩MA20", lambda r: r["trend_multihead"] and r["pullback_ma20"]),
        ("多头(指数)+风险释放", lambda r: r["idx_above_ma20"] and r["risk_released"]),
        ("多头+风险释放+缩量", lambda r: r["trend_multihead"] and r["risk_released"] and r["vol_shrink"]),
        ("空头(非多头)+风险释放", lambda r: not r["trend_multihead"] and r["risk_released"]),
    ]
    for name, pred in combos:
        sub = [r for r in rows if pred(r)]
        n, m1 = _mean_fwd(sub, "fwd1")
        _, m3 = _mean_fwd(sub, "fwd3")
        _, m5 = _mean_fwd(sub, "fwd5")
        f = lambda x: f"{x:.2%}" if x is not None else "—"  # noqa: E731
        print(f"{name:28s} n={n:7d} fwd1={f(m1)} fwd3={f(m3)} fwd5={f(m5)}")
        lines.append(f"| {name} | {n} | {f(m1)} | {f(m3)} | {f(m5)} |")

    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[OK] report → {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
