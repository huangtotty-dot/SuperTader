# -*- coding: utf-8 -*-
"""华工科技（000988）Tushare 区间回测脚本。

目标：
- 读取 000988 持仓与主策略参数
- 使用 Tushare 历史分钟线回放 2026-06-29 ~ 2026-07-17
- 每个分钟切片调用 SignalEngine.evaluate
- 生成信号/交易/汇总报告，便于后续反复使用
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import types
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR_DEFAULT = BASE_DIR / "t_io" / "backtests"
CACHE_DIR = BASE_DIR / "t_io" / "cache" / "tushare_mins"

# 确保与现有工程一致
os.environ.setdefault("http_proxy", "")
os.environ.setdefault("https_proxy", "")
os.environ.setdefault("HTTP_PROXY", "")
os.environ.setdefault("HTTPS_PROXY", "")
os.environ.setdefault("ALL_PROXY", "")
os.environ.setdefault("all_proxy", "")


@dataclass
class ModuleBundle:
    shared: Dict[str, Any]
    PARAMS: Dict[str, Any]
    STOCK_PARAMS: Dict[str, Any]
    MINUTE_FETCH_STATUS: Dict[str, str]
    MINUTE_FETCH_DETAIL: Dict[str, Any]
    SIM_NOW: Optional[datetime]
    load_holdings: Any
    load_t_mode: Any
    SignalEngine: Any
    add_indicators: Any
    get_daily_context: Any
    _default_daily_context: Any
    _fetch_daily_bar: Any
    _build_daily_context_from_df: Any
    clean_code: Any
    _now: Any
    MultiTimeframeFetcher: Any
    load_starvation_state: Any
    save_starvation_state: Any
    _starvation_state_file: Any


def _build_shared_namespace() -> ModuleBundle:
    import importlib.util
    import logging
    import numpy as np
    import requests
    import time as _time
    import urllib.error
    import urllib.request
    from datetime import datetime, timedelta, time as dtime

    shared: Dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(BASE_DIR / "tushare_range_backtest.py"),
        "os": os,
        "sys": sys,
        "__package__": None,
        "json": json,
        "time": _time,
        "logging": logging,
        "datetime": datetime,
        "timedelta": timedelta,
        "dtime": dtime,
        "np": np,
        "pd": pd,
        "requests": requests,
        "urllib": __import__("urllib"),
        "urllib.request": urllib.request,
        "urllib.error": urllib.error,
        "importlib": importlib.util,
        "importlib.util": importlib.util,
    }

    # 按 main.py 的顺序加载，避免跨模块符号缺失
    module_order = [
        "config",
        "utils",
        "data_fetcher",
        "multi_timeframe_fetcher",
        "signal_engine",
        "preopen",
        "index_regime",
        "index_regime_intraday",
        "market_regime",
        "position_sizer",
        "daily_sentiment",
        "preopen",
    ]
    for mod_name in module_order:
        mod_path = BASE_DIR / f"{mod_name}.py"
        if not mod_path.exists():
            continue
        code = mod_path.read_text(encoding="utf-8")
        shared["__name__"] = mod_name
        module_obj = types.ModuleType(mod_name)
        module_obj.__dict__.update(shared)
        sys.modules[mod_name] = module_obj
        exec(compile(code, str(mod_path), "exec"), module_obj.__dict__)
        shared.update(module_obj.__dict__)

    # 补齐跨模块函数引用，避免 data_fetcher.load_starvation_state 之类的闭包找不到符号
    df_mod = sys.modules.get("data_fetcher")
    preopen_mod = sys.modules.get("preopen")
    sig_mod = sys.modules.get("signal_engine")
    if preopen_mod is not None and "PreOpenContext" in preopen_mod.__dict__:
        shared["PreOpenContext"] = preopen_mod.__dict__["PreOpenContext"]

    # 统一把共享命名空间补到已加载模块，避免后续运行时 NameError
    for mod_name in module_order:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        try:
            mod.__dict__.update({k: v for k, v in shared.items() if not k.startswith("__")})
        except Exception:
            pass

    if df_mod is not None:
        df_mod.__dict__.setdefault("_starvation_state_file", lambda: str(BASE_DIR / "t_io" / "buy_starvation_state.json"))
        df_mod.__dict__.setdefault("load_starvation_state", lambda: {})
        df_mod.__dict__.setdefault("save_starvation_state", lambda state: None)
    if sig_mod is not None and preopen_mod is not None and "PreOpenContext" in preopen_mod.__dict__:
        sig_mod.__dict__["PreOpenContext"] = preopen_mod.__dict__["PreOpenContext"]
    if sig_mod is not None and df_mod is not None:
        for name in ("_starvation_state_file", "load_starvation_state", "save_starvation_state", "_buy_soft_support_count"):
            if name in df_mod.__dict__:
                sig_mod.__dict__[name] = df_mod.__dict__[name]
    if preopen_mod is not None and sig_mod is not None:
        for name in ("_special_loss_threshold_adjustments", "_special_loss_reduction_rule", "_special_loss_reduction_stage_rule"):
            if name in preopen_mod.__dict__:
                sig_mod.__dict__[name] = preopen_mod.__dict__[name]

    shared["__name__"] = "__main__"

    return ModuleBundle(
        shared=shared,
        PARAMS=shared["PARAMS"],
        STOCK_PARAMS=shared.get("STOCK_PARAMS", {}),
        MINUTE_FETCH_STATUS=shared["MINUTE_FETCH_STATUS"],
        MINUTE_FETCH_DETAIL=shared["MINUTE_FETCH_DETAIL"],
        SIM_NOW=shared.get("SIM_NOW"),
        load_holdings=shared["load_holdings"],
        load_t_mode=shared["load_t_mode"],
        SignalEngine=shared["SignalEngine"],
        add_indicators=shared["add_indicators"],
        get_daily_context=shared["get_daily_context"],
        _default_daily_context=shared["_default_daily_context"],
        _fetch_daily_bar=shared["_fetch_daily_bar"],
        _build_daily_context_from_df=shared["_build_daily_context_from_df"],
        clean_code=shared["clean_code"],
        _now=shared["_now"],
        MultiTimeframeFetcher=shared.get("MultiTimeframeFetcher"),
        load_starvation_state=shared.get("load_starvation_state"),
        save_starvation_state=shared.get("save_starvation_state"),
        _starvation_state_file=shared.get("_starvation_state_file"),
    )


sys.modules.setdefault("tushare_range_backtest", sys.modules[__name__])
BUNDLE = _build_shared_namespace()
shared = BUNDLE.shared
PARAMS = BUNDLE.PARAMS
STOCK_PARAMS = BUNDLE.STOCK_PARAMS
MINUTE_FETCH_STATUS = BUNDLE.MINUTE_FETCH_STATUS
MINUTE_FETCH_DETAIL = BUNDLE.MINUTE_FETCH_DETAIL
load_holdings = BUNDLE.load_holdings
load_t_mode = BUNDLE.load_t_mode
SignalEngine = BUNDLE.SignalEngine
add_indicators = BUNDLE.add_indicators
get_daily_context = BUNDLE.get_daily_context
_default_daily_context = BUNDLE._default_daily_context
_fetch_daily_bar = BUNDLE._fetch_daily_bar
_build_daily_context_from_df = BUNDLE._build_daily_context_from_df
clean_code = BUNDLE.clean_code
_now = BUNDLE._now

# local runtime state may be mutated by replay
SIM_NOW = shared.get("SIM_NOW")
T_MODE: Dict[str, str] = {}
BACKTEST_DAY_CACHE: Dict[str, Dict[str, Any]] = {}
INDEX_REGIME_BACKTEST_CACHE: Dict[str, Dict[str, Any]] = {}


class TushareClient:
    def __init__(self, token: str):
        import tushare as ts

        ts.set_token(token)
        self.pro = ts.pro_api()

    def fetch_minute_day(self, ts_code: str, date: str, freq: str = "1min") -> pd.DataFrame:
        df = self.pro.stk_mins(
            ts_code=ts_code,
            freq=freq,
            start_date=f"{date} 09:00:00",
            end_date=f"{date} 19:00:00",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"trade_time": "time", "vol": "volume", "amount": "amount"})
        if "time" not in df.columns:
            return pd.DataFrame()
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            return pd.DataFrame()
        # A股常规时段
        times = df["time"].dt.time
        m1 = (times >= datetime.strptime("09:30", "%H:%M").time()) & (times <= datetime.strptime("11:30", "%H:%M").time())
        m2 = (times >= datetime.strptime("13:00", "%H:%M").time()) & (times <= datetime.strptime("15:00", "%H:%M").time())
        df = df[m1 | m2].reset_index(drop=True)
        df["date"] = df["time"].dt.strftime("%Y-%m-%d")
        return df


@dataclass
class PendingTrade:
    side: str  # buy / sell
    action: str
    date: str
    time: str
    price: float
    qty: int
    score: float


def _is_trading_day(date_str: str, daily_df: pd.DataFrame) -> bool:
    if daily_df is None or daily_df.empty:
        return True
    return date_str in set(daily_df["date"].astype(str).tolist())


def _make_ts_code(code: str) -> str:
    api_code = clean_code(code)
    if api_code.startswith(("6", "9", "5")):
        return f"{api_code}.SH"
    return f"{api_code}.SZ"


def _load_daily_bars(code: str, is_etf: bool, end_date: str, lookback_days: int = 360) -> pd.DataFrame:
    # 优先使用腾讯 fqkline 日线（与项目内多周期/日线风格一致）；若失败，再回落到项目日线函数。
    try:
        fetcher_cls = BUNDLE.MultiTimeframeFetcher
        if fetcher_cls is not None:
            fetcher = fetcher_cls()
            df = fetcher.fetch_kline(code, period="day", count=max(lookback_days, 120))
            if df is not None and not df.empty:
                if "amount" not in df.columns:
                    df["amount"] = 0.0
                return df[[c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]].copy()
    except Exception:
        pass
    df = _fetch_daily_bar(code, is_etf=is_etf, as_of=end_date)
    if df is not None and not df.empty:
        return df.copy()

    history_file = BASE_DIR / "archive" / "t_io_reports" / f"{clean_code(code)}_history.csv"
    if history_file.exists():
        try:
            hist = pd.read_csv(history_file)
            if "time" in hist.columns:
                hist["date"] = hist["time"].astype(str).str.slice(0, 10)
                hist = hist.drop(columns=["time"], errors="ignore")
            elif "date" in hist.columns:
                hist["date"] = hist["date"].astype(str).str.slice(0, 10)

            keep = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in hist.columns]
            hist = hist[keep].copy()
            if "amount" not in hist.columns:
                hist["amount"] = 0.0
            for col in ["open", "close", "high", "low", "volume", "amount"]:
                if col in hist.columns:
                    hist[col] = pd.to_numeric(hist[col], errors="coerce")
            hist = hist.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date").reset_index(drop=True)
            hist = hist[hist["date"].astype(str).str.slice(0, 10) <= str(end_date)[:10]].reset_index(drop=True)
            if not hist.empty:
                return hist
        except Exception:
            pass
    return pd.DataFrame()


def _build_partial_daily_context(code: str, daily_bars: pd.DataFrame, minute_df: pd.DataFrame, current_price: float, as_of: str) -> Dict[str, Any]:
    if daily_bars is None:
        daily_bars = pd.DataFrame()
    if not daily_bars.empty and "date" in daily_bars.columns:
        daily_bars = daily_bars[daily_bars["date"].astype(str) < as_of].copy()
    if minute_df is None or minute_df.empty:
        if not daily_bars.empty:
            return _build_daily_context_from_df(code, daily_bars, current_price=current_price)
        return _default_daily_context(code, status="unavailable", reason="无分钟数据构建日线")

    work = daily_bars.copy() if not daily_bars.empty else pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    day = str(minute_df["date"].iloc[-1])[:10]
    day_rows = minute_df[minute_df["date"].astype(str).str.slice(0, 10) == day]
    if day_rows.empty:
        if not work.empty:
            return _build_daily_context_from_df(code, work, current_price=current_price)
        return _default_daily_context(code, status="unavailable", reason="分钟数据不足")

    partial = {
        "date": day,
        "open": float(day_rows.iloc[0]["open"]),
        "close": float(day_rows.iloc[-1]["close"]),
        "high": float(day_rows["high"].max()),
        "low": float(day_rows["low"].min()),
        "volume": float(day_rows["volume"].sum()),
        "amount": float(day_rows["amount"].sum()) if "amount" in day_rows.columns else 0.0,
    }

    if not work.empty and "date" in work.columns:
        work = work[work["date"].astype(str) < day].copy()
    work = pd.concat([work, pd.DataFrame([partial])], ignore_index=True)
    work = work.sort_values("date").reset_index(drop=True)
    return _build_daily_context_from_df(code, work, current_price=current_price)


def _load_index_regime_csv(index_csv: str) -> Dict[str, Dict[str, Any]]:
    if not index_csv:
        return {}
    path = Path(index_csv)
    if not path.exists():
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    if df.empty or "date" not in df.columns:
        return {}
    rows: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        date = str(row.get("date", "")).strip()
        if not date:
            continue
        rows[date] = {k: row.get(k) for k in df.columns}
    return rows


def _attach_index_regime_backtest_context(daily_ctx: Dict[str, Any], ds: str, sim_now, reg_rows: Dict[str, Dict[str, Any]], timing: str) -> Dict[str, Any]:
    row = reg_rows.get(ds)
    prev_dates = sorted([d for d in reg_rows.keys() if d < ds])
    prior_row = reg_rows.get(prev_dates[-1]) if prev_dates else None
    use_row = None
    mode = "eod"
    if timing == "same_day":
        use_row = row or prior_row
    elif timing == "prior_then_tail":
        if sim_now and getattr(sim_now, "hour", 0) * 100 + getattr(sim_now, "minute", 0) >= 1430:
            use_row = row or prior_row
            mode = "tail_proxy"
        else:
            use_row = prior_row or row
            mode = "prior_proxy"
    else:
        use_row = prior_row or row
        mode = "prior_proxy"
    if not use_row:
        daily_ctx.update({
            "index_regime_status": "missing",
            "index_regime_source": "csv_missing",
            "index_regime_date": ds,
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
            "index_policy_reason": "index_csv_missing",
        })
        return daily_ctx
    score = float(use_row.get("score", 0.0) or 0.0)
    score_delta = float(use_row.get("score_delta", 0.0) or 0.0)
    gate = str(use_row.get("gate_advice", "normal_t") or "normal_t")
    regime = str(use_row.get("regime", "range") or "range")
    pos_factor = 1.0
    if regime == "uni_down":
        pos_factor = 0.6
    elif regime == "uni_up":
        pos_factor = 1.1
    temp_bucket = "neutral"
    if score <= -40:
        temp_bucket = "clear"
    elif score <= -25:
        temp_bucket = "freeze"
    elif score <= -15:
        temp_bucket = "cold"
    elif score >= 25:
        temp_bucket = "hot"
    circuit = "normal"
    if temp_bucket in {"freeze", "clear"} and score_delta <= -10:
        circuit = "clear" if temp_bucket == "clear" else "reduce"
    elif temp_bucket == "cold" or gate == "defensive_t":
        circuit = "defensive"
    elif score >= -10 and gate in {"normal_t", "trend_up_hold"} and mode in {"prior_proxy", "tail_proxy"}:
        circuit = "normal"
    daily_ctx.update({
        "index_regime_status": "ok",
        "index_regime_source": "csv",
        "index_regime_date": str(use_row.get("date", ds)),
        "index_regime_mode": mode,
        "index_regime": regime,
        "index_regime_name": str(use_row.get("regime_name", "")),
        "index_score": score,
        "index_score_raw": float(use_row.get("score_raw", score) or score),
        "index_trend_score": float(use_row.get("trend_score", 0.0) or 0.0),
        "index_env_score": float(use_row.get("env_score", 0.0) or 0.0),
        "index_days_in_regime": int(use_row.get("days_in_regime", 0) or 0),
        "index_gate_advice": gate,
        "index_fired_rules": str(use_row.get("fired_rules", "")).split("|") if use_row.get("fired_rules") else [],
        "index_score_delta": score_delta,
        "index_recent_scores": [],
        "index_pos_factor": pos_factor,
        "index_temp_bucket": temp_bucket,
        "index_circuit_state": circuit,
        "index_policy_reason": str(use_row.get("note", "") or gate),
    })
    return daily_ctx


def _prepare_day_cache(minute_df: pd.DataFrame) -> Dict[str, Any]:
    if minute_df is None or minute_df.empty:
        return {}
    base = add_indicators(minute_df.copy())
    cache = {"minute_indicators": base}
    try:
        cache["resample_15m"] = add_15min_indicators(resample_to_15min(minute_df.copy()))
    except Exception:
        cache["resample_15m"] = pd.DataFrame()
    try:
        cache["resample_5m"] = add_5min_indicators(resample_to_5min(minute_df.copy()))
    except Exception:
        cache["resample_5m"] = pd.DataFrame()
    return cache


def _starvation_state_file() -> str:
    return str(BASE_DIR / "t_io" / "buy_starvation_state.json")


def load_starvation_state() -> Dict[str, dict]:
    path = Path(_starvation_state_file())
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_starvation_state(state: Dict[str, dict]) -> None:
    try:
        path = Path(_starvation_state_file())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _default_holding(code: str) -> Dict[str, Any]:
    holdings = load_holdings()
    if code in holdings:
        return holdings[code]
    plain = clean_code(code)
    for k, h in holdings.items():
        if clean_code(k) == plain:
            return h
    return {"name": code, "t_qty": 0, "qty": 0, "type": "stock", "cost": 0}


def _stock_params_snapshot(code: str) -> Dict[str, Any]:
    return {k: STOCK_PARAMS.get(code, {}).get(k) for k in [
        "vwap_buy_deviation",
        "awaiting_buyback_vwap_gap",
        "take_profit_pct",
        "morning_no_sell_until",
        "morning_no_sell_min_ret",
        "buy_confirm_min_score",
        "daily_trade_limit",
        "stock_qty_base_pct",
        "stock_qty_strong_pct",
    ] if k in STOCK_PARAMS.get(code, {})}


def _date_range(start: str, end: str) -> List[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


def run_backtest(code: str, start: str, end: str, freq: str, step: int, out_dir: Path, refresh_cache: bool = False, index_regime_csv: str = "", index_regime_timing: str = "prior_then_tail", sentiment_jsonl: str = "") -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    holding = _default_holding(code)
    api_code = _make_ts_code(code)
    token = os.environ.get("TUSHARE_TOKEN") or PARAMS.get("tushare_token_fallback") or "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"

    client = TushareClient(token)
    engine_cls = SignalEngine
    date_list = _date_range(start, end)
    daily_all = _load_daily_bars(code, is_etf=holding.get("type") == "etf", end_date=end)
    index_regime_rows = _load_index_regime_csv(index_regime_csv)
    daily_source = "tencent_fqkline_partial_daily"
    sentiment_source = sentiment_jsonl or str(Path(BASE_DIR / "t_io" / "logs" / "sentiment_daily.jsonl"))
    sentiment_rows = []
    if sentiment_source and Path(sentiment_source).exists():
        try:
            with open(sentiment_source, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            sentiment_rows.append(rec)
                    except Exception:
                        continue
        except Exception:
            sentiment_rows = []
    sentiment_by_date = {str(r.get("date")): r for r in sentiment_rows if str(r.get("date"))}
    if not sentiment_by_date:
        try:
            sentiment_by_date = {}
            for ds in date_list:
                try:
                    rec = compute_daily_sentiment(mode="tail", as_of=ds)
                    if isinstance(rec, dict):
                        sentiment_by_date[ds] = rec
                except Exception:
                    continue
        except Exception:
            sentiment_by_date = {}
    if daily_all is None or daily_all.empty:
        daily_source = "fallback_daily"

    signals: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    daily_rows: List[Dict[str, Any]] = []
    quality_rows: List[Dict[str, Any]] = []
    skipped_rows: List[Dict[str, Any]] = []

    pending_buy: Optional[PendingTrade] = None
    pending_sell: Optional[PendingTrade] = None
    realized_cycles = 0
    gross_pnl = 0.0
    fees_total = 0.0
    notify_count = 0
    buy_count = 0
    sell_count = 0
    unmatched_buys = 0
    unmatched_sells = 0
    evaluated_days = 0

    # 复用当天/持仓对应的 T 模式
    try:
        t_mode = load_t_mode().get(code, "long")
    except Exception:
        t_mode = "long"

    commission_rate = float(PARAMS.get("commission_rate", 0.0015))
    slippage_bps = float(PARAMS.get("slippage_bps", 0.0))
    t_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
    if t_qty <= 0:
        t_qty = int(holding.get("qty") or 0)

    total_days = len(date_list)
    start_ts = time.time()
    print(f"[backtest] start code={code} days={total_days} step={step} source={daily_source}", flush=True)
    for day_idx, ds in enumerate(date_list, start=1):
        try:
            print(f"[backtest] day {day_idx}/{total_days} {ds} loading...", flush=True)
            df_day_path = CACHE_DIR / clean_code(code) / f"{ds}.csv"
            if df_day_path.exists() and not refresh_cache:
                minute_df = pd.read_csv(df_day_path)
                minute_df["time"] = pd.to_datetime(minute_df["time"], errors="coerce")
                source = "cache"
            else:
                minute_df = client.fetch_minute_day(api_code, ds, freq=freq)
                source = "tushare_api"
                if not minute_df.empty:
                    df_day_path.parent.mkdir(parents=True, exist_ok=True)
                    minute_df.to_csv(df_day_path, index=False, encoding="utf-8-sig")

            if minute_df is None or minute_df.empty:
                skipped_rows.append({"date": ds, "reason": "minute_empty", "source": source})
                quality_rows.append({"date": ds, "status": "minute_empty", "minute_source": source})
                continue

            if "date" not in minute_df.columns:
                minute_df["date"] = minute_df["time"].dt.strftime("%Y-%m-%d")
            minute_df = minute_df.sort_values("time").reset_index(drop=True)
            minute_df = minute_df.dropna(subset=["time", "open", "high", "low", "close"]).reset_index(drop=True)
            if minute_df.empty:
                skipped_rows.append({"date": ds, "reason": "minute_invalid_after_clean"})
                quality_rows.append({"date": ds, "status": "minute_invalid_after_clean", "minute_source": source})
                continue

            day_cache = _prepare_day_cache(minute_df)
            BACKTEST_DAY_CACHE[ds] = day_cache
            minute_indicators = day_cache.get("minute_indicators", minute_df)
            resample_15m_full = day_cache.get("resample_15m", pd.DataFrame())
            resample_5m_full = day_cache.get("resample_5m", pd.DataFrame())

            print(f"[backtest] day {day_idx}/{total_days} {ds} minutes={len(minute_df)} cached_15m={len(resample_15m_full)} cached_5m={len(resample_5m_full)}", flush=True)

            # 逐日回放：从第25根开始，可按 step 跳采样
            engine = engine_cls()
            engine.state_reset_date = ds
            engine.buy_count_per_stock[code] = 0
            engine.sell_count_per_stock[code] = 0
            engine.post_sell_block_until[code] = None
            MINUTE_FETCH_STATUS[code] = "ok"
            MINUTE_FETCH_DETAIL[code] = f"{source}:{freq}"
            daily_bars = daily_all.copy() if isinstance(daily_all, pd.DataFrame) else pd.DataFrame()
            day_signals = 0
            day_trades = 0
            day_gross = 0.0
            day_fees = 0.0

            for tick_idx, i in enumerate(range(25, len(minute_df) + 1, max(1, step)), start=1):
                if tick_idx == 1 or tick_idx % 20 == 0 or i == len(minute_df):
                    print(f"[backtest] day {day_idx}/{total_days} {ds} tick={tick_idx} bars={i}/{len(minute_df)}", flush=True)
                sub_df = minute_indicators.iloc[:i].copy() if isinstance(minute_indicators, pd.DataFrame) else minute_df.iloc[:i].copy()
                if len(sub_df) < 25:
                    continue
                sim_now = pd.to_datetime(sub_df.iloc[-1]["time"]).to_pydatetime()
                shared["SIM_NOW"] = sim_now
                globals()["SIM_NOW"] = sim_now
                # 当前分钟策略上下文
                current_price = float(sub_df.iloc[-1]["close"])
                daily_ctx = _build_partial_daily_context(code, daily_bars, sub_df, current_price=current_price, as_of=ds)
                daily_ctx = _attach_index_regime_backtest_context(daily_ctx, ds, sim_now, index_regime_rows, index_regime_timing)
                # 尝试用 sentiment JSONL 历史记录设置 T-mode
                dt_row = sentiment_by_date.get(ds) or sentiment_by_date.get(str(pd.to_datetime(sim_now).date())) or {}
                try:
                    auto = sentiment_by_date.get(ds)
                    if isinstance(auto, dict) and auto and auto.get("t_decision"):
                        decision = dict(auto["t_decision"])
                        per_stock = auto.get("per_stock") or {}
                        sd = per_stock.get(code) or {}
                        if isinstance(sd, dict) and sd:
                            decision.update(sd)
                        mode = decision.get("mode", "long")
                        if mode not in {"long", "short"}:
                            mode = "long"
                        daily_ctx["t_mode"] = mode
                        daily_ctx["effective_t_mode"] = mode
                        daily_ctx["t_mode_source"] = "sentiment_history"
                        daily_ctx["t_pos_factor"] = float(decision.get("pos_factor", 1.0) or 0.0)
                        daily_ctx["t_trade_gate"] = str(decision.get("trade_gate", "normal") or "normal")
                        daily_ctx["t_reason"] = str(decision.get("reason", "") or auto.get("decision_summary", "") or "")
                        daily_ctx["t_basis_date"] = auto.get("date")
                        daily_ctx["t_heat"] = auto.get("z_top3")
                    else:
                        # 无 sentiment 历史 → 用大盘态判定 T-mode（基于 daily_ctx 中的 index_regime 数据）
                        regime = daily_ctx.get("index_regime", "range")
                        score = float(daily_ctx.get("index_score", 0.0) or 0.0)
                        temp_bucket = daily_ctx.get("index_temp_bucket", "neutral")
                        circuit = daily_ctx.get("index_circuit_state", "normal")
                        pos_factor = float(daily_ctx.get("index_pos_factor", 1.0) or 1.0)
                        if regime == "uni_down":
                            mode = "short"
                            gate = "defensive" if temp_bucket in ("cold", "freeze", "clear") else "normal"
                            reason = f"单边下行({score:.0f}分) → 反T"
                        else:
                            mode = "long"
                            gate = "normal"
                            reason = f"{daily_ctx.get('index_regime_name', '横盘')}({score:.0f}分) → 正T"
                        if circuit in ("reduce", "clear"):
                            if gate == "normal":
                                gate = circuit
                            pos_factor *= 0.5
                            reason += f" | 熔断:{circuit}"
                        daily_ctx["t_mode"] = mode
                        daily_ctx["effective_t_mode"] = mode
                        daily_ctx["t_mode_source"] = "regime_fallback"
                        daily_ctx["t_pos_factor"] = float(max(0.0, pos_factor))
                        daily_ctx["t_trade_gate"] = str(gate)
                        daily_ctx["t_reason"] = reason
                    global T_MODE
                    if isinstance(T_MODE, dict):
                        T_MODE[code] = str(daily_ctx.get("t_mode", "long"))
                    shared["T_MODE"] = T_MODE
                except Exception as e:
                    if "T_MODE" not in daily_ctx:
                        daily_ctx["t_mode"] = "long"
                        daily_ctx["t_mode_source"] = "exception_fallback"
                        daily_ctx["t_trade_gate"] = "normal"
                        daily_ctx["t_pos_factor"] = 1.0
                try:
                    scored = sub_df
                    buy_score, sell_score, sig = engine.evaluate(
                        code,
                        holding.get("name", code),
                        scored,
                        holding,
                        daily_ctx=daily_ctx,
                    )
                except Exception as e:
                    skipped_rows.append({"date": ds, "reason": f"evaluate_error:{type(e).__name__}:{str(e)[:120]}"})
                    break

                if not sig:
                    continue

                t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
                # V1.27fix: 大幅低开(>4%)时降低早盘通知门槛
                _today_ret = (current_price / daily_ctx.get("daily_prev_close", current_price) - 1) if daily_ctx.get("daily_prev_close", 0) > 0 else 0.0
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    notify_threshold = 68
                elif t_val >= 1000:
                    notify_threshold = 65
                elif _today_ret < -0.04 and sig.action in ("PANIC_SELL", "SELL_HIGH"):
                    notify_threshold = 60
                else:
                    notify_threshold = 75
                notify = float(sig.score) >= notify_threshold
                signal_row = {
                    "index_regime_date": daily_ctx.get("index_regime_date"),
                    "index_regime": daily_ctx.get("index_regime"),
                    "index_score": daily_ctx.get("index_score"),
                    "index_score_delta": daily_ctx.get("index_score_delta"),
                    "index_gate_advice": daily_ctx.get("index_gate_advice"),
                    "index_temp_bucket": daily_ctx.get("index_temp_bucket"),
                    "index_circuit_state": daily_ctx.get("index_circuit_state"),
                    "index_pos_factor": daily_ctx.get("index_pos_factor"),
                    "index_policy_reason": daily_ctx.get("index_policy_reason"),
                    "t_mode": daily_ctx.get("t_mode"),
                    "t_mode_source": daily_ctx.get("t_mode_source"),
                    "t_pos_factor": daily_ctx.get("t_pos_factor"),
                    "t_trade_gate": daily_ctx.get("t_trade_gate"),
                    "t_reason": daily_ctx.get("t_reason"),
                    "t_basis_date": daily_ctx.get("t_basis_date"),
                    "dynamic_qty": int(sig.hold_qty or t_qty or 0),
                    "qty_source": "position_sizer" if sig.hold_qty else "signal_default",
                    "blocked_by_index_circuit": bool(sig.hold_qty <= 0),
                    "date": ds,
                    "time": SIM_NOW.strftime("%H:%M:%S"),
                    "code": code,
                    "name": holding.get("name", code),
                    "action": sig.action,
                    "score": float(sig.score),
                    "buy_score": float(buy_score),
                    "sell_score": float(sell_score),
                    "price": float(sig.price),
                    "vwap": float(scored.iloc[-1]["vwap"]) if "vwap" in scored.columns and pd.notna(scored.iloc[-1].get("vwap")) else float(sig.price),
                    "notify": bool(notify),
                    "notify_threshold": int(notify_threshold),
                    "hold_qty": int(sig.hold_qty or t_qty or 0),
                    "daily_gate": daily_ctx.get("daily_gate"),
                    "daily_trend_bg": daily_ctx.get("daily_trend_bg"),
                    "daily_support_name": daily_ctx.get("daily_support_name"),
                    "daily_support_gap": daily_ctx.get("daily_support_gap"),
                    "daily_status": daily_ctx.get("daily_status"),
                    "reasons": "|".join(sig.reasons or []),
                    "minute_source": source,
                    "daily_context_source": daily_ctx.get("daily_context_source", "direct_build"),
                }
                signals.append(signal_row)
                day_signals += 1
                if notify:
                    notify_count += 1

                # 简化 T-cycle：买/卖信号成对匹配，支持先买后卖/先卖后买
                qty = int(signal_row["hold_qty"] or t_qty or 0)
                px = float(signal_row["price"])
                fee_rate = commission_rate + (slippage_bps / 10000.0)
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    buy_count += 1
                    if pending_sell is not None:
                        # sell-first cycle close
                        open_px = pending_sell.price
                        close_px = px
                        gross = (open_px - close_px) * min(qty, pending_sell.qty)
                        fees = (open_px + close_px) * min(qty, pending_sell.qty) * fee_rate
                        net = gross - fees
                        trades.append({
                            "open_date": pending_sell.date,
                            "open_time": pending_sell.time,
                            "close_date": ds,
                            "close_time": signal_row["time"],
                            "cycle_type": "short_t",
                            "open_action": pending_sell.action,
                            "close_action": sig.action,
                            "qty": min(qty, pending_sell.qty),
                            "open_price": round(open_px, 4),
                            "close_price": round(close_px, 4),
                            "gross_pnl": round(gross, 4),
                            "fees": round(fees, 4),
                            "net_pnl": round(net, 4),
                            "open_score": pending_sell.score,
                            "close_score": float(sig.score),
                        })
                        realized_cycles += 1
                        gross_pnl += gross
                        fees_total += fees
                        day_gross += gross
                        day_fees += fees
                        day_trades += 1
                        pending_sell = None
                    else:
                        pending_buy = PendingTrade("buy", sig.action, ds, signal_row["time"], px, qty, float(sig.score))
                elif sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    sell_count += 1
                    if pending_buy is not None:
                        open_px = pending_buy.price
                        close_px = px
                        gross = (close_px - open_px) * min(qty, pending_buy.qty)
                        fees = (open_px + close_px) * min(qty, pending_buy.qty) * fee_rate
                        net = gross - fees
                        trades.append({
                            "open_date": pending_buy.date,
                            "open_time": pending_buy.time,
                            "close_date": ds,
                            "close_time": signal_row["time"],
                            "cycle_type": "long_t",
                            "open_action": pending_buy.action,
                            "close_action": sig.action,
                            "qty": min(qty, pending_buy.qty),
                            "open_price": round(open_px, 4),
                            "close_price": round(close_px, 4),
                            "gross_pnl": round(gross, 4),
                            "fees": round(fees, 4),
                            "net_pnl": round(net, 4),
                            "open_score": pending_buy.score,
                            "close_score": float(sig.score),
                        })
                        realized_cycles += 1
                        gross_pnl += gross
                        fees_total += fees
                        day_gross += gross
                        day_fees += fees
                        day_trades += 1
                        pending_buy = None
                    else:
                        pending_sell = PendingTrade("sell", sig.action, ds, signal_row["time"], px, qty, float(sig.score))

                engine.record_trade_action(code, sig.action, sig.hold_qty)

            if pending_buy is not None:
                unmatched_buys += 1
            if pending_sell is not None:
                unmatched_sells += 1

            evaluated_days += 1
            print(f"[backtest] day {day_idx}/{total_days} {ds} done signals={day_signals} trades={day_trades} net={round(day_gross - day_fees, 4)} elapsed={round(time.time() - start_ts, 1)}s", flush=True)
            day_row = {
                "date": ds,
                "minute_source": source,
                "signals": day_signals,
                "trades": day_trades,
                "gross_pnl": round(day_gross, 4),
                "fees": round(day_fees, 4),
                "net_pnl": round(day_gross - day_fees, 4),
                "minute_rows": int(len(minute_df)),
                "daily_status": daily_ctx.get("daily_status"),
                "daily_gate": daily_ctx.get("daily_gate"),
                "daily_trend_bg": daily_ctx.get("daily_trend_bg"),
                "daily_support_name": daily_ctx.get("daily_support_name"),
                "daily_support_gap": daily_ctx.get("daily_support_gap"),
                "daily_context_source": daily_ctx.get("daily_context_source", "direct_build"),
                "t_mode": daily_ctx.get("t_mode"),
                "t_mode_source": daily_ctx.get("t_mode_source"),
                "t_pos_factor": daily_ctx.get("t_pos_factor"),
                "t_trade_gate": daily_ctx.get("t_trade_gate"),
                "t_reason": daily_ctx.get("t_reason"),
                "t_basis_date": daily_ctx.get("t_basis_date"),
            }
            daily_rows.append(day_row)
            quality_rows.append({"date": ds, "status": "ok", "minute_source": source, "rows": int(len(minute_df))})
        except Exception as e:
            print(f"[backtest] day {day_idx}/{total_days} {ds} error={type(e).__name__}: {str(e)[:180]}", flush=True)
            skipped_rows.append({"date": ds, "reason": f"{type(e).__name__}: {str(e)[:180]}"})
            quality_rows.append({"date": ds, "status": "error", "reason": f"{type(e).__name__}: {str(e)[:180]}"})

    # 如果存在未配对信号，尽量反映在统计中
    if pending_buy is not None:
        unmatched_buys += 1
    if pending_sell is not None:
        unmatched_sells += 1

    signals_df = pd.DataFrame(signals)
    trades_df = pd.DataFrame(trades)
    daily_df = pd.DataFrame(daily_rows)
    quality_df = pd.DataFrame(quality_rows)

    mode_counts = {}
    gate_counts = {}
    for row in signals:
        mode_counts[row.get("t_mode", "long")] = mode_counts.get(row.get("t_mode", "long"), 0) + 1
        gate_counts[row.get("t_trade_gate", "normal")] = gate_counts.get(row.get("t_trade_gate", "normal"), 0) + 1
    summary = {
        "requested_days": len(date_list),
        "evaluated_days": evaluated_days,
        "skipped_days": len(skipped_rows),
        "total_signals": len(signals),
        "notify_signals": notify_count,
        "buy_signals": buy_count,
        "sell_signals": sell_count,
        "completed_cycles": realized_cycles,
        "unmatched_buys": unmatched_buys,
        "unmatched_sells": unmatched_sells,
        "gross_pnl": round(gross_pnl, 4),
        "fees": round(fees_total, 4),
        "net_pnl": round(gross_pnl - fees_total, 4),
        "commission_rate": commission_rate,
        "slippage_bps": slippage_bps,
        "t_mode_source": "dynamic_sentiment",
        "t_mode_counts": mode_counts,
        "t_gate_counts": gate_counts,
    }

    metadata = {
        "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": "tushare_range_backtest.py",
        "code": code,
        "name": holding.get("name", code),
        "ts_code": api_code,
        "start": start,
        "end": end,
        "minute_source": "tushare_stk_mins",
        "daily_source": daily_source,
        "step": step,
        "holding_key": code,
        "stock_params_applied": bool(STOCK_PARAMS.get(code)),
        "minute_token_env": "TUSHARE_TOKEN",
        "minute_cache_dir": str(CACHE_DIR / clean_code(code)),
        "index_regime_csv": index_regime_csv,
        "index_regime_timing": index_regime_timing,
        "sentiment_source": sentiment_source,
    }

    report = {
        "metadata": metadata,
        "summary": summary,
        "data_quality": quality_rows,
        "daily": daily_rows,
        "signals": signals,
        "trades": trades,
        "unmatched": [
            {"type": "buy", "count": unmatched_buys},
            {"type": "sell", "count": unmatched_sells},
        ],
        "skipped": skipped_rows,
        "stock_params_snapshot": _stock_params_snapshot(code),
    }

    base_name = f"{clean_code(code)}_{start}_to_{end}"
    json_path = out_dir / f"{base_name}.json"
    signals_csv = out_dir / f"{base_name}_signals.csv"
    trades_csv = out_dir / f"{base_name}_trades.csv"
    md_path = out_dir / f"{base_name}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[backtest] finished code={code} days={total_days} evaluated={evaluated_days} skipped={len(skipped_rows)} signals={len(signals)} trades={len(trades)} elapsed={round(time.time() - start_ts, 1)}s", flush=True)
    if not signals_df.empty:
        signals_df.to_csv(signals_csv, index=False, encoding="utf-8-sig")
    else:
        signals_csv.write_text("", encoding="utf-8")
    if not trades_df.empty:
        trades_df.to_csv(trades_csv, index=False, encoding="utf-8-sig")
    else:
        trades_csv.write_text("", encoding="utf-8")

    lines = []
    lines.append(f"# 华工科技区间回测报告")
    lines.append("")
    lines.append(f"- 标的: {holding.get('name', code)} ({code} / {api_code})")
    lines.append(f"- 区间: {start} ~ {end}")
    lines.append(f"- T模式来源: {summary.get('t_mode_source', 'dynamic_sentiment')}")
    lines.append(f"- T模式统计: {summary.get('t_mode_counts', {})}")
    lines.append(f"- T门控统计: {summary.get('t_gate_counts', {})}")
    lines.append(f"- 分钟数据源: Tushare stk_mins")
    lines.append(f"- 日线上下文: 腾讯日线/分钟 partial daily")
    lines.append("")
    lines.append("## 汇总")
    for k, v in summary.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 数据质量")
    for row in quality_rows[:30]:
        lines.append(f"- {row}")
    if len(quality_rows) > 30:
        lines.append(f"- ... 共 {len(quality_rows)} 条")
    lines.append("")
    lines.append("## 交易统计")
    lines.append(f"- 完成交易对数: {len(trades_df)}")
    lines.append(f"- 未配对买单: {unmatched_buys}")
    lines.append(f"- 未配对卖单: {unmatched_sells}")
    lines.append("")
    lines.append("## 参数快照")
    lines.append(json.dumps(report["stock_params_snapshot"], ensure_ascii=False, indent=2))
    lines.append("")
    if not signals_df.empty:
        lines.append("## 信号预览")
        lines.append(signals_df.head(20).to_csv(index=False))
    if not trades_df.empty:
        lines.append("## 交易预览")
        lines.append(trades_df.head(20).to_csv(index=False))
    md_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "report": report,
        "json_path": str(json_path),
        "signals_csv": str(signals_csv),
        "trades_csv": str(trades_csv),
        "md_path": str(md_path),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="华工科技 000988 区间回测（Tushare 分钟线）")
    ap.add_argument("--code", default="000988", help="股票代码，默认 000988")
    ap.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--freq", default="1min", help="Tushare 分钟频率")
    ap.add_argument("--step", type=int, default=1, help="每 N 根分钟线评估一次")
    ap.add_argument("--out-dir", default=str(OUT_DIR_DEFAULT), help="输出目录")
    ap.add_argument("--refresh-cache", action="store_true", help="强制刷新 Tushare 缓存")
    ap.add_argument("--index-regime-csv", default="", help="大盘态势CSV路径")
    ap.add_argument("--index-regime-timing", default="prior_then_tail", choices=["prior", "same_day", "prior_then_tail"], help="大盘态势注入时机")
    ap.add_argument("--sentiment-jsonl", default="", help="情绪历史JSONL路径")
    args = ap.parse_args()

    result = run_backtest(
        code=args.code,
        start=args.start,
        end=args.end,
        freq=args.freq,
        step=args.step,
        out_dir=Path(args.out_dir),
        refresh_cache=args.refresh_cache,
        index_regime_csv=args.index_regime_csv,
        index_regime_timing=args.index_regime_timing,
        sentiment_jsonl=args.sentiment_jsonl,
    )

    print(json.dumps({
        "json_path": result["json_path"],
        "signals_csv": result["signals_csv"],
        "trades_csv": result["trades_csv"],
        "md_path": result["md_path"],
        "summary": result["report"]["summary"],
        "metadata": result["report"]["metadata"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
