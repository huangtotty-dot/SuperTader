# -*- coding: utf-8 -*-
"""指数（上证/深成/创业板/科创50/沪深300/中证500/中证1000/上证50/北证50）的贴均线概率。

owner 2026-11-05：「指数（科创50/上证）也需要标」。

个股模型（AUC 0.671）直接套到指数上**严重高估**（预测 30% vs 实际 18%；指数整体
站稳率只有 5.9%，个股 23.9%）——指数波动小，0.2% 的「站上」阈值对指数更难。
所以指数单独拟合。

数据（两源合并、按时间戳去重）：
  · `t_io/cache/tushare_mins/{code}_5min.json`（2026-07-20 ~ 10-20/29）
  · 东财 push2his klt=5（只回最近 ~1536 根 ≈ 32 天，补 10-20 之后的尾巴）
缓存：`tmp/index_5min_{code}.parquet`

口径与个股第二版一致：5 分钟收盘走进日线 MA_n 下方 2% 以内（从带外进入的第一根），
看 6 根（30 分钟）后是否仍站在 MA×1.002 上方。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
CACHE = BASE / "t_io" / "cache" / "tushare_mins"
TMP = BASE / "tmp"
MAS = (5, 10, 20, 30, 60)
BAND = 0.02
IDX = {
    "000001.SH": "1.000001", "399001.SZ": "0.399001", "399006.SZ": "0.399006",
    "000688.SH": "1.000688", "000300.SH": "1.000300", "000016.SH": "1.000016",
    "000905.SH": "1.000905", "000852.SH": "1.000852", "899050.BJ": "0.899050",
}


def _em_fetch(secid: str) -> pd.DataFrame:
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=5&fqt=1&lmt=1600"
           "&end=20500101")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    kl = ((d.get("data") or {}).get("klines")) or []
    rows = []
    for line in kl:
        p = line.split(",")
        rows.append({"time": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]), "volume": float(p[5])})
    return pd.DataFrame(rows)


def load_bars(code: str) -> pd.DataFrame:
    fp = TMP / f"index_5min_{code.replace('.', '_')}.parquet"
    if fp.exists():
        return pd.read_parquet(fp)
    frames = []
    ts = CACHE / f"{code}_5min.json"
    if ts.exists():
        rows = json.loads(ts.read_text(encoding="utf-8"))["rows"]
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        frames.append(df[["time", "open", "high", "low", "close", "volume"]])
    try:
        em = _em_fetch(IDX[code])
        if not em.empty:
            em["time"] = pd.to_datetime(em["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            frames.append(em[["time", "open", "high", "low", "close", "volume"]])
    except Exception as e:
        print(f"  [warn] {code} 东财取数失败: {type(e).__name__}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("time").sort_values("time")
    TMP.mkdir(exist_ok=True)
    out.to_parquet(fp, index=False)
    return out


def events_for(code: str, df: pd.DataFrame) -> list:
    if df is None or len(df) < 500:
        return []
    df = df.reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["time"])
    day_codes, _ = pd.factorize(df["dt"].dt.normalize())
    n = len(df)
    bidx = np.arange(n) - np.searchsorted(day_codes, day_codes, side="left")
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    g = df.groupby(day_codes, sort=True)
    d_close = g["close"].last().to_numpy(float)
    d_high = g["high"].max().to_numpy(float)
    d_low = g["low"].min().to_numpy(float)
    D = len(d_close)
    cs = np.concatenate([[0.0], np.cumsum(d_close)])
    ma_rt, prev_day = {}, np.concatenate([[-1], day_codes[:-1]])
    for nn in MAS:
        ps = np.full(D, np.nan)
        idx = np.arange(nn - 1, D)
        ps[idx] = cs[idx] - cs[idx - (nn - 1)]
        ma_rt[nn] = (ps[day_codes] + close) / nn
    d_range = np.where(d_close > 0, (d_high - d_low) / d_close, np.nan)
    atr20 = pd.Series(d_range).rolling(20).mean().shift(1).to_numpy()
    out = []
    for nn in MAS:
        m = ma_rt[nn]
        pm = np.concatenate([[np.nan], m[:-1]])
        pc = np.concatenate([[np.nan], close[:-1]])
        dp = (pm - pc) / pm
        in_band_prev = np.isfinite(dp) & (dp > 0) & (dp <= BAND)
        cond = ((day_codes >= nn - 1) & np.isfinite(m)
                & (close < m) & (((m - close) / m) <= BAND)
                & ((day_codes != prev_day) | ~in_band_prev))
        first = {}
        for i in np.flatnonzero(cond):
            first.setdefault(day_codes[i], i)
        for i in first.values():
            d = day_codes[i]
            end = np.searchsorted(day_codes, d, side="right")
            j0, j1 = i + 1, i + 13
            if j1 > end:
                continue
            L = float(m[i])
            if not np.isfinite(L) or L <= 0:
                continue
            out.append({"code": code, "date": str(df["dt"].iloc[i].date()), "ma": nn,
                        "dist": (L - close[i]) / L,
                        "atr": atr20[d] if np.isfinite(atr20[d]) else np.nan,
                        "tod": bidx[i] / 48.0,
                        "brk": int(high[j0:j1].max() >= L * 1.002),
                        "hold": int(close[j1 - 1] >= L * 1.002),
                        "ret": float(close[j1 - 1] / close[i] - 1)})
    return out


def main() -> int:
    pd.set_option("display.width", 200)
    rows = []
    for code in IDX:
        df = load_bars(code)
        if df.empty:
            print(f"  {code}: 无数据")
            continue
        ev = events_for(code, df)
        print(f"  {code}: bars={len(df)} {df['time'].iloc[0][:10]}~{df['time'].iloc[-1][:10]} "
              f"事件={len(ev)}")
        rows.extend(ev)
        time.sleep(0.2)
    ev = pd.DataFrame(rows).dropna(subset=["atr"]) if rows else pd.DataFrame()
    if ev.empty:
        print("没有指数事件")
        return 0
    print(f"\n合计 {len(ev):,} 事件 / {ev['code'].nunique()} 指数 / {ev['date'].nunique()} 日  "
          f"{ev['date'].min()} ~ {ev['date'].max()}  站稳基准 {100*ev['hold'].mean():.1f}%")

    print("\n【1】分均线基准")
    r = []
    for ma in MAS:
        s = ev[ev["ma"] == ma]
        if len(s) < 20:
            continue
        r.append({"MA": f"MA{ma}", "n": len(s), "摸过": round(100 * s["brk"].mean(), 1),
                  "站稳": round(100 * s["hold"].mean(), 1),
                  "30分收益%": round(100 * s["ret"].mean(), 3)})
    print(pd.DataFrame(r).to_string(index=False))

    print("\n【2】距离档")
    b = pd.cut(ev["dist"], [0, 0.002, 0.005, 0.01, 0.02])
    g = ev.groupby(b, observed=True).agg(n=("hold", "size"), 站稳=("hold", "mean"),
                                         收益=("ret", "mean"))
    g["站稳"] = (100 * g["站稳"]).round(1)
    g["收益"] = (100 * g["收益"]).round(3)
    print(g.to_string())

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    ev["f_dist"] = ev["dist"] * 100
    ev["f_atr"] = ev["atr"] * 100
    ev["f_tod"] = ev["tod"].clip(upper=0.85)
    dates = sorted(ev["date"].unique())
    cut = dates[int(len(dates) * 0.6)]
    tr, te = ev[ev["date"] < cut], ev[ev["date"] >= cut]
    print(f"\n【3】时间外（训练 {len(tr)} / 验证 {len(te)}，验证基准 {100*te['hold'].mean():.1f}%）")
    for cols, label in ((["f_dist"], "只用距离"), (["f_dist", "f_atr", "f_tod"], "距离+波动+时段")):
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
        m.fit(tr[cols], tr["hold"])
        p = m.predict_proba(te[cols])[:, 1]
        print(f"  {label}: AUC={roc_auc_score(te['hold'], p):.4f}")
        if len(cols) == 3:
            lr, sc = m.named_steps["logisticregression"], m.named_steps["standardscaler"]
            raw = lr.coef_[0] / sc.scale_
            itc = lr.intercept_[0] - float(np.sum(lr.coef_[0] * sc.mean_ / sc.scale_))
            print(f"    原始尺度: intercept={itc:.6f}  dist={raw[0]:.6f}  atr={raw[1]:.6f}  tod={raw[2]:.6f}")
            q = pd.qcut(pd.Series(p), min(5, max(3, len(te) // 30)), duplicates="drop")
            cal = pd.DataFrame({"p": p, "y": te["hold"].to_numpy()}).groupby(q.values, observed=True).agg(
                n=("y", "size"), 预测=("p", "mean"), 实际=("y", "mean"))
            cal["预测"] = (100 * cal["预测"]).round(1)
            cal["实际"] = (100 * cal["实际"]).round(1)
            print(cal.to_string())
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
