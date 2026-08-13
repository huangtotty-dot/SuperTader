def _ensure_ai_review_stats(code: str, holding: dict) -> dict:
    if code not in AI_REVIEW_STATS:
        AI_REVIEW_STATS[code] = {"名称": holding.get("name", code), "最大多头分": 0, "最大空头分": 0, "最大振幅": 0.0, "触发买入次数": 0, "触发卖出次数": 0, "触发买入股数": 0, "触发卖出股数": 0}
    AI_REVIEW_STATS[code]["名称"] = holding.get("name", code)
    return AI_REVIEW_STATS[code]


def _ensure_daily_decision_stats(code: str, holding: dict) -> dict:
    default_price = float(holding.get("cost", 0) or 0)
    if code not in DAILY_DECISION_STATS:
        DAILY_DECISION_STATS[code] = {
            "name": holding.get("name", code),
            "buy_signals": [],
            "buy_low_signals": [],
            "buy_add_signals": [],
            "sell_signals": [],
            "sell_high_signals": [],
            "panic_sell_signals": [],
            "last_price": default_price,
            "last_vwap": default_price,
            "close_price": default_price,
            "last_score": 0.0,
            "last_buy_score": 0.0,
            "last_sell_score": 0.0,
            "last_amp": 0.0,
            "last_scan_time": "",
            "last_status": "未扫描",
            "last_status_detail": "",
            "last_market_state": "unknown",
            "last_benchmark_code": "",
            "last_benchmark_name": "",
            "last_benchmark_state": "unknown",
            "last_benchmark_gate": "unknown",
            "last_benchmark_reason": "",
            "last_buy_limit_reason": "",
            "minute_status": "未拉取",
            "minute_detail": "",
        }
    stats = DAILY_DECISION_STATS[code]
    stats["name"] = holding.get("name", code)
    return stats


def _low_buy_cash_reference() -> float:
    runtime_config = load_runtime_config()
    strategy_cfg = runtime_config.get("strategy", {}) if isinstance(runtime_config, dict) else {}
    return float(strategy_cfg.get("low_buy_cash_reference", 35454.23) or 35454.23)


def _special_low_buy_qty(code: str, holding: dict, price: float, stage: str = "intraday") -> int:
    code = str(code or "").strip()
    price = float(price or 0)
    if price <= 0:
        return 0
    ratio_map = {
        "688102": 0.18,
        "601698": 0.22,
        "300364": 0.10,
        "002639": 0.12,
        "588000": 0.42,  # 科创50ETF - 分批加仓，先加¥15,000，约8,200份
        "601998": 0.42,  # 中信银行 - 按计划加¥15,000，约1,900股
        "600089": 0.70,  # 特变电工 - 大幅加仓¥25,000，约1,090股
    }
    code_stage_factor_map = {
        "688102": {
            "open_trial": 0.18,
            "open_add": 0.26,
            "intraday_trial": 0.30,
            "intraday_add": 0.42,
            "eod_trial": 0.12,
            "eod_add": 0.18,
        },
        "601698": {
            "open_trial": 0.12,
            "open_add": 0.18,
            "intraday_trial": 0.22,
            "intraday_add": 0.32,
            "eod_trial": 0.08,
            "eod_add": 0.12,
        },
        "300364": {
            "open_trial": 0.10,
            "open_add": 0.12,
            "intraday_trial": 0.14,
            "intraday_add": 0.18,
            "eod_trial": 0.08,
            "eod_add": 0.10,
        },
        "002639": {
            "open_trial": 0.08,
            "open_add": 0.10,
            "intraday_trial": 0.12,
            "intraday_add": 0.16,
            "eod_trial": 0.06,
            "eod_add": 0.08,
        },
        "588000": {
            "open_trial": 0.18,
            "open_add": 0.20,
            "intraday_trial": 0.25,
            "intraday_add": 0.30,
            "eod_trial": 0.12,
            "eod_add": 0.15,
        },
        "601998": {
            "open_trial": 0.15,
            "open_add": 0.18,
            "intraday_trial": 0.20,
            "intraday_add": 0.30,
            "eod_trial": 0.10,
            "eod_add": 0.15,
        },
        "600089": {
            "open_trial": 0.25,
            "open_add": 0.35,
            "intraday_trial": 0.40,
            "intraday_add": 0.50,
            "eod_trial": 0.15,
            "eod_add": 0.20,
        },
    }
    stage_factor_map = {
        "open": 0.22,
        "open_trial": 0.22,
        "open_add": 0.30,
        "intraday": 0.35,
        "intraday_trial": 0.35,
        "intraday_add": 0.50,
        "eod": 0.15,
        "eod_trial": 0.15,
        "eod_add": 0.22,
    }
    ratio = float(ratio_map.get(code, 0.0) or 0.0)
    stage_key = str(stage or "intraday")
    stage_factor = float(code_stage_factor_map.get(code, {}).get(stage_key, stage_factor_map.get(stage_key, 0.45)) or 0.45)
    if ratio <= 0 or stage_factor <= 0:
        return 0
    cash_pool = _low_buy_cash_reference() * ratio * stage_factor
    qty = int((cash_pool // price) // 100 * 100)
    current_cap = int(holding.get("qty") or holding.get("t_qty") or holding.get("position_qty") or 0)
    if current_cap > 0:
        qty = min(qty, current_cap)
    return max(100, qty) if cash_pool >= price * 100 else 0


def _default_trade_qty(holding: dict, sig: Optional["Signal"] = None) -> int:
    if sig is not None and sig.action in {"BUY_LOW", "ADD_POS"}:
        special_qty = _special_low_buy_qty(sig.code, holding, float(getattr(sig, "price", 0) or 0))
        if special_qty > 0:
            return special_qty
    candidates = []
    if sig is not None:
        candidates.extend([
            sig.hold_qty,
            sig.factors.get("hold_qty", 0) if isinstance(sig.factors, dict) else 0,
            sig.factors.get("net_qty", 0) if isinstance(sig.factors, dict) else 0,
        ])
    candidates.extend([holding.get("t_qty"), holding.get("qty"), holding.get("position_qty")])
    for value in candidates:
        try:
            qty = int(value or 0)
        except Exception:
            qty = 0
        if qty > 0:
            return qty
    return 0


def _snapshot_file(code: str, day: str) -> str:
    folder = os.path.join(SNAPSHOT_DIR, day[:4], day[5:7])
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{code}_{day}.json")


def _trace_path(kind: str, day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(TRACE_DIR, f"{kind}_{day}.jsonl")


def _preopen_path(day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(PREOPEN_DIR, f"preopen_{day}.json")


def _json_safe(obj):
    """fix D12: 递归把 NaN/inf 转 None，避免 json.dump 产出非法 JSON"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


def _append_jsonl(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_json_safe(record), ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _snapshot_write(code: str, holding: dict, df: pd.DataFrame, indicators: dict, signal: Optional[dict] = None, daily_context: Optional[dict] = None) -> None:
    if df.empty:
        return
    day = str(df.iloc[-1].get("time", ""))[:10]
    if len(day) != 10:
        return
    path = _snapshot_file(code, day)
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    old_bars = existing.get("bars", []) if isinstance(existing, dict) else []
    new_bars = df[[c for c in ["time", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]].to_dict(orient="records")
    merged_bars: Dict[str, dict] = {}
    for row in old_bars + new_bars:
        ts = str(row.get("time", ""))
        if ts:
            merged_bars[ts] = row
    bars = [merged_bars[k] for k in sorted(merged_bars.keys())]
    rec = {
        "code": code,
        "name": holding.get("name", code),
        "date": day,
        "saved_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "row_count": int(len(bars)),
        "last_time": str(df.iloc[-1].get("time", "")),
        "last_close": float(df.iloc[-1].get("close", 0) or 0),
        "last_vwap": float(indicators.get("vwap", df.iloc[-1].get("close", 0)) or 0),
        "market_state": indicators.get("market_state", "unknown"),
        "benchmark_code": indicators.get("benchmark_code", ""),
        "benchmark_name": indicators.get("benchmark_name", ""),
        "benchmark_state": indicators.get("benchmark_state", "unknown"),
        "benchmark_gate": indicators.get("benchmark_gate", "neutral"),
        "benchmark_reason": indicators.get("benchmark_reason", ""),
        "signal": signal or existing.get("signal", {}) if isinstance(existing, dict) else (signal or {}),
        "daily_context": daily_context or (existing.get("daily_context", {}) if isinstance(existing, dict) else {}),
        "bars": bars,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_json_safe(rec), f, ensure_ascii=False)  # fix D12: NaN→None
    os.replace(tmp, path)


def _default_daily_context(code: str, status: str = "unavailable", reason: str = "") -> Dict[str, Any]:
    return {
        "daily_status": status,
        "daily_reason": reason,
        "daily_asof": get_today_str(),
        "daily_price_ref": 0.0,
        "daily_prev_close": 0.0,
        "daily_prev_high": 0.0,
        "daily_prev_low": 0.0,
        "daily_prev_close_real": 0.0,
        "daily_day_ret": 0.0,
        "daily_ma5": 0.0,
        "daily_ma5_slope": 0.0,
        "daily_above_ma5": False,
        "daily_ma5_gap": 0.0,
        "daily_ma5_state": "unknown",
        "daily_ma10": 0.0,
        "daily_ma20": 0.0,
        "daily_ma30": 0.0,
        "daily_ma60": 0.0,
        "daily_ma10_slope": 0.0,
        "daily_ma20_slope": 0.0,
        "daily_ma30_slope": 0.0,
        "daily_ma60_slope": 0.0,
        "daily_trend_bg": "unknown",
        "daily_gate": "neutral",
        "daily_support_name": "",
        "daily_support_level": 0.0,
        "daily_support_gap": 0.0,
        "daily_near_support": False,
        "daily_pullback_support": False,
        "daily_breakdown_risk": False,
        "daily_hard_breakdown": False,
        "daily_overheated": False,
        "daily_ma_clustered": False,
        "daily_bull_aligned": False,
        # V1.24: 压力/支撑位计算所需字段
        "daily_high_10d": 0.0,
        "daily_low_10d": 0.0,
        "pre2_close": 0.0,
        "daily_ma150": 0.0,
    }


