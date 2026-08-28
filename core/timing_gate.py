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
        _STALE["index_cache_stale"] = True
        return df
    _now = datetime.now()
    if _now.weekday() < 5 and _now.strftime("%H:%M") >= "09:15":
        _last = str(df["date"].iloc[-1])
        if _last < _now.strftime("%Y-%m-%d"):
            _STALE["index_cache_stale"] = True
    return df


def _regime(date_str: str) -> dict:
    """指数 vs MA60 的市场状态（多头趋势/空头趋势/震荡），无未来函数。

    B-3(2026-08-21): 多头加缓冲带 close>MA60*regime_up_buffer(默认1.005) 才 trend_up，
    中间带(MA60*0.97 ~ MA60*1.005)归 range，防 08-17/18/19 razor 横跳。
    B-1: 返回 index_cache_stale 标记供 trace。
    """
    p = _params()
    idx = _fetch_index_daily()
    idx = idx[idx["date"].astype(str) <= str(date_str)]
    if len(idx) < 61:
        return {"regime": "unknown", "close": None, "ma60": None,
                "index_cache_stale": bool(_STALE["index_cache_stale"])}
    close = float(idx["close"].iloc[-1])
    ma60 = float(idx["close"].astype(float).rolling(60).mean().iloc[-1])
    up_buffer = float(p.get("regime_up_buffer", 1.005))
    if close > ma60 * up_buffer:
        regime = "trend_up"
    elif close < ma60 * 0.97:
        regime = "trend_dn"
    else:
        regime = "range"
    return {"regime": regime, "close": close, "ma60": round(ma60, 3),
            "ratio": round(close / ma60, 4), "index_cache_stale": bool(_STALE["index_cache_stale"])}


def _stock_features(code: str, date_str: str) -> dict:
    """个股日线时机特征（截止 date_str，无未来）。"""
    df = fetch_daily_kline(code)
    if df.empty:
        return {}
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    sub = df[df["date"] <= str(date_str)]
    if len(sub) < 61:
        return {}
    c = sub["close"].astype(float)
    h = sub["high"].astype(float)
    price = float(c.iloc[-1])
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma60 = float(c.rolling(60).mean().iloc[-1])
    rec_high = float(h.tail(20).max())
    # MACD 金叉（近5日）
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False).mean()
    golden = bool(((dif > dea) & (dif.shift(1) <= dea.shift(1))).tail(5).any())
    # RSI(14)（空头抄底超卖极值用）
    _delta = c.diff()
    _gain = _delta.clip(lower=0).rolling(14).mean()
    _loss = (-_delta.clip(upper=0)).rolling(14).mean()
    _rsi = float((100 - 100 / (1 + _gain / _loss.replace(0, float("nan")))).iloc[-1]) if _loss.iloc[-1] and _loss.iloc[-1] > 0 else 50.0
    # 2026-08-27 因子挖掘（127万股票-日）：两个否决因子的特征
    #   vol_ratio20≥3 爆量当日 w5=38.3%/r5=-1.0%（毒药桶）；dist_ma60>+20% w10=43.6%（追高最差档）
    _vol_ratio20 = None
    if "volume" in sub.columns:
        _v = pd.to_numeric(sub["volume"], errors="coerce")
        _v20 = float(_v.rolling(20).mean().iloc[-1]) if len(_v) >= 20 else float("nan")
        if _v20 and _v20 > 0:
            _vol_ratio20 = round(float(_v.iloc[-1]) / _v20, 2)
    return {
        "price": round(price, 3),
        "trend_multihead": bool(price > ma20 and price > ma60),
        "above_ma60": bool(price > ma60),
        "drawdown": round(price / rec_high - 1, 4) if rec_high > 0 else 0.0,
        "macd_golden_5d": golden,
        "rsi": round(_rsi, 1),
        "ma20": round(ma20, 3), "ma60": round(ma60, 3),
        "vol_ratio20": _vol_ratio20,
        "dist_ma60": round(price / ma60 - 1, 4),
    }


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
    reasons = []
    vetoes = []
    if regime == "trend_up":
        # 多头趋势 → 追强
        # 2026-08-27 因子挖掘：两个硬否决（仅追强侧；抄底侧爆量是恐慌出清常态，不否决）
        _vol_max = float(p.get("veto_vol_spike", 3.0))
        _dist_max = float(p.get("veto_dist_ma60_max", 0.20))
        _vr = f.get("vol_ratio20")
        if _vr is not None and _vr >= _vol_max:
            vetoes.append(f"爆量{_vr:g}倍≥{_vol_max:g}")
        _dm = f.get("dist_ma60")
        if _dm is not None and _dm > _dist_max:
            vetoes.append(f"偏离MA60{_dm:+.1%}>{_dist_max:+.0%}")
        cond = f["trend_multihead"] and f["drawdown"] >= -0.03 and not vetoes
        reasons.append(f"多头趋势: 追强(多头{'✓' if f['trend_multihead'] else '✗'}+浅回撤{'✓' if f['drawdown']>=-0.03 else '✗'})")
        if vetoes:
            reasons.append(f"否决: {'、'.join(vetoes)}")
        if f["macd_golden_5d"]:
            reasons.append("MACD金叉近5日 ✓（加分）")
        # P1(2026-08-25): 原写法 `cond and (macd_golden_5d or True)`——`or True` 恒真，
        #   金叉对 go 零影响，是误导性死逻辑（既非必要项也未真正加分，只进 score 那 10 分，
        #   而 score 不参与 verdict）。此处明确：追强 go 判据 = 多头结构 + 浅回撤，金叉不参与 go。
        #   金叉的加分仅体现在 position_builder 的 composite_score，语义与此一致。
        go = cond
    elif regime == "trend_dn":
        # 空头趋势 → 抄底超跌极值（2026-08-16 实验：深回撤 + RSI<20 深度超卖，样本内/外一致提升）
        _rsi_lim = float(p.get("trend_dn_rsi_max", 20))
        _dd_ok = f["drawdown"] < -0.10
        _rsi_ok = (f.get("rsi") or 50) < _rsi_lim
        cond = _dd_ok and _rsi_ok
        reasons.append(f"空头趋势: 抄底(深回撤{'✓' if _dd_ok else '✗'} + RSI极值{'✓' if _rsi_ok else '✗'} "
                       f"rsi={f.get('rsi')} drawdown={f['drawdown']:.1%})")
        go = cond
    else:
        # 震荡 → 降频
        go = False
        reasons.append("震荡市: 降频，暂不建仓/加仓")
    return {"go": bool(go), "regime": regime, "reason": "；".join(reasons), "features": f,
            "veto": vetoes, "index": _idx_info}


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
