# -*- coding: utf-8 -*-
"""持仓实时 VWAP 快照缓存（2026-11-13 从 main.py 抽出）。

⚠️ 共享状态注意：`_SPOT_VWAP_CACHE` 会被 `_refresh_spot_vwap_cache` **重绑定**（`= cache`），
而 main.py 的 scan_once 仍要读它 ⇒ main.py 必须用**模块属性**方式读（`_sv._SPOT_VWAP_CACHE`），
不能用 `from ... import`（那是快照）。`_LAST_SPOT_VWAP_REFRESH` 只被本函数读/写，是专属全局。
"""
from __future__ import annotations

import time
from typing import Dict

from config import HOLDINGS, log
from core.data_fetcher import clean_code

_SPOT_VWAP_CACHE: Dict[str, float] = {}  # code -> 实时 VWAP
_LAST_SPOT_VWAP_REFRESH = 0.0


def _refresh_spot_vwap_cache() -> None:
    """刷**持仓股**的实时 VWAP（成交额 ÷ 成交量，交易所官方口径）。

    2026-10-16 owner 拍板 A 方案：数据源由 **akshare 全市场快照** 换成 **腾讯逐只快照**。

    **为什么必须换**：akshare `stock_zh_a_spot_em()` 要翻 **59 页**，实测
    **97.7s + import akshare 11.2s ≈ 109s/次**；而本函数在**逐只持仓循环里**被调、
    节流阈值只有 **60s < 109s** ⇒ **节流形同虚设，每只持仓重拉一次**（7 只 ≈ 763s）
    —— 这正是 main.py 主循环一轮 ~15 分钟的来源（逐阶段计时实测 `I0持仓循环(全部) 均941s`）。
    腾讯侧走 `snapshot_auction_many`（同日已改 keep-alive 连接复用）：**持仓 7 只 <1s**，
    且**新鲜度更好** —— 同一轮内各持仓共享同一份快照，而不是散布在 13 分钟里。

    P2-13(2026-10-23)：快照来源再换 **QuoteBus**（`core.market_data.quote_bus.get_quotes`）——
    跨进程共享档 + 文件锁 + stale 兜底；新鲜（<8s）命中零网络，过期才由总线经腾讯
    `snapshot_auction_many` 补齐。本地 60s 节流与「整批取不到保留旧缓存」语义不变。

    ⚠️ **缓存只装持仓**：全仓唯一的消费点是 main.py scan_once 里的
    `_sv._SPOT_VWAP_CACHE.get(api_code)`（逐只持仓），原来装全市场 5000 只是白装。
    **若将来出现查非持仓码的消费方，必须改回取数口径。**

    ⚠️ 成交量单位按板块不同（科创板是"股"、其余是"手"，差 100 倍）——
    统一交给 `tencent_provider.qt_vwap` 处理（用"VWAP 必落在当日 [low,high] 内"判），
    **别在这里自己算**。
    """
    global _LAST_SPOT_VWAP_REFRESH, _SPOT_VWAP_CACHE
    now_ts = time.time()
    if now_ts - _LAST_SPOT_VWAP_REFRESH < 60:
        return
    _LAST_SPOT_VWAP_REFRESH = now_ts
    try:
        _codes = []
        for _c in (HOLDINGS or {}):
            _b = clean_code(_c)
            if _b and _b not in _codes:
                _codes.append(_b)
        if not _codes:
            return
        from core.market_data.tencent_provider import qt_vwap
        from core.market_data.quote_bus import get_quotes as _bus_get_quotes
        snaps = _bus_get_quotes(_codes, max_age_sec=8.0) or {}
        if not snaps:
            # 与旧实现同语义：**整批取不到就保留旧缓存**（旧版 `spot is None/empty → return`），
            # 别把好缓存清成空、让引擎退回分钟自算口径。
            return
        cache = {}
        for _c in _codes:
            _v = qt_vwap(snaps.get(_c) or {})
            if _v is not None and _v > 0:
                cache[_c] = _v
        _SPOT_VWAP_CACHE = cache
        log.debug(f"📡 VWAP 实时缓存刷新: {len(cache)}/{len(_codes)} 只（腾讯逐只）")
    except Exception as e:
        log.debug(f"VWAP 缓存刷新失败: {str(e)[:80]}")
