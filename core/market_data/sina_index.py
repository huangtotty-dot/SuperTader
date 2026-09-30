# -*- coding: utf-8 -*-
"""新浪指数 K 线取数（2026-09-30）。

**为什么需要它**：北交所成交额在本机其余源**全不可得** ——
  · GM：无北交所数据（`BJSE.*` 全 0 行；`bj899050` 只回 1 根垃圾行）
  · 腾讯：北证50 K线只回 **1 根**（拿不到昨日）
  · 东财 / akshare：风控整体不可达
而新浪 `CN_MarketDataService.getKLineData` 给 `bj899050` 5 分钟线，覆盖多日**且带 amount**。

**三方核对（2026-09-30）**：新浪 5 分钟当日合计 **122.4 亿** ＝ 腾讯实时报价 122.4 亿
＝ 逐只北交所个股求和 122.5 亿 ⇒ 新浪的 北证50 amount 是**全北交所**成交额，
**不是** 50 只成分股。且 `上证 + 深综 + 北证50` 与同花顺「成交额总计」两天都只差 2 亿（0.01%）。

⚠️ 本模块只做「取数 + 归一列」，**不抛**：失败返回空 DataFrame，由调用方降级。
"""
import json
import logging
import urllib.request

import pandas as pd

log = logging.getLogger("market_data.sina_index")

_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
        "CN_MarketDataService.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={n}")

#: 最近一次失败原因（供数据健康/测试观测；成功置 None）
LAST_ERROR = None


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])


def parse_jsonp(raw: str) -> pd.DataFrame:
    """新浪 jsonp 响应 → 帧（纯函数，离线可测）。非法/空 → 空帧。

    字段名固定 `day/open/high/low/close/volume[/amount]`，`day` 重命名为 `time`。
    """
    i, j = raw.find("("), raw.rfind(")")
    if i < 0 or j <= i:
        return _empty()
    try:
        arr = json.loads(raw[i + 1:j])
    except Exception:
        return _empty()
    if not arr:
        return _empty()
    out = pd.DataFrame(arr).rename(columns={"day": "time"})
    if "time" not in out.columns:
        return _empty()
    out["time"] = pd.to_datetime(out["time"], errors="coerce")
    out = out.dropna(subset=["time"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    # 5 分钟线有时**不带 amount** ⇒ 补 0，让调用方口径统一（不参与加总即无影响）
    if "amount" not in out.columns:
        out["amount"] = 0.0
    return out.sort_values("time").reset_index(drop=True)


def fetch_index_minutes(symbol: str, scale: int = 5, datalen: int = 400) -> pd.DataFrame:
    """新浪指数 K 线 → 与 `GmProvider.index_minute` 同形的帧（time/open/high/low/close/volume/amount）。

    `scale` 为分钟数（5=5分钟，240=日线）。**永不抛**；失败返回空帧并记 `LAST_ERROR`。
    """
    global LAST_ERROR
    url = _URL.format(sym=symbol, scale=int(scale), n=int(datalen))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="replace")
        out = parse_jsonp(raw)
        if out.empty:
            LAST_ERROR = "响应不可解析或为空"
            log.warning("新浪指数K线响应异常(%s)：%s", symbol, raw[:80])
            return out
        LAST_ERROR = None
        return out
    except Exception as e:                      # noqa: BLE001 —— 故意吞掉，降级而非崩溃
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:80]}"
        log.warning("新浪指数K线取数失败(%s)：%s", symbol, LAST_ERROR)
        return _empty()
