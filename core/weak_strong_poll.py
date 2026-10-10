# -*- coding: utf-8 -*-
"""弱转强 · 轮询/预筛胶水（2026-10-10 施工，WS3 轮询优化）。

**关键优化（owner 2026-10-10 强调「轮询速度是硬约束」）**：绝不逐股轮询。两层漏斗把
10:00 要看的股票从全池 ~5000 砍到几十只：

  盘前 09:25~09:55（每天一次，全池一次批量拉日线）
    「前一日大涨(≥9.5%)家数」top-N 板块（exp15 定稿的涨停数替身，**不用**
    daily_summary.json 的排名）→ 只留板块内**超跌候选**（昨收 < MA20）
    → 得 {code: prev_close/ma20/board/name}。
  10:00（一次性，单次批量调用）
    超跌候选一次批量拉 `1800s`（GM `history` list form，~1 次调用覆盖全部候选）
    → 10:00 棒 {code: low/close/amount/volume} → 交 `core/weak_strong.evaluate`。

**纯逻辑 + 注入数据源**：不 import gm.api（分钟取数由 gm_main 注入 `fetch_fn`），
日线走 facade（与 hunter_ma5_alert 同款），故可离线单测。
"""
from __future__ import annotations

import json
import os

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WL_FP = os.path.join(_BASE, "stock_hunter", "watchlist_jiuyan.json")

MERGES = {"创新药": "医药", "中药": "医药"}
_BATCH = 200          # daily_many 分片（对齐 hunter_ma5_alert）
_DAYS = 40            # 日线根数（MA20 预热 + 裕度）


def sym2board_map() -> dict:
    """6位代码 -> 主板块（第一个韭研分类，含 merge；与 exp12 同口径，单一真源）。"""
    try:
        with open(_WL_FP, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    m = {}
    for code, info in (data.items() if isinstance(data, dict) else []):
        if not isinstance(info, dict) or not str(code).isdigit():
            continue
        cats = []
        for i in range(1, 10):
            c = str(info.get(f"jiuyan_category{i}", "") or "").strip()
            if c:
                cats.append(MERGES.get(c, c))
        if not cats:
            c = str(info.get("jiuyan_category", "") or "").strip()
            cats = [MERGES.get(cc.strip(), cc.strip()) for cc in c.split("|") if cc.strip()]
        m[str(code)] = cats[0] if cats else ""
    return m


_LU_THRESHOLD = 0.095   # 前一日涨幅 ≥9.5% 计涨停（对齐 exp12 lu_known 替身口径）


def _codes_with_board(board_map: dict) -> list:
    """有韭研分类主板块的 6 位码列表（供全池批量取日线）。"""
    return [c for c in board_map if board_map.get(c)]


def premarket_candidates(top_n: int, date: str = None, provider=None) -> dict:
    """盘前超跌候选 → {code: {'prev_close','ma20','board','name'}}。

    **板块口径（exp15 全池复验定稿，2026-10-10）**：取「前一日大涨(≥9.5%)家数」top-N 板块
    （对齐 exp12 `lu_known` 替身），**不用** `daily_summary.json` 的排名——exp15 里平均分与
    涨停数两种真实排名在 top-4 组合都塌成负 Sharpe，只有从日线 bar 现算的涨停家数替身通过。

    实现：全池一次批量拉日线(40 根)，同批算 ①板块涨停家数排名 ②超跌(昨收<MA20) 预筛，
    只留 top-N 板块内超跌票。取不到日线/板块无分类的票自动缺省跳过。
    """
    from core.market_data import facade
    from collections import Counter
    if provider is None:
        try:
            provider = facade.get_provider()
        except Exception:
            return {}
    board_map = sym2board_map()
    codes = _codes_with_board(board_map)
    if not codes:
        return {}
    frames = {}
    for i in range(0, len(codes), _BATCH):
        try:
            frames.update(provider.daily_many(codes[i:i + _BATCH], days=_DAYS) or {})
        except Exception:
            pass

    # ① 前一日大涨家数 → 板块排名（复用各票 past 收盘序列）
    lu = Counter()
    pasts = {}
    for c in codes:
        fr = frames.get(c)
        if fr is None or getattr(fr, "empty", True):
            continue
        try:
            dates = fr["date"].astype(str)
            closes = fr["close"].astype(float)
        except Exception:
            continue
        # 昨收口径：取 date < 今日（盘中 forming bar 不参与）的最后一根完整日线收盘
        if date:
            past = closes[dates < date]
        else:
            from datetime import datetime
            past = closes[dates < datetime.now().strftime("%Y-%m-%d")]
        if len(past) < 2:
            continue
        pasts[c] = past
        if float(past.iloc[-1]) / float(past.iloc[-2]) - 1 >= _LU_THRESHOLD:
            lu[board_map[c]] += 1
    hot = {b for b, n in lu.most_common(int(top_n)) if n > 0}
    if not hot:
        return {}

    # ② 只留 top-N 板块内「昨收 < MA20」的超跌票（弱转强 weak_W1 预筛）
    out = {}
    for c in codes:
        if board_map.get(c) not in hot:
            continue
        past = pasts.get(c)
        if past is None or len(past) < 20:
            continue
        prev_close = float(past.iloc[-1])
        ma20 = float(past.tail(20).mean())
        if not (prev_close > 0 and prev_close < ma20):
            continue
        out[c] = {"prev_close": prev_close, "ma20": ma20,
                  "board": board_map[c], "name": ""}
    return out


def ten_am_bars(codes, now, fetch_fn) -> dict:
    """10:00 棒 → {code: {'low','close','amount','volume'}}。

    `fetch_fn(codes, now)` 由 gm_main 注入（GM `history` list form，frequency="1800s"，
    起止=今日，取 10:00 那一根）。本函数只做**防御式抽取**：缺关键价的票不入选。
    """
    out = {}
    try:
        raw = fetch_fn(list(codes), now)
    except Exception:
        return {}
    for c in list(codes):
        try:
            b = raw.get(c)
            if not b:
                continue
            low = float(b.get("low") or 0)
            close = float(b.get("close") or 0)
            amount = float(b.get("amount") or 0)
            volume = float(b.get("volume") or 0)
            if not (low > 0 and close > 0):
                continue
            out[c] = {"low": low, "close": close, "amount": amount, "volume": volume}
        except Exception:
            continue
    return out


def split_into_prev_close_ma20(candidates: dict) -> tuple[dict, dict]:
    """把 premarket_candidates 的输出拆成 evaluate 要的 {code:prev_close}/{code:ma20}。"""
    pc, ma = {}, {}
    for c, v in (candidates or {}).items():
        pc[c] = v.get("prev_close")
        ma[c] = v.get("ma20")
    return pc, ma
