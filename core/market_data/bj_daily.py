# -*- coding: utf-8 -*-
"""北交所日线取数器（2026-09-29）。

**背景：北交所日线在当前环境没有可用的主源**（2026-09-29 实测）：

  - 腾讯 ifzq（`appstock/app/fqkline/get`）：`bj430047` 返回 `day: []` 且**连 `qfqday` 键都没有**；
    40 只抽样 **0/40**。注意这与「腾讯不支持北交所」不同——`qt.gtimg.cn`（报价）**支持**，
    只有 K 线主机不支持。
  - GM：`BJSE.` / `BSE.` / `BJ.` 三种前缀 `history_n` **全 0 行**。
  - 东财 `push2his`（K线主机）：本机**整体不可达**——对照组平安银行 `0.000001` 同样
    `RemoteDisconnected`，已排除代理因素（无 proxy 环境变量，无代理 opener 亦失败）。
  - akshare `stock_zh_a_hist`：同族连接失败（与上同因）。

故本模块做成**可插拔取数器**：唯一候选通路走东财 `push2his` + **8 次重试**
（仓库既有风控对策：「push2his 间歇断连(风控)，重试 8 次」）。

**失败不抛**：返回空 DataFrame，由调用方**跳过该股并计数**（不静默、不污染 GM→腾讯主链）。
将来 GM 或腾讯补上北交所 K 线后，只需替换本文件实现，调用方无需改动。
"""
import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

log = logging.getLogger("market_data.bj_daily")

_COLS = ["date", "open", "high", "low", "close", "volume"]
_HOST = "push2his.eastmoney.com"
_RETRIES = 8
_RETRY_SLEEP = 0.8

#: 最近一次失败原因（供数据健康/测试观测；成功时置 None）
LAST_ERROR = None
#: 本进程内失败次数（调用方据此在健康里如实汇报"北交所数据不可得"）
FAIL_COUNT = 0


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=_COLS)


def _secid(code: str) -> str:
    """北交所 secid：东财对北交所用 `0.` 市场前缀（与深市同段）。"""
    return "0." + str(code).split("_")[0]


def _kline_url(code: str, days: int) -> str:
    # klt=101 日线；fqt=1 前复权（与 GmProvider 的 ADJUST_PREV 口径一致）
    beg = (datetime.now() - timedelta(days=int(days * 1.6) + 30)).strftime("%Y%m%d")
    return (f"https://{_HOST}/api/qt/stock/kline/get?secid={_secid(code)}"
            f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1"
            f"&beg={beg}&end=20500101")


def _parse(raw: str) -> pd.DataFrame:
    """东财 klines → 标准日线帧。东财 f56 成交量单位为**手**（与腾讯 K 线一致，
    故不像 gm 那样 ÷100）。"""
    d = json.loads(raw)
    kl = ((d.get("data") or {}).get("klines")) or []
    rows = []
    for line in kl:
        p = str(line).split(",")
        if len(p) < 6:
            continue
        try:
            rows.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                         "high": float(p[3]), "low": float(p[4]), "volume": float(p[5])})
        except (TypeError, ValueError):
            continue
    if not rows:
        return _empty()
    return pd.DataFrame(rows)[_COLS].sort_values("date").reset_index(drop=True)


def fetch_bj_daily(code: str, days: int = 250) -> pd.DataFrame:
    """北交所日线。**永不抛**；取不到返回空帧并记 LAST_ERROR/FAIL_COUNT。"""
    global LAST_ERROR, FAIL_COUNT
    url = _kline_url(code, days)
    last = None
    for _ in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://quote.eastmoney.com/",
            })
            raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
            df = _parse(raw)
            if not df.empty:
                LAST_ERROR = None
                return df
            last = "空响应"
        except Exception as e:            # noqa: BLE001 —— 故意吞掉，降级而非崩溃
            last = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(_RETRY_SLEEP)
    FAIL_COUNT += 1
    LAST_ERROR = last
    log.warning("北交所日线不可得(%s)：%s（已重试 %d 次，跳过该股）", code, last, _RETRIES)
    return _empty()
