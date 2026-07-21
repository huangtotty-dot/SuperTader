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


def _signal_qty(record: dict, fallback_qty: int = 0) -> int:
    try:
        qty = int(record.get("qty", 0) or 0)
    except Exception:
        qty = 0
    if qty > 0:
        return qty
    try:
        hold_qty = int(record.get("hold_qty", 0) or 0)
    except Exception:
        hold_qty = 0
    if hold_qty > 0:
        return hold_qty
    try:
        net_qty = int(record.get("net_qty", 0) or 0)
    except Exception:
        net_qty = 0
    if net_qty > 0:
        return net_qty
    return max(0, int(fallback_qty or 0))


def _sum_signal_qty(signals: List[dict], fallback_qty: int = 0) -> int:
    return sum(_signal_qty(item, fallback_qty) for item in signals)


def _qty_weight(qty: int, base_qty: int) -> float:
    qty = max(0, int(qty or 0))
    base_qty = max(100, int(base_qty or 0))
    weight = qty / base_qty if base_qty else 0.0
    return float(_clamp(weight, 0.5, 3.0))


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


def _result_trace_path(day: Optional[str] = None) -> str:
    day = day or get_today_str()
    return os.path.join(TRACE_DIR, f"signal_outcome_{day}.jsonl")


def _append_jsonl(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _register_signal_outcome(sig: "Signal", holding: dict) -> None:
    SIGNAL_OUTCOME_TRACKER.setdefault(sig.code, []).append({
        "signal_time": sig.ts,
        "action": sig.action,
        "signal_price": sig.price,
        "signal_score": sig.score,
        "vwap_at_signal": sig.indicators.get("vwap", sig.price),
        "market_state": sig.indicators.get("market_state", "unknown"),
        "benchmark_state": sig.indicators.get("benchmark_state", "unknown"),
        "benchmark_gate": sig.indicators.get("benchmark_gate", "neutral"),
        "qty": _default_trade_qty(holding, sig),
        "hold_qty": int(sig.factors.get("hold_qty", holding.get("t_qty", 0)) or 0),
        "name": sig.name,
        "price_points": [],
        "maturity_5m": False,
        "maturity_15m": False,
    })


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
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)


def _benchmark_meta_for_code(code: str) -> Dict[str, str]:
    code = str(code or "").strip()
    if code.startswith(("688", "689")):
        return {"code": "sh000688", "name": "科创50", "market": "sh", "kind": "star"}
    if code.startswith(("300", "301")):
        return {"code": "sz399006", "name": "创业板指", "market": "sz", "kind": "chi_next"}
    if code.startswith(("60", "68", "90")):
        return {"code": "sh000001", "name": "上证指数", "market": "sh", "kind": "sse"}
    return {"code": "sz399001", "name": "深证成指", "market": "sz", "kind": "szse"}


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


def _calc_ps_levels(price: float, daily_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """V1.24-R2: 计算压力位和支撑位

    压力候选：近10日最高、前日收盘、MA5、MA10、MA20、MA30、MA60、MA150
      只取 >= price 的候选，压力位 = min(这些候选) -> 最接近的阻力

    支撑候选：近10日最低、前日收盘、MA5、MA10、MA20、MA30、MA60、MA150
      只取 <= price 的候选，支撑位 = max(这些候选) -> 最接近的支撑

    返回 dict 包含:
      - pressure_name, pressure_level, pressure_gap
      - support_name, support_level, support_gap
      - is_major_pressure (是否为重要压力 -> 全部卖出)
      - sell_qty_pct (50=部分卖出, 100=全部卖出)
    """
    ma_candidates = [
        ("MA5", daily_ctx.get("daily_ma5", 0.0)),
        ("MA10", daily_ctx.get("daily_ma10", 0.0)),
        ("MA20", daily_ctx.get("daily_ma20", 0.0)),
        ("MA30", daily_ctx.get("daily_ma30", 0.0)),
        ("MA60", daily_ctx.get("daily_ma60", 0.0)),
        ("MA150", daily_ctx.get("daily_ma150", 0.0)),
    ]
    valid_mas = [(name, val) for name, val in ma_candidates if val > 0]

    high_10d = daily_ctx.get("daily_high_10d", 0.0)
    low_10d = daily_ctx.get("daily_low_10d", 0.0)
    pre2_close = daily_ctx.get("pre2_close", 0.0)

    # 压力候选：所有 >= price 的值（均线不区分是否"上方"，直接加入）
    pressure_candidates = []
    if high_10d > 0:
        pressure_candidates.append(("近10日最高", high_10d))
    if pre2_close > 0:
        pressure_candidates.append(("前日收盘", pre2_close))
    for name, val in valid_mas:
        pressure_candidates.append((name, val))

    # 只保留 >= price 的，然后取最小值 = 最接近的阻力
    pressure_candidates = [(n, v) for n, v in pressure_candidates if v >= price]
    if pressure_candidates:
        pressure_name, pressure_level = min(pressure_candidates, key=lambda x: x[1])
    else:
        pressure_name, pressure_level = "", 0.0

    # 支撑候选：所有 <= price 的值
    support_candidates = []
    if low_10d > 0:
        support_candidates.append(("近10日最低", low_10d))
    if pre2_close > 0:
        support_candidates.append(("前日收盘", pre2_close))
    for name, val in valid_mas:
        support_candidates.append((name, val))

    # 只保留 <= price 的，然后取最大值 = 最接近的支撑
    support_candidates = [(n, v) for n, v in support_candidates if v <= price]
    if support_candidates:
        support_name, support_level = max(support_candidates, key=lambda x: x[1])
    else:
        support_name, support_level = "", 0.0

    pressure_gap = (pressure_level - price) / price if pressure_level > 0 and price > 0 else 0.0
    support_gap = (price - support_level) / price if support_level > 0 and price > 0 else 0.0

    # 判断压力重要性 -> 决定卖出比例
    # 短期均线 (MA5/MA10) 压力 -> 部分卖出 (50%)
    # 中期/长期/历史高点压力 -> 全部卖出 (100%)
    is_major_pressure = False
    sell_qty_pct = 100  # 默认全部卖出
    if pressure_name in ("MA5", "MA10"):
        is_major_pressure = False
        sell_qty_pct = 50   # 部分卖出
    elif pressure_name in ("MA20", "MA30", "MA60", "MA150", "近10日最高", "前日收盘"):
        is_major_pressure = True
        sell_qty_pct = 100  # 全部卖出

    return {
        "pressure_name": pressure_name,
        "pressure_level": pressure_level,
        "pressure_gap": pressure_gap,
        "support_name": support_name,
        "support_level": support_level,
        "support_gap": support_gap,
        "is_major_pressure": is_major_pressure,
        "sell_qty_pct": sell_qty_pct,
        "pressure_candidates": pressure_candidates,
        "support_candidates": support_candidates,
    }


