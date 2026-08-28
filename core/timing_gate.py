# -*- coding: utf-8 -*-
"""
timing_gate.py — 建仓/加仓时机判定（2026-08-15 新增，regime 条件化）

基于 W34 建仓/加仓时机实验（17863 行两时段合并，日线800天）：
  市场状态决定最佳入场规则——
  · 多头趋势（指数>MA60）：追强 = 个股多头结构 + 浅回撤(≥-3%) [+MACD金叉加分] → fwd5 3.24%
  · 空头趋势（指数<MA60）：抄底超跌 = 深回撤(<-10%) → fwd5 3.37%
  · 条件不满足 → NO-GO（降频，避免无脑进场）

无未来函数：全部用截止 date_str 的日线数据。配置见 config.ENTRY_TIMING_PARAMS。

用法：
    from timing_gate import timing_verdict
    v = timing_verdict("000988", "2026-08-14")
CLI：
    python timing_gate.py --code 000988 --date 2026-08-14
"""
import argparse
import json
import os
import sys
import urllib.request as _ur
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[1]  # 项目根（本模块位于 core/ 下）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.position_builder import fetch_daily_kline  # noqa: E402
from core import build_decision as _bd  # noqa: E402  P3 同源：决策核单一真源

try:
    from config import ENTRY_TIMING_PARAMS as _ETP
except Exception:
    _ETP = {"enabled": True, "regime_ma60": True}

INDEX_CACHE = BASE / "t_io" / "cache" / "daily_kline" / "index_sh000001.json"

# B-1(2026-08-21): 指数缓存日期校验——重拉失败回退旧缓存时置 True，随 _regime 返回供 trace 标记
_STALE = {"index_cache_stale": False}


def _params() -> dict:
    try:
        from config import ENTRY_TIMING_PARAMS
        return ENTRY_TIMING_PARAMS or _ETP
    except Exception:
        return _ETP


def _fetch_index_daily():
    """上证指数日线（前复权 800天，缓存）。返回 DataFrame(date, close)。

    P1-2 收敛：改走 core/market_data provider（gm 主源，腾讯兜底，多主机 WAF 兜底在 provider 内）。
    B-1(2026-08-21) 语义保留：盘中(工作日≥09:15)缓存缺今日 → 视为陈旧并置 index_cache_stale=True，
    避免"昨日收盘冒充今日"误判 regime（08-18 根因）。
    """
    from core.market_data import get_provider
    df = get_provider().index_daily("sh000001", 800)
    _STALE["index_cache_stale"] = False
    if df is None or df.empty:
        # 审核 #13: 指数日线全失败恢复抛错（原行为；此前静默返回空 → regime 静默退化）
        _STALE["index_cache_stale"] = True
        raise ValueError("上证指数日线获取失败（gm/腾讯均不可用）")
    _now = datetime.now()
    # B-1 语义：仅盘中(工作日 09:15-15:00)才按"缺今日"标陈旧；盘前用昨收为参考，不算陈旧
    if _now.weekday() < 5 and "09:15" <= _now.strftime("%H:%M") <= "15:00":
        _last = str(df["date"].iloc[-1])
        if _last < _now.strftime("%Y-%m-%d"):
            _STALE["index_cache_stale"] = True
    return df


def _regime(date_str: str) -> dict:
    """指数 vs MA60 的市场状态（多头趋势/空头趋势/震荡），无未来函数。

    B-3(2026-08-21): 多头加缓冲带 close>MA60*regime_up_buffer(默认1.005) 才 trend_up，
    中间带(MA60*0.97 ~ MA60*1.005)归 range，防 08-17/18/19 razor 横跳。
    B-1: 返回 index_cache_stale 标记供 trace。
    P3 同源：判定逻辑委托 core/build_decision.regime_from_index_daily（单一真源）。
    """
    p = _params()
    idx = _fetch_index_daily()
    r = _bd.regime_from_index_daily(idx, date_str, p)
    r["index_cache_stale"] = bool(_STALE["index_cache_stale"])
    return r


def _stock_features(code: str, date_str: str) -> dict:
    """个股日线时机特征（截止 date_str，无未来）。
    P3 同源：特征计算委托 core/build_decision.features_from_daily（单一真源）。"""
    df = fetch_daily_kline(code)
    if df.empty:
        return {}
    return _bd.features_from_daily(df, date_str)


def timing_verdict(code: str, date_str: str = None) -> dict:
    """建仓/加仓时机判定。返回 {go, regime, reason, features, veto, index}。"""
    date_str = date_str or pd.Timestamp.now().strftime("%Y-%m-%d")
    p = _params()
    r = _regime(date_str)
    f = _stock_features(code, date_str)
    # 2026-08-28: 指数具体点位随结果带出（GUI/卡点显示"站上多少转多头、跌破多少转空头"）
    _idx_info = {"close": r.get("close"), "ma60": r.get("ma60"), "ratio": r.get("ratio")}
    if r.get("close") and r.get("ma60"):
        _up_buf = float(p.get("regime_up_buffer", 1.005))
        _idx_info["up_line"] = round(r["ma60"] * _up_buf, 1)   # 站上即转多头（与 _regime 同口径）
        _idx_info["dn_line"] = round(r["ma60"] * 0.97, 1)      # 跌破即转空头（与 _regime 同口径）
    if not f:
        return {"go": False, "regime": r["regime"], "reason": "日线不足", "features": f,
                "veto": [], "index": _idx_info}
    regime = r["regime"]
    # P3 同源：决策（含否决因子）委托 core/build_decision.timing_decision（单一真源）。
    # 注：金叉不参与 go（P1 2026-08-25 修复语义保留），仅体现在 composite_score 加分。
    _dec = _bd.timing_decision(f, regime, p)
    return {"go": _dec["go"], "regime": regime, "reason": "；".join(_dec["reasons"]), "features": f,
            "veto": _dec["veto"], "index": _idx_info}


def _cli():
    ap = argparse.ArgumentParser(description="建仓/加仓时机判定")
    ap.add_argument("--code", required=True)
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    v = timing_verdict(args.code, args.date)
    f = v["features"]
    print(f"{args.code} @ {args.date or 'today'}")
    print(f"  市场状态: {v['regime']} ｜ 时机判定: {'✅ GO 可进场' if v['go'] else '🚫 NO-GO 降频'}")
    if f:
        print(f"  个股: 价{f.get('price')} 多头{f.get('trend_multihead')} "
              f"回撤{f.get('drawdown'):+.1%} 金叉{f.get('macd_golden_5d')}")
    print(f"  理由: {v['reason']}")


if __name__ == "__main__":
    _cli()
