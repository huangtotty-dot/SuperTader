# -*- coding: utf-8 -*-
"""GmProvider — 掘金数据源（合并实施方案 P1-1，主源）。
gm SDK volume 单位=股 → 统一 ÷100 收敛为"手"；日线强制前复权(ADJUST_PREV)。
"""
from datetime import datetime

import pandas as pd

from .codec import to_gm
from .provider import MarketDataProvider


def _gm_index_symbol(index: str) -> str:
    """"sh000001"→"SHSE.000001"；"sz399001"→"SZSE.399001"；已是 GM 格式原样返回。"""
    i = str(index).strip()
    low = i.lower()
    if low.startswith("sh") and not low.startswith("shse"):
        return "SHSE." + i[2:]
    if low.startswith("sz") and not low.startswith("szse"):
        return "SZSE." + i[2:]
    return i


class GmProvider(MarketDataProvider):
    source = "gm"

    def __init__(self, token=None):
        import gm.api as gma
        self._gma = gma
        if token is None:
            from .gm_token import load_token
            token = load_token()
        self._ready = bool(token)
        if token:
            gma.set_token(token)

    # ---------- 日线 ----------
    def daily(self, code: str, days: int = 800) -> pd.DataFrame:
        gma = self._gma
        df = gma.history_n(symbol=to_gm(code), frequency="1d", count=days,
                           end_time=datetime.now().strftime("%Y-%m-%d"),
                           fields="eob,open,high,low,close,volume",
                           adjust=gma.ADJUST_PREV, df=True)
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        out = pd.DataFrame({
            "date": pd.to_datetime(df["eob"]).dt.strftime("%Y-%m-%d"),
            "open": df["open"].astype(float), "high": df["high"].astype(float),
            "low": df["low"].astype(float), "close": df["close"].astype(float),
            "volume": df["volume"].astype(float) / 100.0,  # 股 → 手
        })
        return out.sort_values("date").reset_index(drop=True)

    # ---------- 分钟线 ----------
    def minute(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        # ttl_seconds 仅对 CSV 缓存（腾讯侧）有意义；gm 每次实时拉取，忽略之。
        gma = self._gma
        end_time = f"{date} 15:00:00"
        df = gma.history_n(symbol=to_gm(code), frequency="60s", count=240,
                           end_time=end_time,
                           fields="eob,open,high,low,close,volume,amount",
                           adjust=gma.ADJUST_PREV, df=True)
        if df is None or df.empty:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
        t = pd.to_datetime(df["eob"]).dt.tz_localize(None)
        # 只保留请求日期当日 bar（count=240 在数据不足一日时可能回溢到前一日）
        mask = (t.dt.strftime("%Y-%m-%d") == str(date)).values
        sub = df.iloc[mask].copy()
        sub["time"] = t.values[mask]
        sub["open"] = sub["open"].astype(float)
        sub["high"] = sub["high"].astype(float)
        sub["low"] = sub["low"].astype(float)
        sub["close"] = sub["close"].astype(float)
        sub["volume"] = sub["volume"].astype(float) / 100.0  # 股 → 手
        sub["amount"] = sub["amount"].astype(float)
        out = sub[["time", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
        return out.sort_values("time").reset_index(drop=True)

    # ---------- 实时快照 ----------
    def snapshot(self, codes: list) -> dict:
        gma = self._gma
        if not codes:
            return {}
        rows = gma.current([to_gm(c) for c in codes]) or []
        out = {}
        for r in rows:
            try:
                px = float(r.get("price") or 0)
                if px <= 0:
                    continue
                sym = str(r.get("symbol") or "")
                code = sym.split(".")[-1] if "." in sym else sym
                ts = r.get("created_at")
                ts_date = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else None
                out[code] = {
                    "price": px,
                    "open": float(r.get("open") or 0),
                    "high": float(r.get("high") or 0),
                    "low": float(r.get("low") or 0),
                    "volume": float(r.get("cum_volume") or 0) / 100.0,  # 股 → 手
                    "ts_date": ts_date,
                }
            except Exception:
                continue
        return out

    # ---------- 指数日线 ----------
    def index_daily(self, index: str = "sh000001", days: int = 800) -> pd.DataFrame:
        gma = self._gma
        df = gma.history_n(symbol=_gm_index_symbol(index), frequency="1d", count=days,
                           end_time=datetime.now().strftime("%Y-%m-%d"),
                           fields="eob,close", adjust=gma.ADJUST_PREV, df=True)
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "close"])
        out = pd.DataFrame({
            "date": pd.to_datetime(df["eob"]).dt.strftime("%Y-%m-%d"),
            "close": df["close"].astype(float),
        })
        return out.sort_values("date").reset_index(drop=True)
