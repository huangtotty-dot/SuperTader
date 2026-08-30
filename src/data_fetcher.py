# === V3.0: 显式导入，替代 exec() 共享命名空间 ===
import numpy as np
import pandas as pd
import json
import os
import time
import logging
import urllib.request
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, time as dtime
from dataclasses import dataclass, field

try:
    from config import PARAMS, STOCK_PARAMS, CACHE_DIR
except ImportError:
    PARAMS = {}; STOCK_PARAMS = {}; CACHE_DIR = "./cache"

def clean_code(code: str) -> str:
    """去除 _A/_B 等账户后缀，返回纯数字代码供数据接口使用"""
    if not code:
        return ""
    if "_" in code:
        return code.split("_")[0]
    return code


def _fnum(v, default: float = 0.0) -> float:
    """fix D12: NaN 安全数值转换。NaN 是真值，`x or 0.0` 拦不住 NaN，
    裸 float(nan) 写进 dict 会让 json.dump 产出非法 JSON。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
        return default
    return f


def _fetch_daily_bar_tencent(api_code: str, count: int = 400) -> pd.DataFrame:
    """fix D4: 前复权日线。P1-2 收敛：改走 core/market_data provider（gm 主源，腾讯兜底）。
    ETF 日线主用此链路；个股 akshare 失败时也作兜底。返回 date/open/high/low/close/volume。"""
    from core.market_data import get_provider
    return get_provider().daily(api_code, count)


def _fetch_daily_bar(code: str, is_etf: bool = False, as_of: Optional[str] = None) -> tuple:
    """拉取日线，返回 (DataFrame, 失败原因)。df 非空时原因为空串。

    fix D4: ETF 日线改走腾讯 fqkline 主链路，删除签名错误的 sina 死兜底；
    失败原因不再静默吞掉，由调用方写入 daily_status/daily_reason。
    fix D12: 回溯窗口 180→400 天，使 MA120/MA250 有足够样本。"""
    api_code = clean_code(code)
    errors: List[str] = []
    df = pd.DataFrame()
    end_date = (as_of or _now().strftime("%Y%m%d")).replace("-", "")
    start_date = (_now() - timedelta(days=400)).strftime("%Y%m%d")
    if is_etf:
        try:
            df = _fetch_daily_bar_tencent(api_code)
        except Exception as e:
            errors.append(f"tencent:{str(e)[:60]}")
            df = pd.DataFrame()
        if df.empty:
            try:
                import akshare as ak
                df = ak.fund_etf_hist_em(symbol=api_code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
            except Exception as e:
                errors.append(f"akshare_em:{str(e)[:60]}")
                df = pd.DataFrame()
    else:
        try:
            import akshare as ak
            df = ak.stock_zh_a_hist(symbol=api_code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        except Exception as e:
            errors.append(f"akshare:{str(e)[:60]}")
            df = pd.DataFrame()
        if df is None or df.empty:
            try:
                df = _fetch_daily_bar_tencent(api_code)
            except Exception as e:
                errors.append(f"tencent:{str(e)[:60]}")
                df = pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame(), ";".join(errors) or "日线接口均无数据"
    try:
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame(), f"日线字段缺失({','.join(df.columns)[:60]})"
        df = df[keep_cols].copy()
        df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date").reset_index(drop=True)
        # 腾讯链路无 end 参数，as_of 截断统一在此做
        if as_of:
            cut = str(as_of)[:10]
            df = df[df["date"] <= cut].reset_index(drop=True)
        if df.empty:
            return pd.DataFrame(), "as_of 截断后无日线数据"
        return df, ""
    except Exception as e:
        return pd.DataFrame(), f"日线清洗异常:{str(e)[:60]}"


def _build_daily_context_from_df(code: str, df: pd.DataFrame, current_price: float = 0.0, intraday_asof: Optional[str] = None) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < PARAMS["daily_context_min_rows"]:
        return _default_daily_context(code, status="insufficient", reason=f"日线数据不足({0 if df is None else len(df)})")
    try:
        work = df.copy()
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce")
        work = work.dropna(subset=["date", "open", "close", "high", "low"]).reset_index(drop=True)
        # fix P1-9: 盘中口径剔除当日未完成日线 bar，MA 与 daily_prev_* 一律基于昨日完整数据；
        # intraday_asof 为空（盘后口径）时保留当日 bar，行为与修复前一致
        intraday_dropped = False
        if intraday_asof and len(work) >= 2:
            cut = str(intraday_asof)[:10]
            if str(work.iloc[-1]["date"])[:10] >= cut:
                work = work.iloc[:-1].reset_index(drop=True)
                intraday_dropped = True
        if work.empty or len(work) < PARAMS["daily_context_min_rows"]:
            return _default_daily_context(code, status="insufficient", reason="清洗后日线不足")
        work["ma5"] = work["close"].rolling(5).mean()
        work["ma10"] = work["close"].rolling(10).mean()
        work["ma20"] = work["close"].rolling(20).mean()
        work["ma30"] = work["close"].rolling(30).mean()
        work["ma60"] = work["close"].rolling(60).mean()
        work["ma120"] = work["close"].rolling(120).mean()
        work["ma150"] = work["close"].rolling(150).mean()
        work["ma180"] = work["close"].rolling(180).mean()
        work["ma250"] = work["close"].rolling(250).mean()
        # 日线 MACD(12,26,9) / RSI(14) / BOLL(20,2) / 量能（供建仓/加仓日线判断）
        _ema12 = work["close"].ewm(span=12, adjust=False).mean()
        _ema26 = work["close"].ewm(span=26, adjust=False).mean()
        work["macd_dif"] = _ema12 - _ema26
        work["macd_dea"] = work["macd_dif"].ewm(span=9, adjust=False).mean()
        work["macd_hist"] = (work["macd_dif"] - work["macd_dea"]) * 2
        _d = work["close"].diff()
        _g = _d.clip(lower=0).rolling(14, min_periods=1).mean()
        _l = (-_d.clip(upper=0)).rolling(14, min_periods=1).mean()
        work["rsi"] = (100 - 100 / (1 + (_g / _l.replace(0, float("nan"))))).fillna(50.0)
        work["boll_mid"] = work["close"].rolling(20).mean()
        work["boll_std"] = work["close"].rolling(20).std()
        work["boll_up"] = work["boll_mid"] + 2 * work["boll_std"]
        work["boll_dn"] = work["boll_mid"] - 2 * work["boll_std"]
        work["boll_pct"] = (work["close"] - work["boll_dn"]) / (work["boll_up"] - work["boll_dn"]).replace(0, float("nan"))
        work["vol_ma5"] = work["volume"].rolling(5).mean()
        # fix D12: 删除 MA365 死特征（400 天窗口内恒 NaN，且 NaN 会产出非法 JSON）
        today = work.iloc[-1]
        prev = work.iloc[-2]
        prev_prev = work.iloc[-3] if len(work) >= 3 else None
        ref_price = float(current_price or 0.0) or _fnum(today["close"])
        prev_close = _fnum(prev["close"])
        day_ret = (_fnum(today["close"]) - prev_close) / prev_close if prev_close else 0.0
        prev_day_ret = (_fnum(prev["close"]) - _fnum(prev_prev["close"])) / _fnum(prev_prev["close"]) if prev_prev is not None and _fnum(prev_prev["close"]) else 0.0
        ma5 = _fnum(today["ma5"])
        ma10 = _fnum(today["ma10"])
        ma20 = _fnum(today["ma20"])
        ma30 = _fnum(today["ma30"])
        ma60 = _fnum(today["ma60"])
        ma120 = _fnum(today["ma120"])
        ma150 = _fnum(today["ma150"])
        ma180 = _fnum(today["ma180"])
        ma250 = _fnum(today["ma250"])
        ma5_prev = _fnum(work.iloc[-6]["ma5"], ma5) if len(work) >= 6 else ma5
        ma10_prev = _fnum(work.iloc[-6]["ma10"], ma10) if len(work) >= 6 else ma10
        ma20_prev = _fnum(work.iloc[-6]["ma20"], ma20) if len(work) >= 6 else ma20
        ma30_prev = _fnum(work.iloc[-6]["ma30"], ma30) if len(work) >= 6 else ma30
        ma60_prev = _fnum(work.iloc[-6]["ma60"], ma60) if len(work) >= 6 else ma60
        ma120_prev = _fnum(work.iloc[-6]["ma120"], ma120) if len(work) >= 6 else ma120
        ma150_prev = _fnum(work.iloc[-6]["ma150"], ma150) if len(work) >= 6 else ma150
        ma180_prev = _fnum(work.iloc[-6]["ma180"], ma180) if len(work) >= 6 else ma180
        ma250_prev = _fnum(work.iloc[-6]["ma250"], ma250) if len(work) >= 6 else ma250
        ma5_slope = (ma5 - ma5_prev) / ma5_prev if ma5_prev else 0.0
        ma10_slope = (ma10 - ma10_prev) / ma10_prev if ma10_prev else 0.0
        ma20_slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev else 0.0
        ma30_slope = (ma30 - ma30_prev) / ma30_prev if ma30_prev else 0.0
        ma60_slope = (ma60 - ma60_prev) / ma60_prev if ma60_prev else 0.0
        ma120_slope = (ma120 - ma120_prev) / ma120_prev if ma120_prev else 0.0
        ma150_slope = (ma150 - ma150_prev) / ma150_prev if ma150_prev else 0.0
        ma180_slope = (ma180 - ma180_prev) / ma180_prev if ma180_prev else 0.0
        ma250_slope = (ma250 - ma250_prev) / ma250_prev if ma250_prev else 0.0
        gap_to_ma5 = abs(ref_price - ma5) / ma5 if ma5 else 999.0
        gap_to_ma10 = abs(ref_price - ma10) / ma10 if ma10 else 999.0
        gap_to_ma20 = abs(ref_price - ma20) / ma20 if ma20 else 999.0
        gap_to_ma30 = abs(ref_price - ma30) / ma30 if ma30 else 999.0
        gap_to_ma60 = abs(ref_price - ma60) / ma60 if ma60 else 999.0
        gap_to_ma120 = abs(ref_price - ma120) / ma120 if ma120 else 999.0
        gap_to_ma150 = abs(ref_price - ma150) / ma150 if ma150 else 999.0
        gap_to_ma180 = abs(ref_price - ma180) / ma180 if ma180 else 999.0
        gap_to_ma250 = abs(ref_price - ma250) / ma250 if ma250 else 999.0
        near_candidates = []
        for level_name, level, gap in [("MA5", ma5, gap_to_ma5), ("MA10", ma10, gap_to_ma10), ("MA20", ma20, gap_to_ma20), ("MA30", ma30, gap_to_ma30), ("MA60", ma60, gap_to_ma60), ("MA120", ma120, gap_to_ma120), ("MA150", ma150, gap_to_ma150), ("MA180", ma180, gap_to_ma180), ("MA250", ma250, gap_to_ma250)]:
            if level > 0 and gap <= PARAMS["daily_ma_support_loose_gap"]:
                near_candidates.append((gap, level_name, level))
        near_candidates.sort(key=lambda x: (x[0], x[1]))
        support_name = near_candidates[0][1] if near_candidates else ""
        support_level = float(near_candidates[0][2]) if near_candidates else 0.0
        support_gap = float(near_candidates[0][0]) if near_candidates else 0.0
        bull_aligned = ma10 > ma20 > ma30 > 0 and ma20_slope >= 0 and ma30_slope >= 0
        ma_clustered = ma20 > 0 and ma30 > 0 and abs(ma20 - ma30) / ma30 < 0.05 if ma30 else False
        trend_bg = "unknown"
        if ma60 and ref_price < ma60 * (1 - PARAMS["daily_ma_hard_breakdown_gap"]) and ma60_slope <= 0:
            trend_bg = "weak_breakdown"
        elif ma30 and ref_price < ma30 and ma30_slope < 0 and ma20 <= ma30:
            trend_bg = "downtrend"
        elif bull_aligned:
            trend_bg = "bull"
        elif ref_price >= ma20 > 0 and ma30_slope > 0 and ref_price >= ma60 * 0.97 if ma60 else False:
            trend_bg = "uptrend"
        elif ma_clustered and ref_price >= ma60 * 0.97 if ma60 else False:
            trend_bg = "base"
        elif ma30 > 0 and ref_price < ma30:
            trend_bg = "downtrend"
        else:
            trend_bg = "neutral"
        near_support = bool(support_name)
        pullback_support = near_support and trend_bg in {"bull", "uptrend", "base"} and not (ref_price < ma60 * (1 - PARAMS["daily_ma_breakdown_gap"]) if ma60 else False)
        breakdown_risk = False
        if ma20 > 0 and ma30 > 0:
            breakdown_risk = (ref_price < ma20 * (1 - PARAMS["daily_ma_breakdown_gap"]) and ref_price < ma30) or (ref_price < ma30 * (1 - PARAMS["daily_ma_breakdown_gap"]) and ma30_slope < 0)
        hard_breakdown = bool(ma60 and ref_price < ma60 * (1 - PARAMS["daily_ma_hard_breakdown_gap"]) and ma60_slope <= 0)
        overheated = False
        if ma10 > 0 and ref_price > ma10 * (1 + PARAMS["daily_overheat_ma10_gap"]):
            overheated = True
        if ma20 > 0 and ref_price > ma20 * (1 + PARAMS["daily_overheat_ma20_gap"]):
            overheated = True
        if day_ret > PARAMS["daily_overheat_day_ret"] and ma10 > 0 and ref_price > ma10 * 1.04:
            overheated = True
        if ma5 > 0 and gap_to_ma5 <= 0.01:
            ma5_state = "near_ma5_chop"
        elif ma5 > 0 and ref_price >= ma5 and ma5_slope >= 0:
            ma5_state = "above_ma5_trend"
        elif ma5 > 0 and (ref_price < ma5 or ma5_slope < 0):
            ma5_state = "below_ma5_weak"
        else:
            ma5_state = "unknown"
        if hard_breakdown or breakdown_risk:
            gate = "risk"
        elif overheated:
            gate = "overheat"
        elif pullback_support:
            gate = "supportive"
        elif trend_bg in {"downtrend", "weak_breakdown"}:
            gate = "caution"
        else:
            gate = "neutral"
        return {
            "daily_status": "ok",
            "daily_reason": "",
            "daily_asof": str(work.iloc[-1]["date"]),
            # fix P1-9: 口径标识，intraday=盘中（已剔除当日未完成 bar），eod=盘后（含当日）
            "daily_scope": "intraday" if intraday_asof else "eod",
            "daily_price_ref": ref_price,
            # fix P1-9: 盘中口径下 daily_prev_close 取昨日完整收盘（剔除当日 bar 后 iloc[-2] 已是前收，不能沿用）
            "daily_prev_close": _fnum(today["close"]) if intraday_dropped else prev_close,
            "daily_prev_high": _fnum(today["high"]),
            "daily_prev_low": _fnum(today["low"]),
            "daily_prev_close_real": _fnum(today["close"]),  # 最新完整交易日收盘
            "daily_day_ret": day_ret,
            "daily_prev_day_ret": prev_day_ret,
            "daily_ma5": ma5,
            "daily_ma5_slope": ma5_slope,
            "daily_above_ma5": bool(ref_price >= ma5) if ma5 else False,
            "daily_ma5_gap": (ref_price - ma5) / ma5 if ma5 else 0.0,
            "daily_ma5_state": ma5_state,
            "daily_ma10": ma10,
            "daily_ma20": ma20,
            "daily_ma30": ma30,
            "daily_ma60": ma60,
            "daily_ma120": ma120,
            "daily_ma150": ma150,
            "daily_ma180": ma180,
            "daily_ma250": ma250,
            "daily_ma10_slope": ma10_slope,
            "daily_ma20_slope": ma20_slope,
            "daily_ma30_slope": ma30_slope,
            "daily_ma60_slope": ma60_slope,
            "daily_ma120_slope": ma120_slope,
            "daily_ma150_slope": ma150_slope,
            "daily_ma180_slope": ma180_slope,
            "daily_ma250_slope": ma250_slope,
            "daily_trend_bg": trend_bg,
            "daily_gate": gate,
            "daily_support_name": support_name,
            "daily_support_level": support_level,
            "daily_support_gap": support_gap,
            "daily_near_support": near_support,
            "daily_pullback_support": pullback_support,
            "daily_breakdown_risk": breakdown_risk,
            "daily_hard_breakdown": hard_breakdown,
            "daily_overheated": overheated,
            "daily_ma_clustered": ma_clustered,
            "daily_bull_aligned": bull_aligned,
            # 日线 MACD/RSI/BOLL/量能（建仓/加仓日线判断）
            "daily_macd_dif": _fnum(today["macd_dif"]),
            "daily_macd_dea": _fnum(today["macd_dea"]),
            "daily_macd_hist": _fnum(today["macd_hist"]),
            # 近5日出现MACD金叉(DIF上穿DEA)，不要求当前 dif>dea（多头状态）
            "daily_macd_golden": bool(
                ((work["macd_dif"] > work["macd_dea"]) & (work["macd_dif"].shift(1) <= work["macd_dea"].shift(1))).tail(5).any()),
            "daily_rsi": _fnum(today["rsi"]),
            "daily_boll_pct": _fnum(today["boll_pct"]),
            "daily_vol_today": _fnum(today["volume"]),
            "daily_vol_ma5": _fnum(today["vol_ma5"]),
        }
    except Exception as e:
        return _default_daily_context(code, status="error", reason=str(e)[:80])


def _attach_index_regime_context(ctx: Dict[str, Any], code: str, as_of: Optional[str] = None) -> Dict[str, Any]:
    if not PARAMS.get("index_regime_context_enabled", True):
        ctx.update({
            "index_regime_status": "disabled",
            "index_regime_source": "disabled",
            "index_regime_date": as_of or get_today_str(),
            "index_regime_mode": "eod",
            "index_regime": "range",
            "index_regime_name": "横盘震荡",
            "index_score": 0.0,
            "index_score_raw": 0.0,
            "index_trend_score": 0.0,
            "index_env_score": 0.0,
            "index_days_in_regime": 0,
            "index_gate_advice": "normal_t",
            "index_fired_rules": [],
            "index_score_delta": 0.0,
            "index_recent_scores": [],
            "index_pos_factor": 1.0,
            "index_temp_bucket": "neutral",
            "index_circuit_state": "normal",
            "index_policy_reason": "index_regime_context_disabled",
            "index_degraded": ["index_regime"],
        })
        return ctx

    target_date = as_of or get_today_str()
    # V1.30: 盘中锁定日线状态机 —— 交易时段内禁止用不完整日线K线重算日线regime，
    # 统一沿用最近一个完整交易日的判定（mode="morning" 自动对齐到今日之前）；
    # 收盘后（>=15:05）才用当日完整K线以 eod 模式更新。
    # 盘中响应改由 index_regime_intraday 分时预警（I1~I5，注入 feats["intraday_alerts"]）承担，
    # 消除 range↔uni_down 盘中抖动（2026-07-24 曾导致买入熔断反复开关、±20分因子跳变）。
    mode = "eod"
    try:
        if as_of is None and PARAMS.get("index_regime_intraday_lock", True):
            from datetime import time as _dtime
            _n = _now()
            if _n.weekday() < 5 and _n.time() < _dtime(15, 5):
                mode = "morning"
    except Exception:
        pass
    try:
        from analysis.index_regime import detect_index_regime, get_regime_position_factor, index_regime_name
        regime, score, ir_ctx = detect_index_regime(as_of=target_date, force=False, mode=mode)
        regime_value = getattr(regime, "value", str(regime))
        score = float(ir_ctx.get("score", score) or 0.0)
        raw_score = float(ir_ctx.get("score_raw", score) or score)
        trend_score = float(ir_ctx.get("trend_score", 0.0) or 0.0)
        env_score = float(ir_ctx.get("env_score", 0.0) or 0.0)
        days_in_regime = int(ir_ctx.get("days_in_regime", 0) or 0)
        gate_advice = str(ir_ctx.get("gate_advice", "normal_t") or "normal_t")
        degraded = ir_ctx.get("degraded") or []
        detail = ir_ctx.get("detail", {}) or {}
        fired_rules = detail.get("fired_rules") or []
        recent_scores = []
        try:
            recent_days = detail.get("recent_days") or []
            for row in recent_days[-5:]:
                if isinstance(row, dict) and row.get("score") is not None:
                    recent_scores.append(float(row.get("score", 0.0)))
        except Exception:
            recent_scores = []
        score_delta = 0.0
        if len(recent_scores) >= 2:
            score_delta = float(recent_scores[-1] - recent_scores[-2])
        index_pos_factor = float(get_regime_position_factor(regime))
        temp_bucket = "neutral"
        if score <= float(PARAMS.get("index_temp_clear_score", -40.0)):
            temp_bucket = "clear"
        elif score <= float(PARAMS.get("index_temp_freeze_score", -25.0)):
            temp_bucket = "freeze"
        elif score <= float(PARAMS.get("index_temp_cold_score", -15.0)):
            temp_bucket = "cold"
        elif score >= float(PARAMS.get("index_temp_hot_score", 25.0)):
            temp_bucket = "hot"
        circuit = "normal"
        if temp_bucket in {"freeze", "clear"} and score_delta <= float(PARAMS.get("index_deterioration_delta", -10.0)):
            circuit = "clear" if temp_bucket == "clear" else "reduce"
        elif temp_bucket == "cold" or gate_advice == "defensive_t":
            circuit = "defensive"
        if regime_value == "uni_down" and days_in_regime >= int(PARAMS.get("index_deterioration_days", 2)) and score_delta <= 0:
            if circuit == "defensive":
                circuit = "reduce"
        if score >= float(PARAMS.get("index_stabilize_score", -10.0)) and days_in_regime >= int(PARAMS.get("index_stabilize_days", 2)) and gate_advice in {"normal_t", "trend_up_hold"}:
            if circuit in {"reduce", "defensive"}:
                circuit = "stand_aside" if score < 0 else "normal"
        ctx.update({
            "index_regime_status": "ok",
            "index_regime_source": "index_regime.py",
            "index_regime_date": target_date,
            "index_regime_mode": mode,
            "index_regime": regime_value,
            "index_regime_name": index_regime_name(regime),
            "index_score": score,
            "index_score_raw": raw_score,
            "index_trend_score": trend_score,
            "index_env_score": env_score,
            "index_days_in_regime": days_in_regime,
            "index_gate_advice": gate_advice,
            "index_fired_rules": fired_rules,
            "index_score_delta": score_delta,
            "index_recent_scores": recent_scores,
            "index_pos_factor": index_pos_factor,
            "index_temp_bucket": temp_bucket,
            "index_circuit_state": circuit,
            "index_policy_reason": detail.get("state", {}).get("note") or gate_advice,
            "index_degraded": degraded,
        })
    except Exception as e:
        ctx.update({
            "index_regime_status": "error",
            "index_regime_source": "fallback",
            "index_regime_date": target_date,
            "index_regime_mode": mode,
            "index_regime": "range",
            "index_regime_name": "横盘震荡",
            "index_score": 0.0,
            "index_score_raw": 0.0,
            "index_trend_score": 0.0,
            "index_env_score": 0.0,
            "index_days_in_regime": 0,
            "index_gate_advice": "normal_t",
            "index_fired_rules": [],
            "index_score_delta": 0.0,
            "index_recent_scores": [],
            "index_pos_factor": 1.0,
            "index_temp_bucket": "neutral",
            "index_circuit_state": "normal",
            "index_policy_reason": str(e)[:80],
            "index_degraded": ["index_regime"],
        })
    return ctx


def get_daily_context(code: str, holding: dict, current_price: float = 0.0, as_of: Optional[str] = None, intraday: Optional[bool] = None) -> Dict[str, Any]:
    """fix P1-9: 盘中/收盘两套口径，由 intraday 参数控制：
    - intraday=True：剔除当日未完成日线 bar，MA 与 daily_prev_* 一律用昨日完整数据；
    - intraday=False：含当日 bar（盘后口径，修复前行为）；
    - intraday=None（默认，向后兼容）：自动判定——工作日 15:05 前且未指定 as_of 视为盘中。
    调用端说明：main.py 快照写入、support_resistance pivot 等无需改代码，默认即正确口径；
    盘后复盘/回放类调用若需含当日完整 bar，显式传 intraday=False。"""
    if not PARAMS.get("daily_context_enabled", True):
        return _default_daily_context(code, status="disabled", reason="参数关闭")
    if intraday is None:
        intraday = False
        if as_of is None:
            _n = _now()
            if _n.weekday() < 5 and _n.time() < dtime(15, 5):
                intraday = True
    # 口径不同缓存必须分开，避免盘中/盘后互相污染
    cache_key = f"{code}_{as_of or get_today_str()}_{'i' if intraday else 'e'}"
    cached = DAILY_CONTEXT_CACHE.get(cache_key)
    if isinstance(cached, dict):
        ts = cached.get("ts")
        ctx = cached.get("ctx")
        if isinstance(ts, datetime) and isinstance(ctx, dict):
            if (_now() - ts).total_seconds() < PARAMS["daily_cache_ttl_seconds"]:
                return ctx
    try:
        df, fetch_reason = _fetch_daily_bar(code, is_etf=holding.get("type") == "etf", as_of=as_of)
        if df.empty:
            # fix D4: 拉取失败原因透出到 daily_status/daily_reason，供界面展示，不再静默
            ctx = _default_daily_context(code, status="unavailable", reason=fetch_reason or "日线拉取为空")
        else:
            ctx = _build_daily_context_from_df(code, df, current_price=current_price,
                                               intraday_asof=(as_of or get_today_str()) if intraday else None)
        ctx = _attach_index_regime_context(ctx, code, as_of=as_of)
        DAILY_CONTEXT_CACHE[cache_key] = {"ts": _now(), "ctx": ctx}
        return ctx
    except Exception as e:
        ctx = _default_daily_context(code, status="error", reason=str(e)[:80])
        ctx = _attach_index_regime_context(ctx, code, as_of=as_of)
        DAILY_CONTEXT_CACHE[cache_key] = {"ts": _now(), "ctx": ctx}
        return ctx


def label(code: str, holding: dict) -> str:
    return f"{holding.get('name') or code}({code})"

def load_strategy_memory() -> Dict[str, dict]:
    if not os.path.exists(LEARNING_FILE):
        return {}
    try:
        with open(LEARNING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_watchlist() -> Dict[str, dict]:
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_holdings() -> Dict[str, dict]:
    global STRATEGY_MEMORY
    from src.holdings_repo import load_held, HOLDINGS_FILE as _HR_FILE
    # 友好报错：用户手改 holdings.json 常见漏逗号；load_held 容错静默，这里先显式校验一次
    if os.path.exists(_HR_FILE):
        try:
            with open(_HR_FILE, "r", encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            log.error(f"❌ holdings.json 格式错误: {e}。请检查标点符号是否遗漏！")
            return {}

    holdings = load_held()  # 仅持有（qty/base/t_qty>0）；未持有的 auto 候选不进入手动链
    STRATEGY_MEMORY = load_strategy_memory()
    for code, h in holdings.items():
        if not h.get("name"):
            h["name"] = code
    return holdings

@dataclass
class Signal:
    code: str
    name: str
    action: str
    price: float
    score: float
    reasons: List[str] = field(default_factory=list)
    details: List[Dict[str, Any]] = field(default_factory=list)
    indicators: Dict[str, float] = field(default_factory=dict)
    factors: Dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=datetime.now)
    cycle_id: str = ""
    cycle_action_count: int = 0
    hold_qty: int = 0

def _minute_cache_file(code: str, market_date: str) -> str:
    return os.path.join(CACHE_DIR, f"minute_{code}_{market_date}.csv")


def _load_minute_cache(code: str, market_date: str) -> pd.DataFrame:
    cache_file = _minute_cache_file(code, market_date)
    if not os.path.exists(cache_file):
        return pd.DataFrame()

    try:
        age = _now().timestamp() - os.path.getmtime(cache_file)
        if age > PARAMS["cache_ttl_seconds"]:
            return pd.DataFrame()

        df = pd.read_csv(cache_file)
        if not df.empty and "time" in df.columns:
            df["time"] = df["time"].astype(str).str.strip()
            mask = df["time"].str.fullmatch(r"\d{3,4}", na=False)
            if mask.any():
                padded = df.loc[mask, "time"].str.zfill(4)
                df.loc[mask, "time"] = padded.str.slice(0, 2) + ":" + padded.str.slice(2, 4) + ":00"
        if not df.empty:
            return df
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


def _save_minute_cache(code: str, market_date: str, df: pd.DataFrame):
    try:
        df.to_csv(_minute_cache_file(code, market_date), index=False, encoding="utf-8")
    except Exception:
        pass


def cleanup_expired_minute_cache():
    """清理过期分钟线缓存"""
    try:
        if not os.path.exists(CACHE_DIR):
            return

        now_ts = _now().timestamp()
        removed = 0
        for filename in os.listdir(CACHE_DIR):
            if not filename.startswith("minute_") or not filename.endswith(".csv"):
                continue
            file_path = os.path.join(CACHE_DIR, filename)
            try:
                age = now_ts - os.path.getmtime(file_path)
                if age > PARAMS["cache_ttl_seconds"] * 10:
                    os.remove(file_path)
                    removed += 1
            except Exception as e:
                # fix D11: 原日志引用未定义的 code/holding 导致 NameError，改用 filename
                log.warning(f"⚠️  清理分钟线缓存 {filename} 失败: {str(e)[:120]}")
                continue

        if removed:
            log.info(f"🧹 清理过期分钟线缓存 {removed} 个")
    except Exception as e:
        log.debug(f"⚠️  清理缓存失败: {str(e)[:60]}")


def fetch_minute_bar(code: str, is_etf: bool = False) -> pd.DataFrame:
    """获取分钟线数据。P1-2 收敛：改走 core/market_data provider（CSV缓存TTL + gm主源 + 腾讯兜底）。
    保留 MINUTE_FETCH_STATUS/MINUTE_FETCH_DETAIL 诊断与 data_quality trace。"""
    market_date = _now().strftime("%Y-%m-%d")
    fetch_started = _now()
    MINUTE_FETCH_DETAIL[code] = ""
    api_code = clean_code(code)
    from core.market_data import get_provider
    df = get_provider().minute(api_code, market_date, ttl_seconds=PARAMS.get("cache_ttl_seconds"))
    src = df.attrs.get("source", "fetch")
    if df.empty:
        MINUTE_FETCH_STATUS[code] = "provider_empty"
        MINUTE_FETCH_DETAIL[code] = f"provider({src}) 返回空分钟数据"
    else:
        MINUTE_FETCH_STATUS[code] = "cache_hit" if src == "cache" else "ok"
        MINUTE_FETCH_DETAIL[code] = f"provider source={src} rows={len(df)}"
    _append_jsonl(_trace_path("data_quality", market_date), {
        "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": code, "source": src, "minute_status": MINUTE_FETCH_STATUS[code],
        "raw_rows": int(len(df)), "parsed_rows": int(len(df)), "valid_rows": int(len(df)),
        "fetch_cost_ms": int((_now() - fetch_started).total_seconds() * 1000),
    })
    return df



def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 2:
        return df
    c = df["close"]

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(PARAMS["rsi_period"], min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(PARAMS["rsi_period"], min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    # V1.1.2 修复（bug fix，非调优，C 语义）：0/0 钉平窗填 50 中性；
    # 纯上涨窗保持 NaN 与现网一致；预热 leading NaN 不变
    df["rsi"] = (100 - 100 / (1 + rs)).mask((gain == 0) & (loss == 0), 50.0)

    ma = c.rolling(PARAMS["bb_period"], min_periods=1).mean()
    sd = c.rolling(PARAMS["bb_period"], min_periods=1).std()
    df["bb_up"] = ma + PARAMS["bb_std"] * sd
    df["bb_dn"] = ma - PARAMS["bb_std"] * sd
    band_width = (df["bb_up"] - df["bb_dn"]).replace(0, np.nan)
    df["bb_pct"] = (c - df["bb_dn"]) / band_width

    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = exp1 - exp2
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd"] - df["macd_signal"]) * 2

    df["ema_fast"] = c.ewm(span=PARAMS["ema_fast_period"], adjust=False).mean()
    df["ema_slow"] = c.ewm(span=PARAMS["ema_slow_period"], adjust=False).mean()
    df["ema_spread"] = (df["ema_fast"] - df["ema_slow"]) / df["ema_slow"].replace(0, np.nan)

    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["tp_vol"] = tp * df["volume"]
    time_text = df["time"].astype(str).str.strip()
    parsed_time = pd.to_datetime(time_text, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    if parsed_time.isna().all():
        parsed_hms = pd.to_datetime(time_text, format="%H:%M:%S", errors="coerce")
        if parsed_hms.notna().all():
            parsed_time = pd.Timestamp.now().normalize() + (parsed_hms - parsed_hms.dt.normalize())
        else:
            parsed_hm = pd.to_datetime(time_text, format="%H:%M", errors="coerce")
            if parsed_hm.notna().all():
                parsed_time = pd.Timestamp.now().normalize() + (parsed_hm - parsed_hm.dt.normalize())
    df["date"] = parsed_time.dt.date

    # VWAP：优先用实际成交额 / 成交量（腾讯分钟线 volume 单位为"手"=100股，
    # amount 为元，故 volume×100 换算为股，使 VWAP 量纲正确）；
    # amount 列缺失或全为 NaN 时回退到 typical_price × volume 估算
    if "amount" in df.columns and df["amount"].notna().sum() > 0:
        df["vwap"] = df.groupby("date")["amount"].cumsum() / (df.groupby("date")["volume"].cumsum() * 100.0)
    else:
        df["vwap"] = df.groupby("date")["tp_vol"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["vwap"].ffill().fillna(df["close"])
    df["vwap_dev"] = (c - df["vwap"]) / df["vwap"].replace(0, np.nan)
    # V1.27: ATR(14) 归一化 VWAP 偏离度
    _atr_14 = df["high"].sub(df["low"]).abs().rolling(14, min_periods=1).mean()
    df["vwap_dev_atr"] = df["vwap_dev"] / (_atr_14 / df["close"]).replace(0, np.nan)

    day_high = df.groupby("date")["high"].transform("max")
    day_low = df.groupby("date")["low"].transform("min")
    df["day_amplitude"] = (day_high - day_low) / day_low.replace(0, np.nan)
    df["range_pos"] = (c - day_low) / (day_high - day_low + 1e-9)

    last_date = df["date"].iloc[-1]
    prev_data = df[df["date"] < last_date]
    df["prev_high"] = prev_data["high"].max() if not prev_data.empty else df["high"].rolling(120).max()

    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    df["mom5"] = c.pct_change(5)

    k_length = df["high"] - df["low"] + 1e-5
    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / k_length
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / k_length

    return df


def resample_to_15min(df: pd.DataFrame) -> pd.DataFrame:
    """将1分钟K线聚合为15分钟K线，用于更高时间框架的技术分析

    聚合规则：
    - open: 15分钟区间内第一根1分钟线的open
    - high: 15分钟区间内最高high
    - low: 15分钟区间内最低low
    - close: 15分钟区间内最后一根1分钟线的close
    - volume/amount: 15分钟区间内累加
    """
    if df.empty or len(df) < 15:
        return pd.DataFrame()

    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    # 15分钟频率分组（自动处理11:30-13:00休市间隔）
    df["time_15m"] = df["time"].dt.floor("15min")

    agg = df.groupby("time_15m").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    }).reset_index()
    agg = agg.rename(columns={"time_15m": "time"})

    return agg


def add_15min_indicators(df_15min: pd.DataFrame) -> pd.DataFrame:
    """为15分钟K线计算技术指标：MACD、RSI、EMA、成交量比"""
    if df_15min.empty or len(df_15min) < 3:
        return df_15min

    c = df_15min["close"]

    # 15分钟RSI (周期6，更敏感地捕捉短线超卖)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(6, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(6, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    # V1.1.2 修复（bug fix，非调优，C 语义）：0/0 钉平窗填 50 中性；
    # 纯上涨窗保持 NaN 与现网一致；预热 leading NaN 不变
    df_15min["rsi_15m"] = (100 - 100 / (1 + rs)).mask((gain == 0) & (loss == 0), 50.0)

    # 15分钟MACD
    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df_15min["macd_15m"] = exp1 - exp2
    df_15min["macd_signal_15m"] = df_15min["macd_15m"].ewm(span=9, adjust=False).mean()
    df_15min["macd_hist_15m"] = (df_15min["macd_15m"] - df_15min["macd_signal_15m"]) * 2

    # 15分钟EMA
    df_15min["ema_fast_15m"] = c.ewm(span=8, adjust=False).mean()
    df_15min["ema_slow_15m"] = c.ewm(span=21, adjust=False).mean()
    df_15min["ema_spread_15m"] = (df_15min["ema_fast_15m"] - df_15min["ema_slow_15m"]) / df_15min["ema_slow_15m"].replace(0, np.nan)

    # 15分钟成交量比（相对于最近4根15分钟线均值，约1小时）
    df_15min["vol_ma4_15m"] = df_15min["volume"].rolling(4, min_periods=1).mean()
    df_15min["vol_ratio_15m"] = df_15min["volume"] / df_15min["vol_ma4_15m"].replace(0, np.nan)

    # 15分钟2周期动量（30分钟跨度）
    df_15min["mom2_15m"] = c.pct_change(2)

    return df_15min



def resample_to_5min(df: pd.DataFrame) -> pd.DataFrame:
    """将1分钟K线聚合为5分钟K线，用于低吸时的量能缩量+企稳反转确认"""
    if df.empty or len(df) < 5:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_5m"] = df["time"].dt.floor("5min")
    agg = df.groupby("time_5m").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
    }).reset_index()
    agg = agg.rename(columns={"time_5m": "time"})
    return agg


def add_5min_indicators(df_5min: pd.DataFrame) -> pd.DataFrame:
    """为5分钟K线计算指标：量能缩量、企稳反转"""
    if df_5min.empty or len(df_5min) < 3:
        return df_5min
    c = df_5min["close"]
    v = df_5min["volume"]
    # 5分钟2周期动量（10分钟跨度）
    df_5min["mom2_5m"] = c.pct_change(2)
    # 5分钟成交量比（相对于前一根5分钟线）
    df_5min["vol_ratio_5m"] = v / v.shift(1).replace(0, np.nan)
    # 5分钟MACD柱状体（用于判断企稳）
    exp1 = c.ewm(span=6, adjust=False).mean()
    exp2 = c.ewm(span=13, adjust=False).mean()
    macd = exp1 - exp2
    macd_signal = macd.ewm(span=5, adjust=False).mean()
    df_5min["macd_hist_5m"] = (macd - macd_signal) * 2
    # 5分钟低点是否抬高（企稳信号）
    df_5min["low_5m"] = df_5min["low"]
    df_5min["low_rising_5m"] = df_5min["low_5m"] > df_5min["low_5m"].shift(1)
    # 5分钟价格是否止跌（close >= open 或 close > 前close）
    df_5min["stop_falling_5m"] = (df_5min["close"] >= df_5min["open"]) | (df_5min["close"] > df_5min["close"].shift(1))
    return df_5min
