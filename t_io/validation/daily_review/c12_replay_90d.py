# -*- coding: utf-8 -*-
"""C12 阴跌日低吸豁免通道 · 90日扩窗回放（周六评审预研第1项，2026-08-20）

复用 2026-08-18 单日回放（stock_hunter/analysis_c12_replay.py）的同口径规则，
扩到 minute_snapshots 全部可得历史（2026-03 ~ 2026-08），对当前 6 只持仓逐日回放：
  现役引擎低吸: bb_pct_5m <= 0.15 且 15分MACD hist > 0
  C12 豁免低吸: bb_pct_5m <= 0.15 且 hist <= 0 且 rsi_5m_p6 < 20 且
               （当日跌幅 <= -3% 或 自日内高点回撤 <= -3%）
统计：C12 触发次数、前瞻收益（+30min/+60min/至收盘）胜率与均值、
     按当日涨跌幅分桶、最差案例，供周六评审判断豁免通道是否放行。
口径限制：分钟快照仅覆盖系统运行期间曾持仓/监控的标的与日期，
     非全市场随机样本，结论只代表"本系统持仓池"上的豁免表现。
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, r"E:\superTrader")
from analysis.indicators import resample_to_5min, add_5min_indicators, resample_to_15min

BASE = Path(r"E:\superTrader")
SNAP_DIR = BASE / "t_io" / "minute_snapshots" / "2026"
KLINE_CACHE = BASE / "t_io" / "cache" / "daily_kline"
OUT_MD = BASE / "doc" / "每日复盘" / "C12_修正版90日扩窗回放_20260820.md"

CODES = ["600176", "000988", "588170", "600481", "603667", "002639"]


def macd15_hist_series(df1: pd.DataFrame) -> pd.DataFrame:
    df15 = resample_to_15min(df1)
    c = df15["close"]
    dif = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    df15["m15_hist"] = (dif - dea) * 2
    return df15[["time", "m15_hist"]]


def load_prev_close_map(code: str) -> dict:
    fp = KLINE_CACHE / f"{code}.json"
    if not fp.exists():
        return {}
    rows = json.loads(fp.read_text(encoding="utf-8")).get("rows", [])
    dates = [r["date"] for r in rows]
    return {dates[i]: rows[i - 1]["close"] for i in range(1, len(rows))}


def replay_day(code: str, fp: Path, prev_map: dict):
    date = fp.stem.split("_", 1)[1]
    prev = prev_map.get(date)
    if not prev:
        return None, []
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    bars = d.get("bars") or d.get("minutes") or d.get("data") or []
    if len(bars) < 40:
        return None, []
    df1 = pd.DataFrame(bars)
    try:
        df5 = add_5min_indicators(resample_to_5min(df1))
        m15 = macd15_hist_series(df1)
        df5 = pd.merge_asof(df5.sort_values("time"), m15.sort_values("time"),
                            on="time", direction="backward")
    except Exception:
        return None, []
    df5["day_ret"] = df5["close"] / prev - 1
    df5["dd_from_high"] = df5["close"] / df5["high"].cummax() - 1
    df5 = df5.iloc[13:].reset_index(drop=True)
    if df5.empty:
        return None, []
    touch = df5[df5["bb_pct_5m"] <= 0.15].copy()
    touch["engine_buy"] = touch["m15_hist"] > 0
    touch["c12_buy"] = ((~touch["engine_buy"]) & (touch["rsi_5m_p6"] < 20)
                        & ((touch["day_ret"] <= -0.03) | (touch["dd_from_high"] <= -0.03)))

    df1["time"] = pd.to_datetime(df1["time"])
    df1i = df1.set_index("time").sort_index()
    close_day = df1i["close"].iloc[-1]

    fires = []
    for _, r in touch[touch["c12_buy"]].iterrows():
        t, p = r["time"], r["close"]
        def fwd(minutes):
            win = df1i.loc[t:t + pd.Timedelta(minutes=minutes), "close"]
            return (win.iloc[-1] / p - 1) * 100 if len(win) else float("nan")
        fires.append({
            "date": date, "code": code, "time": str(t), "price": p,
            "day_ret_at_fire": r["day_ret"] * 100,
            "fwd30": fwd(30), "fwd60": fwd(60),
            "to_close": (close_day / p - 1) * 100,
        })
    day_row = {
        "date": date, "code": code,
        "day_close_ret": (close_day / prev - 1) * 100,
        "n_touch": len(touch),
        "n_engine": int(touch["engine_buy"].sum()),
        "n_c12": len(fires),
    }
    return day_row, fires


def main():
    day_rows, all_fires = [], []
    for code in CODES:
        prev_map = load_prev_close_map(code)
        files = sorted(SNAP_DIR.glob(f"*/{code}_*.json"))
        files = [f for f in files if f"{code}_B" not in f.name]
        for fp in files:
            dr, fires = replay_day(code, fp, prev_map)
            if dr:
                day_rows.append(dr)
                all_fires.extend(fires)

    days = pd.DataFrame(day_rows)
    fires = pd.DataFrame(all_fires)
    L = []
    L.append("# C12 修正版 90 日扩窗回放（2026-08-20 生成）\n")
    L.append(f"- 回放标的：{', '.join(CODES)}（当前持仓池）")
    L.append(f"- 覆盖：{days['date'].min()} ~ {days['date'].max()}，共 {len(days)} 个标的日")
    L.append(f"- 触下轨 5分K 总数：{int(days['n_touch'].sum())}；"
             f"现役引擎买入触发 {int(days['n_engine'].sum())} 根；"
             f"C12 豁免触发 {len(fires)} 根\n")
    if fires.empty:
        L.append("**90 日内 C12 豁免通道零触发** —— 通道条件过严或持仓池阴跌样本不足。\n")
    else:
        def stat(col):
            s = fires[col].dropna()
            win = (s > 0).mean() * 100
            return f"胜率 {win:.0f}%（{(s > 0).sum()}/{len(s)}），均值 {s.mean():+.2f}%，中位 {s.median():+.2f}%，最差 {s.min():+.2f}%"
        L.append("## C12 触发点前瞻收益\n")
        L.append(f"- +30min：{stat('fwd30')}")
        L.append(f"- +60min：{stat('fwd60')}")
        L.append(f"- 至收盘：{stat('to_close')}\n")
        L.append("## 按代码汇总\n")
        L.append("| 代码 | 触发数 | 至收盘胜率 | 至收盘均值 |")
        L.append("|---|---|---|---|")
        for code, g in fires.groupby("code"):
            s = g["to_close"].dropna()
            L.append(f"| {code} | {len(g)} | {(s > 0).mean() * 100:.0f}% | {s.mean():+.2f}% |")
        L.append("\n## 逐次触发明细\n")
        L.append("| 日期 | 代码 | 时间 | 买价 | 触发时日跌幅 | +30min | +60min | 至收盘 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for _, r in fires.iterrows():
            L.append(f"| {r['date']} | {r['code']} | {r['time'][11:16]} | {r['price']:.2f} "
                     f"| {r['day_ret_at_fire']:+.2f}% | {r['fwd30']:+.2f}% "
                     f"| {r['fwd60']:+.2f}% | {r['to_close']:+.2f}% |")
    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"标的日 {len(days)}，C12 触发 {len(fires)} 次")
    if not fires.empty:
        s = fires["to_close"].dropna()
        print(f"至收盘胜率 {(s > 0).mean() * 100:.0f}% 均值 {s.mean():+.2f}% 最差 {s.min():+.2f}%")
    print(f"输出 -> {OUT_MD}")


if __name__ == "__main__":
    main()
