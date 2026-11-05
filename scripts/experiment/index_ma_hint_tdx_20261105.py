# -*- coding: utf-8 -*-
"""指数贴均线概率 · 通达信数据严格验证（owner 2026-11-05：「可以去抓取通达信的数据严格验证」）。

数据来源（按优先级）：
  1. 通达信本地 `vipdoc/{sh,sz}/fzline/{mk}{code}.lc5`（5 分钟，需客户端做过盘后下载）；
  2. pytdx 行情服务器 `get_index_bars(0=5min)` 往回翻页；
  3. 都没有 ⇒ 打印指引（TDX 客户端：盘后数据下载 → 5 分钟K线 → 上海/深圳指数）。

口径与 `index_ma_hint_20261105.py` 完全一致：5 分钟收盘走进日线 MA_n 下方 2% 以内
（从带外进入的第一根），看 6 根（30 分钟）后是否仍站在 MA×1.002 上方。

产物：`tmp/index_5min_tdx_{code}.parquet` + 终端表：
  · 分距离档的站稳率（用来替换/校准生产里的 `_HINT_INDEX_BUCKETS`）
  · 时间外 AUC（距离 / 距离+波动+时段）
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
TMP = BASE / "tmp"
TDX_HOME = Path(__import__("os").environ.get("TDX_HOME") or r"C:\new_tdx")
CODES = {
    "000001.SH": ("sh", "000001"), "399001.SZ": ("sz", "399001"),
    "399006.SZ": ("sz", "399006"), "000688.SH": ("sh", "000688"),
    "000300.SH": ("sh", "000300"), "000016.SH": ("sh", "000016"),
    "000905.SH": ("sh", "000905"), "000852.SH": ("sh", "000852"),
}
MAS = (5, 10, 20, 30, 60)
BAND = 0.02


def read_lc5(fp: Path) -> pd.DataFrame:
    """通达信 5 分钟 `.lc5`：32 字节/根。日期=uint16 编码，时间=分钟数，OHLC/额=float32，量=uint32。"""
    b = fp.read_bytes()
    n = len(b) // 32
    rows = []
    for i in range(0, n * 32, 32):
        d, t, o, h, lo, c, amt, vol, _ = struct.unpack("<HHfffffII", b[i:i + 32])
        y, m, dd = d // 2048 + 2004, (d % 2048) // 100, (d % 2048) % 100
        if not (1 <= m <= 12 and 1 <= dd <= 31):
            continue
        rows.append({"time": f"{y:04d}-{m:02d}-{dd:02d} {t // 60:02d}:{t % 60:02d}:00",
                     "open": o, "high": h, "low": lo, "close": c, "volume": float(vol)})
    return pd.DataFrame(rows)


def fetch_pytdx(code: str, mk: str, c6: str, pages: int = 30) -> pd.DataFrame:
    """pytdx `get_index_bars` 往回翻页（每页 800 根）。返回按时间升序。"""
    from pytdx.config.hosts import hq_hosts
    from pytdx.hq import TdxHq_API
    market = 1 if mk == "sh" else 0
    api = TdxHq_API()
    for _name, h, p in hq_hosts[:60]:
        try:
            if not api.connect(h, int(p), time_out=2):
                continue
        except Exception:
            continue
        try:
            frames, start = [], 0
            for _ in range(pages):
                d = api.get_index_bars(0, market, c6, start, 800)
                if not d:
                    break
                frames.append(pd.DataFrame(d))
                if len(d) < 800:
                    break
                start += 800
            if frames:
                df = pd.concat(frames, ignore_index=True)
                df = df.drop_duplicates("datetime").sort_values("datetime")
                out = pd.DataFrame({
                    "time": pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": df["open"].astype(float), "high": df["high"].astype(float),
                    "low": df["low"].astype(float), "close": df["close"].astype(float),
                    "volume": df["vol"].astype(float) if "vol" in df else 0.0,
                })
                print(f"    pytdx {h}:{p} → {len(out)} 根 {out['time'].iloc[0]} ~ {out['time'].iloc[-1]}")
                return out
        except Exception as e:
            print(f"    pytdx {h} 失败: {type(e).__name__}: {str(e)[:50]}")
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    return pd.DataFrame()


def load_5min(code: str, mk: str, c6: str) -> pd.DataFrame:
    fp = TMP / f"index_5min_tdx_{code.replace('.', '_')}.parquet"
    if fp.exists():
        return pd.read_parquet(fp)
    lc5 = TDX_HOME / "vipdoc" / mk / "fzline" / f"{mk}{c6}.lc5"
    df = pd.DataFrame()
    if lc5.exists() and lc5.stat().st_size > 32:
        df = read_lc5(lc5)
        print(f"  {code}: 本地 lc5 {len(df)} 根 {df['time'].iloc[0]} ~ {df['time'].iloc[-1]}")
    else:
        print(f"  {code}: 本地无 lc5，试 pytdx…")
        df = fetch_pytdx(code, mk, c6)
    if not df.empty:
        TMP.mkdir(exist_ok=True)
        df.to_parquet(fp, index=False)
    return df


def events_for(code: str, df: pd.DataFrame) -> list:
    if df is None or len(df) < 500:
        return []
    df = df.reset_index(drop=True)
    dt = pd.to_datetime(df["time"])
    day_codes, _ = pd.factorize(dt.dt.normalize())
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
            out.append({"code": code, "date": str(dt.iloc[i].date()), "ma": nn,
                        "dist": (L - close[i]) / L,
                        "atr": atr20[d] if np.isfinite(atr20[d]) else np.nan,
                        "tod": bidx[i] / 48.0,
                        "hold": int(close[j1 - 1] >= L * 1.002),
                        "ret": float(close[j1 - 1] / close[i] - 1)})
    return out


def main() -> int:
    pd.set_option("display.width", 200)
    rows = []
    for code, (mk, c6) in CODES.items():
        df = load_5min(code, mk, c6)
        if df.empty:
            print(f"  {code}: 无数据")
            continue
        ev = events_for(code, df)
        print(f"  {code}: 事件={len(ev)}")
        rows.extend(ev)
    ev = pd.DataFrame(rows).dropna(subset=["atr"]) if rows else pd.DataFrame()
    if not ev.empty:
        TMP.mkdir(exist_ok=True)
        ev.to_parquet(TMP / "index_ma_events_tdx.parquet", index=False)
    if ev.empty:
        print("\n没有指数事件：TDX 本地没有 5 分钟档，pytdx 也没取到。\n"
              "请在通达信客户端执行：盘后数据下载 → 5 分钟K线 → 上海/深圳指数，再重跑本脚本。")
        return 0
    print(f"\n合计 {len(ev):,} 事件 / {ev['code'].nunique()} 指数 / {ev['date'].nunique()} 日  "
          f"{ev['date'].min()} ~ {ev['date'].max()}  站稳基准 {100*ev['hold'].mean():.1f}%")

    print("\n【1】距离分档（替换生产 `_HINT_INDEX_BUCKETS` 的依据）")
    b = pd.cut(ev["dist"], [0, 0.002, 0.005, 0.01, 0.02])
    g = ev.groupby(b, observed=True).agg(n=("hold", "size"), 站稳=("hold", "mean"),
                                         收益=("ret", "mean"))
    g["站稳"] = (100 * g["站稳"]).round(1)
    g["收益"] = (100 * g["收益"]).round(3)
    print(g.to_string())

    print("\n【2】分均线")
    r = []
    for ma in MAS:
        s = ev[ev["ma"] == ma]
        if len(s) < 20:
            continue
        r.append({"MA": f"MA{ma}", "n": len(s), "站稳": round(100 * s["hold"].mean(), 1),
                  "30分收益%": round(100 * s["ret"].mean(), 3)})
    print(pd.DataFrame(r).to_string(index=False))

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
    print(f"\n【3】时间外（训练 {len(tr):,} / 验证 {len(te):,}，基准 {100*te['hold'].mean():.1f}%）")
    for cols, label in ((["f_dist"], "只用距离"), (["f_dist", "f_atr", "f_tod"], "距离+波动+时段")):
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
        m.fit(tr[cols], tr["hold"])
        p = m.predict_proba(te[cols])[:, 1]
        print(f"  {label}: AUC={roc_auc_score(te['hold'], p):.4f}")
        if len(cols) == 3:
            lr, sc = m.named_steps["logisticregression"], m.named_steps["standardscaler"]
            raw = lr.coef_[0] / sc.scale_
            itc = lr.intercept_[0] - float(np.sum(lr.coef_[0] * sc.mean_ / sc.scale_))
            print(f"    原始尺度: intercept={itc:.6f} dist={raw[0]:.6f} atr={raw[1]:.6f} tod={raw[2]:.6f}")
            q = pd.qcut(pd.Series(p), 10, duplicates="drop")
            cal = pd.DataFrame({"p": p, "y": te["hold"].to_numpy()}).groupby(q.values, observed=True).agg(
                n=("y", "size"), 预测=("p", "mean"), 实际=("y", "mean"))
            cal["预测"] = (100 * cal["预测"]).round(1)
            cal["实际"] = (100 * cal["实际"]).round(1)
            print("    十分位校准:")
            print(cal.to_string())
            # 分指数
            for code, sub in te.groupby("code"):
                ps = m.predict_proba(sub[cols])[:, 1]
                print(f"      {code}: n={len(sub):>4} 基准={100*sub['hold'].mean():4.1f}%  "
                      f"AUC={roc_auc_score(sub['hold'], ps):.3f}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
