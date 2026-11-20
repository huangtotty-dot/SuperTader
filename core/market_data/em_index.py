# -*- coding: utf-8 -*-
"""东方财富 · 指数日线兜底（2026-11-18）。

**为什么需要它**：腾讯 `fqkline`（`ifzq` 与 `web.ifzq` 两个主机）2026-11-18 起对
`/appstock/app/fqkline/get` 统一回 **HTTP 501（WAF 拦截页）**，而金矿（GM）终端会间歇性
`1001 无法连接到终端服务` ⇒ 指数日线**两个源同时不可得**：
  · 「盘中状态」成交量图整卡报错「指数日线不可得(sh000001)」；
  · `daily_kline_raw`（K 线弹窗当日 forming 根）同源，同样拿不到。

东财 `push2his` 的指数日线仍正常，且**带真实成交额**（`f57`，元），口径足够直接喂
`_hs_daily_from_frames` 的量级校验（上证单腿 ~6.5e11）。

返回 DataFrame 列：[date, open, high, low, close, volume, amount]；失败返回空 DataFrame（不抛）。
`volume` 为东财原样（手），与腾讯指数口径一致；`amount` 为元。
"""
from __future__ import annotations

import json

import pandas as pd
import requests

_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _secid(sym: str) -> str:
    """sh000001 → 1.000001；sz399106 → 0.399106（东财：1=沪、0=深/北）。"""
    s = str(sym).lower()
    if s.startswith("sh"):
        return "1." + s[2:]
    if s.startswith(("sz", "bj")):
        return "0." + s[2:]
    return ""


def index_daily_em(sym: str, days: int = 310, end_date: str | None = None,
                   timeout: float = 10.0) -> pd.DataFrame:
    """东财指数日线（含成交额）。失败/空 → 空 DataFrame，绝不抛。"""
    secid = _secid(sym)
    if not secid:
        return pd.DataFrame(columns=_COLS)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",                       # 日线
        "fqt": "1",
        "end": str(end_date).replace("-", "") if end_date else "20500101",
        "lmt": str(max(int(days), 30)),
    }
    try:
        r = requests.get(_URL, params=params, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200 or not r.content:
            return pd.DataFrame(columns=_COLS)
        data = json.loads(r.content.decode("utf-8", errors="replace"))
        klines = ((data or {}).get("data") or {}).get("klines") or []
    except Exception:
        return pd.DataFrame(columns=_COLS)
    rows = []
    for line in klines:
        p = str(line).split(",")
        if len(p) < 7:
            continue
        try:
            rows.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                         "high": float(p[3]), "low": float(p[4]),
                         "volume": float(p[5] or 0), "amount": float(p[6] or 0)})
        except Exception:
            continue
    if not rows:
        return pd.DataFrame(columns=_COLS)
    df = pd.DataFrame(rows, columns=_COLS)
    if end_date:
        df = df[df["date"] <= str(end_date)]
    df.attrs["source"] = "eastmoney"
    return df.reset_index(drop=True)
