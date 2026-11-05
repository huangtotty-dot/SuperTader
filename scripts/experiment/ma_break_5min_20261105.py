# -*- coding: utf-8 -*-
"""5 分钟线「走到关键均线位置」能否翻过去 · 特征实验（owner 2026-11-05）。

问题：盘中 5 分钟线走到 **日线 MA5/10/20/30/60** 附近时，到底能不能翻过去？
      有没有可提前观测的特征（量、斜率、位置、时间…）能区分？

数据：`t_io/cache/tushare_mins/*_5min_d540.json` —— 981 只 × 361 日 × 49 根
      （2025-03-31 ~ 2026-09-18，与 30min d540 面板同池同窗口）。

口径（只用**已完成**的 bar，无前视）：
  · 日线 = 当日 5min bar 聚合（close=末根）；
  · 实时均线 MA_rt = (前 n-1 个已完成日收盘 + 当前 bar 收盘) / n（与 GUI `_daily_ma_bundle` 同口径）；
  · **事件**（走到关键位，自下而上）：
        前一根 bar 收盘 明显在均线下方（< MA×0.999）
        且 本根 high ≥ MA_rt（盘中摸到线）
        且 本根 close 仍在均线下方（dist = (L−close)/L > 0，问题才干净）
    每只票每个交易日每条均线**只取第一次**（去重）；
  · **冻结参考位** L = 事件那根的 MA_rt（后续均线会随价格动，用会移动的目标测「翻过去」不干净）；
  · 结果：
        brk_high_H  = 未来 H 根内 high 触及 L×1.002（摸过去了）
        brk_close_H = 未来 H 根内 close ≥ L×1.002（收上去过）
        hold_H      = 第 H 根收盘仍 ≥ L×1.002（站住了）
        ret_H / ret_eod = 事件收盘 → 第 H 根 / 当日收盘 的收益
    H ∈ {6, 12, 24}（30/60/120 分钟）。

特征（全部事件时点可得）：
  ret6/ret24 近 30/120 分钟动量 · volr 本根量/前24根均量 · ma_slope5 日线均线5日斜率
  dist 离均线百分比 · tod 发生在当日第几根 · gap 今开相对昨收 · dayret 当日至今涨幅
  align 多头排列(MA5>MA10>MA20) · atr20 日线波动
  volratio 盘中量比 = 截至当前 bar 的累计量 ÷ 前 60 日**同一时点**累计量均值（无前视）
  touches20 前 20 日「摸到该线但收盘没过」次数 · side60 价格在 MA60 上/下
  prev_above 昨日收盘是否已在该线上方

产物：`tmp/ma_break_5min_events.parquet`（缓存，重跑秒开）+ 终端统计表。
运行：`python scripts/experiment/ma_break_5min_20261105.py [--rebuild]`
"""
from __future__ import annotations

import json
import sys
from glob import glob
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
CACHE = BASE / "t_io" / "cache" / "tushare_mins"
OUT = BASE / "tmp" / "ma_break_5min_events.parquet"
MAS = (5, 10, 20, 30, 60)
HS = (6, 12, 24)
EPS_BRK = 0.002         # 站上均线 0.2% 才算「翻过去」
BELOW_MARGIN = 0.001    # 前一根须在均线下方 0.1% 以上


def build() -> pd.DataFrame:
    files = sorted(glob(str(CACHE / "*_5min_d540.json")))
    if "--limit" in sys.argv:            # 调试：只跑前 N 只
        files = files[:int(sys.argv[sys.argv.index("--limit") + 1])]
    frames = []
    for k, fp in enumerate(files, 1):
        code = Path(fp).name.split("_5min_d540")[0]
        try:
            rows = json.loads(Path(fp).read_text(encoding="utf-8"))["rows"]
        except Exception as e:
            print(f"[skip] {code}: {e}")
            continue
        if len(rows) < 200:
            continue
        df = pd.DataFrame(rows)
        df["dt"] = pd.to_datetime(df["time"])
        df["day"] = df["dt"].dt.normalize()
        day_codes, day_uniq = pd.factorize(df["day"])
        n = len(df)
        # 当日第几根
        bidx = np.arange(n) - np.searchsorted(day_codes, day_codes, side="left")
        close = df["close"].to_numpy(float)
        high = df["high"].to_numpy(float)
        low = df["low"].to_numpy(float)
        open_ = df["open"].to_numpy(float)
        vol = df["volume"].to_numpy(float)

        g = df.groupby(day_codes, sort=True)
        d_close = g["close"].last().to_numpy(float)
        d_high = g["high"].max().to_numpy(float)
        d_low = g["low"].min().to_numpy(float)
        d_open = g["open"].first().to_numpy(float)
        d_vol = g["volume"].sum().to_numpy(float)
        D = len(d_close)
        if D < 70:
            continue
        cs = np.concatenate([[0.0], np.cumsum(d_close)])

        def daily_ma(nn):
            out = np.full(D, np.nan)
            if D >= nn:
                out[nn - 1:] = (cs[nn:] - cs[:-nn]) / nn
            return out

        d_ma = {nn: daily_ma(nn) for nn in MAS}
        # 事件时点的「已完成后 n-1 日收盘和」：对第 d 天而言是 close[d-n+1..d-1]
        prevsum = {}
        for nn in MAS:
            ps = np.full(D, np.nan)
            idx = np.arange(nn - 1, D)
            ps[idx] = cs[idx] - cs[idx - (nn - 1)]
            prevsum[nn] = ps

        ma_rt = {}
        for nn in MAS:
            ma_rt[nn] = (prevsum[nn][day_codes] + close) / nn

        touch_day = {nn: (d_high >= d_ma[nn]) & (d_close < d_ma[nn]) for nn in MAS}
        d_range = np.where(d_close > 0, (d_high - d_low) / d_close, np.nan)
        atr20 = pd.Series(d_range).rolling(20).mean().shift(1).to_numpy()
        # 盘中「量比」——⚠️ 绝不能用当日**全天**总量（事件在上午却知道下午的量＝前视）。
        # 口径：截至当前 bar 的累计量 ÷ 前 60 个交易日**同一时点**的累计量均值。
        cumvol = df.groupby(day_codes)["volume"].cumsum().to_numpy(float)
        nb = int(bidx.max()) + 1
        cvmat = np.full((D, nb), np.nan)
        cvmat[day_codes, bidx] = cumvol
        cv_prior = (pd.DataFrame(cvmat).rolling(60, min_periods=20).mean().shift(1).to_numpy())
        gap = np.full(D, np.nan)
        gap[1:] = d_open[1:] / d_close[:-1] - 1.0
        align = np.zeros(D, bool)
        align[1:] = (d_ma[5][:-1] > d_ma[10][:-1]) & (d_ma[10][:-1] > d_ma[20][:-1])

        prev_close = np.concatenate([[np.nan], close[:-1]])
        prev_day = np.concatenate([[-1], day_codes[:-1]])
        prev_ma = {nn: np.concatenate([[np.nan], ma_rt[nn][:-1]]) for nn in MAS}
        recs: list = []

        for nn in MAS:
            m = ma_rt[nn]
            cond = (
                (day_codes >= nn - 1)
                & (day_codes == prev_day)
                & np.isfinite(m) & np.isfinite(prev_close)
                & (prev_close < prev_ma[nn] * (1 - BELOW_MARGIN))  # 上一根仍在下方
                & (high >= m)                                     # 本根盘中摸到
                & (close < m)                                     # 收盘仍在下方（问题才干净）
            )
            if not cond.any():
                continue
            # 每日只取第一根（同一日均线的一次接触）
            idxs = np.flatnonzero(cond)
            first = {}
            for i in idxs:
                d = day_codes[i]
                if d not in first:
                    first[d] = i
            ev = np.fromiter(first.values(), dtype=np.int64)

            # 前 20 日「摸到但没过」次数（按已完成日线算）
            td = touch_day[nn]
            t20 = pd.Series(td.astype(float)).rolling(20).sum().shift(1).to_numpy()
            # 均线斜率（5 个交易日）
            slope5 = np.full(D, np.nan)
            slope5[6:] = d_ma[nn][5:-1] / d_ma[nn][:-6] - 1.0

            for i in ev:
                d = day_codes[i]
                L = ma_rt[nn][i]
                if not np.isfinite(L) or L <= 0:
                    continue
                end = np.searchsorted(day_codes, d, side="right")  # 当日最后一根+1
                j0 = i + 1
                rec = {
                    "code": code, "date": str(df["day"].iloc[i])[:10], "ma": nn,
                    "bar": int(bidx[i]), "L": L,
                    "close_i": close[i], "dist": (L - close[i]) / L,
                }
                for H in HS:
                    j1 = j0 + H
                    if j1 > end:      # 当日剩余不足 H 根 ⇒ 不参与该窗口统计
                        rec[f"brk_high_{H}"] = np.nan
                        rec[f"brk_close_{H}"] = np.nan
                        rec[f"hold_{H}"] = np.nan
                        rec[f"ret_{H}"] = np.nan
                        continue
                    fut_h = high[j0:j1]
                    fut_c = close[j0:j1]
                    rec[f"brk_high_{H}"] = int(fut_h.max() >= L * (1 + EPS_BRK))
                    rec[f"brk_close_{H}"] = int(fut_c.max() >= L * (1 + EPS_BRK))
                    rec[f"hold_{H}"] = int(fut_c[-1] >= L * (1 + EPS_BRK))
                    rec[f"ret_{H}"] = float(fut_c[-1] / close[i] - 1.0)
                rec["ret_eod"] = float(close[end - 1] / close[i] - 1.0)
                # 特征
                ii = i
                rec["ret6"] = float(close[ii] / close[ii - 6] - 1.0) if ii >= 6 else np.nan
                rec["ret24"] = float(close[ii] / close[ii - 24] - 1.0) if ii >= 24 else np.nan
                v0 = max(0, ii - 24)
                base_v = np.nanmean(vol[v0:ii]) if ii > v0 else np.nan
                rec["volr"] = float(vol[ii] / base_v) if base_v and base_v > 0 else np.nan
                rec["ma_slope5"] = float(slope5[d]) if np.isfinite(slope5[d]) else np.nan
                rec["tod"] = int(bidx[i])
                rec["gap"] = float(gap[d]) if np.isfinite(gap[d]) else np.nan
                rec["dayret"] = float(close[ii] / d_open[d] - 1.0)
                rec["align"] = int(align[d]) if np.isfinite(d_ma[20][d - 1]) else np.nan
                rec["atr20"] = float(atr20[d]) if np.isfinite(atr20[d]) else np.nan
                _cv = cv_prior[d, bidx[i]]
                rec["volratio"] = float(cumvol[i] / _cv) if np.isfinite(_cv) and _cv > 0 else np.nan
                rec["touches20"] = float(t20[d]) if np.isfinite(t20[d]) else np.nan
                m60 = ma_rt[60][i]
                rec["side60"] = int(close[ii] > m60) if np.isfinite(m60) else np.nan
                rec["prev_above"] = int(d_close[d - 1] > d_ma[nn][d - 1]) if np.isfinite(d_ma[nn][d - 1]) else np.nan
                recs.append(rec)
        if recs:
            frames.append(pd.DataFrame(recs))
        if k % 100 == 0:
            n_ev = sum(len(f) for f in frames)
            print(f"  ... {k}/{len(files)} 只，累计事件 {n_ev:,}", flush=True)
    ev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if ev.empty:
        raise SystemExit("没有事件")
    OUT.parent.mkdir(exist_ok=True)
    ev.to_parquet(OUT, index=False)
    print(f"事件表: {OUT}  {len(ev):,} 行")
    return ev


def _dayclust_t(sub: pd.DataFrame, all_: pd.DataFrame, col: str) -> tuple[float, float]:
    """按日聚类的差值 t：每日(该组均值 − 全体均值)，再对日均值序列做 t。"""
    a = sub.groupby("date")[col].mean()
    b = all_.groupby("date")[col].mean()
    j = pd.concat([a, b], axis=1, keys=["g", "a"]).dropna()
    if len(j) < 5:
        return float("nan"), float("nan")
    d = j["g"] - j["a"]
    t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if d.std(ddof=1) > 0 else float("nan")
    return float(d.mean()), float(t)


def analyze(ev: pd.DataFrame) -> None:
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 40)
    print("\n" + "=" * 96)
    print(f"事件总数 {len(ev):,}   票 {ev['code'].nunique()}   日 {ev['date'].nunique()}   "
          f"窗口 {ev['date'].min()} ~ {ev['date'].max()}")
    print("=" * 96)

    print("\n【1】各均线的基准（无条件）通过率 —— 走到这条线上，后来发生了什么")
    rows = []
    for nn in MAS:
        s = ev[ev["ma"] == nn]
        if s.empty:
            continue
        r = {"MA": f"MA{nn}", "n": len(s)}
        for H in HS:
            r[f"摸过{H}(%)"] = round(100 * s[f"brk_high_{H}"].mean(), 1)
            r[f"收上{H}(%)"] = round(100 * s[f"brk_close_{H}"].mean(), 1)
            r[f"站稳{H}(%)"] = round(100 * s[f"hold_{H}"].mean(), 1)
            r[f"事后{H}收益(%)"] = round(100 * s[f"ret_{H}"].mean(), 3)
        r["当日剩余收益(%)"] = round(100 * s["ret_eod"].mean(), 3)
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n【2】特征分层（以 hold_12 = 30 分钟后仍站在线上 0.2% 为目标）")
    print("    t = 按日聚类的「该档通过率 − 全体通过率」t 值；|t|≥3 才算有话说")
    feats = [
        ("volr", "量能/前24根均量", [0.5, 1.0, 1.5, 2.0]),
        ("ma_slope5", "均线5日斜率", [-0.02, -0.005, 0.005, 0.02]),
        ("dist", "离均线距离", [0.001, 0.003, 0.006, 0.012]),
        ("tod", "当日第几根", [6, 12, 24, 36]),
        ("ret6", "近30分钟动量", [-0.01, -0.003, 0.003, 0.01]),
        ("ret24", "近120分钟动量", [-0.02, -0.005, 0.005, 0.02]),
        ("gap", "今开相对昨收", [-0.01, -0.003, 0.003, 0.01]),
        ("dayret", "当日至今涨幅", [-0.01, 0.0, 0.01, 0.02]),
        ("atr20", "日线波动", [0.02, 0.03, 0.045, 0.06]),
        ("volratio", "盘中量比(同时点累计)", [0.8, 1.0, 1.3, 1.8]),
        ("touches20", "前20日摸线次数", [0, 1, 2, 4]),
    ]
    for nn in MAS:
        s = ev[ev["ma"] == nn].copy()
        if len(s) < 2000:
            continue
        print(f"\n--- MA{nn}（n={len(s):,}）  基准 站稳12 = {100*s['hold_12'].mean():.1f}% ---")
        out = []
        for col, label, cuts in feats:
            x = s[col]
            if x.notna().sum() < len(s) * 0.5:
                continue
            try:
                b = pd.cut(x, [-np.inf] + cuts + [np.inf], duplicates="drop")
            except Exception:
                continue
            for bv, sub in s.groupby(b, observed=True):
                if len(sub) < 200:
                    continue
                dm, t = _dayclust_t(sub, s, "hold_12")
                out.append({"特征": label, "档": str(bv), "n": len(sub),
                            "站稳12(%)": round(100 * sub["hold_12"].mean(), 1),
                            "差(pp)": round(100 * dm, 2), "t": round(t, 1),
                            "事后12收益(%)": round(100 * sub["ret_12"].mean(), 3)})
        if out:
            o = pd.DataFrame(out)
            o = o[o["t"].abs() >= 2]
            print(o.to_string(index=False) if len(o) else "    （没有 |t|≥2 的档）")

    print("\n【3】时间外样本检查（前 60% 日期拟合方向 / 后 40% 验证）")
    dates = sorted(ev["date"].unique())
    cut = dates[int(len(dates) * 0.6)]
    tr, te = ev[ev["date"] < cut], ev[ev["date"] >= cut]
    print(f"    训练 {tr['date'].min()}~{tr['date'].max()}（{len(tr):,}） | "
          f"验证 {te['date'].min()}~{te['date'].max()}（{len(te):,}）")
    checks = [
        ("盘中量比≥1.5 vs <1.0", lambda d: d["volratio"] >= 1.5, lambda d: d["volratio"] < 1.0),
        ("bar量能≥2× vs <1×", lambda d: d["volr"] >= 2.0, lambda d: d["volr"] < 1.0),
        ("均线上行 slope5>0 vs <0", lambda d: d["ma_slope5"] > 0, lambda d: d["ma_slope5"] < 0),
        ("多头排列 vs 非", lambda d: d["align"] == 1, lambda d: d["align"] == 0),
        ("前20日摸线≥2 vs 0", lambda d: d["touches20"] >= 2, lambda d: d["touches20"] == 0),
        ("MA60 上方 vs 下方", lambda d: d["side60"] == 1, lambda d: d["side60"] == 0),
        ("早盘(前12根) vs 尾盘(后12根)", lambda d: d["tod"] <= 11, lambda d: d["tod"] >= 36),
    ]
    for label, hi, lo in checks:
        r = {}
        for name, d in (("训练", tr), ("验证", te)):
            a, b = d[hi(d)], d[lo(d)]
            r[name] = (len(a), len(b), 100 * a["hold_12"].mean() if len(a) else np.nan,
                       100 * b["hold_12"].mean() if len(b) else np.nan)
        a1, b1, ra, rb = r["训练"]
        a2, b2, ra2, rb2 = r["验证"]
        print(f"    {label:<26} 训练 {ra:5.1f}% vs {rb:5.1f}%（{ra-rb:+5.1f}pp，n={a1}/{b1}） | "
              f"验证 {ra2:5.1f}% vs {rb2:5.1f}%（{ra2-rb2:+5.1f}pp，n={a2}/{b2}）")


def main() -> int:
    if "--rebuild" not in sys.argv and OUT.exists():
        ev = pd.read_parquet(OUT)
        print(f"读取缓存 {OUT}（{len(ev):,} 行）；加 --rebuild 重建")
    else:
        ev = build()
    analyze(ev)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
