# -*- coding: utf-8 -*-
"""
divergence.py — 30/60 分钟线顶背离/底背离检测（2026-08-19 新增）

需求：建仓扫描待选股增加背离列，30/60 分钟线出现顶/底背离时显示并飞书提醒。

数据：tushare stk_mins 近 30 日 30/60 分钟线（含当日 forming 根），当日缓存（盘中首拉一次，后续秒级）。
背离判定（与 t_gui 日线背离同逻辑，MACD dif）：
  - 顶背离：价格创新高，但 MACD dif 未创新高（看跌）
  - 底背离：价格创新低，但 MACD dif 未创新低（看涨）
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

CACHE_DIR = BASE / "t_io" / "cache" / "tushare_mins"
FETCH_DAYS = 35


def _ts_code(code: str) -> str:
    base = str(code).split("_")[0]
    return (base + ".SH") if base[0] in "56" else (base + ".SZ")


def fetch_freq_kline(code: str, freq: str = "60min", days: int = FETCH_DAYS) -> pd.DataFrame:
    """tushare 拉近 days 日 30/60 分钟线，当日缓存。返回 {time, open, high, low, close, volume}。
    生产默认 days=35：30min 走 1min 聚合保持口径；长历史(days>35)30min 用原生数据，
    因为 1min 单次拉取有 8000 条上限(90 天会被截断)。缓存文件名按 days 区分。"""
    ts_code = _ts_code(code)
    cache_key = f"{ts_code}_{freq}" if days == FETCH_DAYS else f"{ts_code}_{freq}_d{days}"
    fp = CACHE_DIR / f"{cache_key}.json"
    today = datetime.now().strftime("%Y-%m-%d")
    if fp.exists():
        try:
            cached = json.loads(fp.read_text(encoding="utf-8"))
            if cached.get("date") == today and cached.get("rows"):
                df = pd.DataFrame(cached["rows"])
                df["time"] = pd.to_datetime(df["time"])
                return df
        except Exception:
            pass
    try:
        from index_regime_intraday import _iri_tushare_pro
        pro = _iri_tushare_pro()
        start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        ts_freq = "1min" if (freq == "30min" and days == FETCH_DAYS) else freq
        df = pro.stk_mins(ts_code=ts_code, freq=ts_freq,
                          start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume"})
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    keep = [c for c in ("time", "open", "close", "high", "low", "volume", "amount") if c in df.columns]
    df = df[keep]
    # 聚合到目标分辨率（30min 生产默认用 1min 聚合；其余用原生+幂等重采样）
    if freq == "30min" and ts_freq == "1min":
        df = _resample_minutes(df, "30min")
    else:
        df = _resample_minutes(df, freq)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _rows = df.copy()
        _rows["time"] = _rows["time"].astype(str)  # json 不能序列化 Timestamp
        fp.write_text(json.dumps({"date": today, "rows": _rows.to_dict(orient="records")},
                                 ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return df


def _resample_minutes(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["_tb"] = df["time"].dt.floor(freq)
    agg = df.groupby("_tb").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "amount": "sum",
    }).reset_index()
    return agg.rename(columns={"_tb": "time"})


def _macd_dif(closes) -> np.ndarray:
    c = pd.Series(closes, dtype=float)
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    return (e12 - e26).values


def _local_extrema(highs, lows, n_bars=3):
    peaks, troughs = [], []
    for i in range(n_bars, len(highs) - n_bars):
        if all(highs[i] >= highs[i - j] for j in range(1, n_bars + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, n_bars + 1)):
            peaks.append(i)
        if all(lows[i] <= lows[i - j] for j in range(1, n_bars + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, n_bars + 1)):
            troughs.append(i)

    def _merge(idxs, vals, keep_max):
        """合并相邻极值（间距≤4 视为同一极值，保留更极端者），避免相邻等高峰误判。"""
        if not idxs:
            return []
        out = [idxs[0]]
        for i in idxs[1:]:
            if i - out[-1] <= 4:
                if (keep_max and vals[i] > vals[out[-1]]) or (not keep_max and vals[i] < vals[out[-1]]):
                    out[-1] = i
            else:
                out.append(i)
        return out

    return _merge(peaks, highs, True), _merge(troughs, lows, False)


def detect_divergence_events(df: pd.DataFrame) -> list:
    """检测单分辨率 K 线全部顶/底背离事件。
    返回 [{index, time, type('顶'/'底'), price, dif, consec}]；事件记在最新峰/谷上。
    consec=True 表示该峰/谷与前一个峰/谷形成连续同向背离（验证显示 60min 连续底背离有区分度）。"""
    if df is None or df.empty or len(df) < 40:
        return []
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    times = df["time"].values
    dif = _macd_dif(closes)
    peaks, troughs = _local_extrema(highs, lows)
    events = []
    peak_events, trough_events = {}, {}
    for i in range(1, len(peaks)):
        p2, p1 = peaks[i - 1], peaks[i]
        if highs[p1] > highs[p2] and dif[p1] < dif[p2]:
            e = {"index": int(p1), "time": str(times[p1]),
                 "type": "顶", "price": float(highs[p1]), "dif": float(dif[p1]), "consec": False}
            events.append(e)
            peak_events[p1] = e
    for i in range(1, len(troughs)):
        t2, t1 = troughs[i - 1], troughs[i]
        if lows[t1] < lows[t2] and dif[t1] > dif[t2]:
            e = {"index": int(t1), "time": str(times[t1]),
                 "type": "底", "price": float(lows[t1]), "dif": float(dif[t1]), "consec": False}
            events.append(e)
            trough_events[t1] = e
    events.sort(key=lambda e: e["index"])
    peak_pos = {p: i for i, p in enumerate(peaks)}
    trough_pos = {t: i for i, t in enumerate(troughs)}
    for e in events:
        if e["type"] == "顶":
            pos = peak_pos.get(e["index"])
            e["consec"] = bool(pos is not None and pos >= 1 and peaks[pos - 1] in peak_events)
        else:
            pos = trough_pos.get(e["index"])
            e["consec"] = bool(pos is not None and pos >= 1 and troughs[pos - 1] in trough_events)
    return events


def detect_minute_divergence_detail(code: str) -> dict:
    """检测个股 30/60 分钟线背离详情（含连续标记）。返回 {m30: {type, consec}, m60: {...}}。
    验证结论（2026-08-19，180天）：单次背离命中率≈随机基线；60min 连续底背离是唯一可信正向信号。"""
    out = {}
    for freq, key in (("30min", "m30"), ("60min", "m60")):
        try:
            df = fetch_freq_kline(code, freq)
            if df is None or df.empty or len(df) < 40:
                continue
            events = detect_divergence_events(df)
            if not events:
                continue
            last = events[-1]
            out[key] = {"type": "顶背离" if last["type"] == "顶" else "底背离",
                        "consec": bool(last.get("consec", False))}
        except Exception:
            continue
    return out


def detect_divergence_df(df: pd.DataFrame) -> str:
    """对单个分辨率 K 线判定背离。返回 '顶背离' / '底背离' / None。"""
    if df is None or df.empty or len(df) < 40:  # MACD(26) 预热 + 峰谷
        return None
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    dif = _macd_dif(closes)
    peaks, troughs = _local_extrema(highs, lows)
    if len(peaks) >= 2:
        p2, p1 = peaks[-2], peaks[-1]
        if highs[p1] > highs[p2] and dif[p1] < dif[p2]:
            return "顶背离"
    if len(troughs) >= 2:
        t2, t1 = troughs[-2], troughs[-1]
        if lows[t1] < lows[t2] and dif[t1] > dif[t2]:
            return "底背离"
    return None


def detect_minute_divergence(code: str) -> dict:
    """检测个股 30/60 分钟线顶/底背离。返回 {m30: '顶背离'/'底背离'/None, m60: ...}。"""
    out = {}
    for freq, key in (("30min", "m30"), ("60min", "m60")):
        try:
            df = fetch_freq_kline(code, freq)
            d = detect_divergence_df(df)
            if d:
                out[key] = d
        except Exception:
            continue
    return out


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="30/60分钟线背离检测")
    ap.add_argument("--code", required=True)
    args = ap.parse_args()
    r = detect_minute_divergence(args.code)
    print(f"{args.code}: 30分={r.get('m30', '无')} 60分={r.get('m60', '无')}")


if __name__ == "__main__":
    _cli()
