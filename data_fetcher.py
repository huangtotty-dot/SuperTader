def clean_code(code: str) -> str:
    """去除 _A/_B 等账户后缀，返回纯数字代码供数据接口使用"""
    if not code:
        return ""
    if "_" in code:
        return code.split("_")[0]
    return code


def _fetch_daily_bar(code: str, is_etf: bool = False, as_of: Optional[str] = None) -> pd.DataFrame:
    try:
        import akshare as ak
        api_code = clean_code(code)
        end_date = (as_of or _now().strftime("%Y%m%d")).replace("-", "")
        start_date = (_now() - timedelta(days=180)).strftime("%Y%m%d")
        if is_etf:
            for fn in ["fund_etf_hist_em", "fund_etf_hist_sina"]:
                if hasattr(ak, fn):
                    try:
                        df = getattr(ak, fn)(symbol=api_code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
                        if isinstance(df, pd.DataFrame) and not df.empty:
                            break
                    except Exception:
                        df = pd.DataFrame()
                else:
                    df = pd.DataFrame()
        else:
            df = ak.stock_zh_a_hist(symbol=api_code, period="daily", start_date=start_date, end_date=end_date, adjust="qfq")
        if df is None or df.empty:
            return pd.DataFrame()
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        df = df.rename(columns=rename_map)
        keep_cols = [c for c in ["date", "open", "close", "high", "low", "volume", "amount"] if c in df.columns]
        if len(keep_cols) < 5:
            return pd.DataFrame()
        df = df[keep_cols].copy()
        df["date"] = df["date"].astype(str).str.slice(0, 10)
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "close", "high", "low"]).sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


def _build_daily_context_from_df(code: str, df: pd.DataFrame, current_price: float = 0.0) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < PARAMS["daily_context_min_rows"]:
        return _default_daily_context(code, status="insufficient", reason=f"日线数据不足({0 if df is None else len(df)})")
    try:
        work = df.copy()
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce")
        work = work.dropna(subset=["date", "open", "close", "high", "low"]).reset_index(drop=True)
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
        work["ma365"] = work["close"].rolling(365).mean()
        today = work.iloc[-1]
        prev = work.iloc[-2]
        prev_prev = work.iloc[-3] if len(work) >= 3 else None
        ref_price = float(current_price or today["close"] or 0.0)
        prev_close = float(prev["close"] or 0.0)
        day_ret = (float(today["close"]) - prev_close) / prev_close if prev_close else 0.0
        prev_day_ret = (float(prev["close"]) - float(prev_prev["close"])) / float(prev_prev["close"]) if prev_prev is not None else 0.0
        ma5 = float(today["ma5"] or 0.0)
        ma10 = float(today["ma10"] or 0.0)
        ma20 = float(today["ma20"] or 0.0)
        ma30 = float(today["ma30"] or 0.0)
        ma60 = float(today["ma60"] or 0.0)
        ma120 = float(today["ma120"] or 0.0)
        ma150 = float(today["ma150"] or 0.0)
        ma180 = float(today["ma180"] or 0.0)
        ma250 = float(today["ma250"] or 0.0)
        ma365 = float(today["ma365"] or 0.0)
        ma5_prev = float(work.iloc[-6]["ma5"] or ma5) if len(work) >= 6 else ma5
        ma10_prev = float(work.iloc[-6]["ma10"] or ma10) if len(work) >= 6 else ma10
        ma20_prev = float(work.iloc[-6]["ma20"] or ma20) if len(work) >= 6 else ma20
        ma30_prev = float(work.iloc[-6]["ma30"] or ma30) if len(work) >= 6 else ma30
        ma60_prev = float(work.iloc[-6]["ma60"] or ma60) if len(work) >= 6 else ma60
        ma120_prev = float(work.iloc[-6]["ma120"] or ma120) if len(work) >= 6 else ma120
        ma150_prev = float(work.iloc[-6]["ma150"] or ma150) if len(work) >= 6 else ma150
        ma180_prev = float(work.iloc[-6]["ma180"] or ma180) if len(work) >= 6 else ma180
        ma250_prev = float(work.iloc[-6]["ma250"] or ma250) if len(work) >= 6 else ma250
        ma365_prev = float(work.iloc[-6]["ma365"] or ma365) if len(work) >= 6 else ma365
        ma5_slope = (ma5 - ma5_prev) / ma5_prev if ma5_prev else 0.0
        ma10_slope = (ma10 - ma10_prev) / ma10_prev if ma10_prev else 0.0
        ma20_slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev else 0.0
        ma30_slope = (ma30 - ma30_prev) / ma30_prev if ma30_prev else 0.0
        ma60_slope = (ma60 - ma60_prev) / ma60_prev if ma60_prev else 0.0
        ma120_slope = (ma120 - ma120_prev) / ma120_prev if ma120_prev else 0.0
        ma150_slope = (ma150 - ma150_prev) / ma150_prev if ma150_prev else 0.0
        ma180_slope = (ma180 - ma180_prev) / ma180_prev if ma180_prev else 0.0
        ma250_slope = (ma250 - ma250_prev) / ma250_prev if ma250_prev else 0.0
        ma365_slope = (ma365 - ma365_prev) / ma365_prev if ma365_prev else 0.0
        gap_to_ma5 = abs(ref_price - ma5) / ma5 if ma5 else 999.0
        gap_to_ma10 = abs(ref_price - ma10) / ma10 if ma10 else 999.0
        gap_to_ma20 = abs(ref_price - ma20) / ma20 if ma20 else 999.0
        gap_to_ma30 = abs(ref_price - ma30) / ma30 if ma30 else 999.0
        gap_to_ma60 = abs(ref_price - ma60) / ma60 if ma60 else 999.0
        gap_to_ma120 = abs(ref_price - ma120) / ma120 if ma120 else 999.0
        gap_to_ma150 = abs(ref_price - ma150) / ma150 if ma150 else 999.0
        gap_to_ma180 = abs(ref_price - ma180) / ma180 if ma180 else 999.0
        gap_to_ma250 = abs(ref_price - ma250) / ma250 if ma250 else 999.0
        gap_to_ma365 = abs(ref_price - ma365) / ma365 if ma365 else 999.0
        near_candidates = []
        for level_name, level, gap in [("MA5", ma5, gap_to_ma5), ("MA10", ma10, gap_to_ma10), ("MA20", ma20, gap_to_ma20), ("MA30", ma30, gap_to_ma30), ("MA60", ma60, gap_to_ma60), ("MA120", ma120, gap_to_ma120), ("MA150", ma150, gap_to_ma150), ("MA180", ma180, gap_to_ma180), ("MA250", ma250, gap_to_ma250), ("MA365", ma365, gap_to_ma365)]:
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
            "daily_price_ref": ref_price,
            "daily_prev_close": prev_close,
            "daily_prev_high": float(today["high"] or 0.0),
            "daily_prev_low": float(today["low"] or 0.0),
            "daily_prev_close_real": float(today["close"] or 0.0),  # 最新交易日收盘
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
            "daily_ma365": ma365,
            "daily_ma10_slope": ma10_slope,
            "daily_ma20_slope": ma20_slope,
            "daily_ma30_slope": ma30_slope,
            "daily_ma60_slope": ma60_slope,
            "daily_ma120_slope": ma120_slope,
            "daily_ma150_slope": ma150_slope,
            "daily_ma180_slope": ma180_slope,
            "daily_ma250_slope": ma250_slope,
            "daily_ma365_slope": ma365_slope,
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
    mode = "eod"
    try:
        from index_regime import detect_index_regime, get_regime_position_factor, index_regime_name
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


def get_daily_context(code: str, holding: dict, current_price: float = 0.0, as_of: Optional[str] = None) -> Dict[str, Any]:
    if not PARAMS.get("daily_context_enabled", True):
        return _default_daily_context(code, status="disabled", reason="参数关闭")
    cache_key = f"{code}_{as_of or get_today_str()}"
    cached = DAILY_CONTEXT_CACHE.get(cache_key)
    if isinstance(cached, dict):
        ts = cached.get("ts")
        ctx = cached.get("ctx")
        if isinstance(ts, datetime) and isinstance(ctx, dict):
            if (_now() - ts).total_seconds() < PARAMS["daily_cache_ttl_seconds"]:
                return ctx
    try:
        df = _fetch_daily_bar(code, is_etf=holding.get("type") == "etf", as_of=as_of)
        if df.empty:
            ctx = _default_daily_context(code, status="unavailable", reason="日线拉取为空")
        else:
            ctx = _build_daily_context_from_df(code, df, current_price=current_price)
        ctx = _attach_index_regime_context(ctx, code, as_of=as_of)
        DAILY_CONTEXT_CACHE[cache_key] = {"ts": _now(), "ctx": ctx}
        return ctx
    except Exception as e:
        ctx = _default_daily_context(code, status="error", reason=str(e)[:80])
        ctx = _attach_index_regime_context(ctx, code, as_of=as_of)
        DAILY_CONTEXT_CACHE[cache_key] = {"ts": _now(), "ctx": ctx}
        return ctx


def _fetch_benchmark_minute_bar(meta: Dict[str, str]) -> pd.DataFrame:
    symbol = meta.get("code", "")
    if not symbol:
        return pd.DataFrame()

    last_error = ""
    for attempt in range(2):
        try:
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.qq.com/"
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read().decode("utf-8", errors="ignore")
                if not content.strip() or "<html" in content.lower() or "<!doctype html" in content.lower():
                    raise ValueError("benchmark minute response invalid")
                data = json.loads(content)
            minute_data = data.get("data", {}).get(symbol) or data.get("data", {}).get(symbol.replace("sh", "")) or data.get("data", {}).get(symbol.replace("sz", ""))
            if not minute_data:
                raise ValueError("benchmark minute symbol missing")
            rows = minute_data.get("data") or minute_data.get("day") or []
            if isinstance(rows, dict):
                rows = rows.get("data") or []

            parsed = []
            today_str = _now().strftime("%Y-%m-%d")
            for row in rows:
                if isinstance(row, str):
                    parts = row.split()
                elif isinstance(row, list):
                    parts = [str(x) for x in row]
                else:
                    continue
                if len(parts) >= 4:
                    tm = str(parts[0]).strip()
                    close_p = float(parts[1])
                    vol = float(parts[2])
                    amount = float(parts[3]) if len(parts) > 3 else np.nan
                    open_p = high_p = low_p = close_p
                    if len(parts) >= 6:
                        open_p, close_p, high_p, low_p, vol = map(float, parts[1:6])
                        amount = float(parts[6]) if len(parts) > 6 else amount
                    if tm.isdigit() and len(tm) in (3, 4):
                        tm = tm.zfill(4)
                        ts = f"{today_str} {tm[:2]}:{tm[2:]}:00"
                    elif ":" in tm and len(tm) <= 5:
                        ts = f"{today_str} {tm}:00"
                    else:
                        ts = tm
                    parsed.append({"time": ts, "open": open_p, "close": close_p, "high": high_p, "low": low_p, "volume": vol, "amount": amount})
            df = pd.DataFrame(parsed)
            if df.empty:
                raise ValueError("benchmark minute empty")
            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
            return df
        except Exception as e:
            last_error = str(e)
            if attempt == 0:
                time.sleep(0.6)
    log.debug(f"⚠️  指数分钟线获取失败 {meta.get('name', symbol)}[{symbol}]: {last_error[:80]}")
    return pd.DataFrame()


def _resolve_benchmark_snapshot(code: str, holding: dict) -> Dict[str, Any]:
    meta = _benchmark_meta_for_code(code)
    df = _fetch_benchmark_minute_bar(meta)
    snapshot: Dict[str, Any] = {
        "benchmark_code": meta.get("code", ""),
        "benchmark_name": meta.get("name", ""),
        "benchmark_kind": meta.get("kind", ""),
        "benchmark_state": "unknown",
        "benchmark_gate": "neutral",
        "benchmark_gate_reason": "指数数据不足",
        "benchmark_price": 0.0,
        "benchmark_vwap": 0.0,
        "benchmark_today_ret": 0.0,
        "benchmark_vol_ratio": 0.0,
        "benchmark_momentum": 0.0,
    }
    if df.empty:
        return snapshot

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    price = float(last["close"]) if pd.notna(last.get("close")) else 0.0
    vwap = float(last["vwap"]) if pd.notna(last.get("vwap")) else price
    today_open = float(df[df["date"] == last["date"]].iloc[0]["open"]) if "date" in df.columns and not df[df["date"] == last["date"]].empty else price
    today_ret = (price - today_open) / today_open if today_open > 0 else 0.0
    vol_ratio = float(last["vol_ratio"]) if pd.notna(last.get("vol_ratio")) else 1.0
    momentum = float(last["macd_hist"]) if pd.notna(last.get("macd_hist")) else 0.0
    ema_spread = float(last["ema_spread"]) if pd.notna(last.get("ema_spread")) else 0.0
    day_amplitude = float(last["day_amplitude"]) if pd.notna(last.get("day_amplitude")) else 0.0
    state = "range_bound"
    if day_amplitude < PARAMS["min_amplitude"]:
        state = "dead_water"
    elif today_ret >= PARAMS["trend_today_ret_threshold"] and price >= vwap and ema_spread >= 0 and vol_ratio >= 1.05:
        state = "trend_up"
    elif today_ret <= -PARAMS["trend_today_ret_threshold"] and price <= vwap and ema_spread <= 0:
        state = "trend_down"
    elif price >= vwap and momentum >= 0:
        state = "bias_up"
    elif price <= vwap and momentum <= 0:
        state = "bias_down"

    benchmark_gate = "neutral"
    benchmark_reason = "指数中性"
    if state in {"trend_down", "bias_down"} and today_ret < 0:
        benchmark_gate = "weak"
        benchmark_reason = "指数偏弱，抬高买入门槛"
    elif state in {"trend_up", "bias_up"} and today_ret >= 0:
        benchmark_gate = "strong"
        benchmark_reason = "指数偏强，允许顺势低吸/加仓"
    elif state == "dead_water":
        benchmark_gate = "weak"
        benchmark_reason = "指数波动不足，谨慎出手"

    snapshot.update({
        "benchmark_state": state,
        "benchmark_gate": benchmark_gate,
        "benchmark_gate_reason": benchmark_reason,
        "benchmark_price": price,
        "benchmark_vwap": vwap,
        "benchmark_today_ret": today_ret,
        "benchmark_vol_ratio": vol_ratio,
        "benchmark_momentum": momentum,
        "benchmark_ema_spread": ema_spread,
        "benchmark_day_amplitude": day_amplitude,
        "benchmark_prev_close": float(prev["close"]) if pd.notna(prev.get("close")) else price,
    })
    return snapshot

def fetch_stock_name(code: str, is_etf: bool = False) -> str:
    if code in _name_cache: return _name_cache[code]
    try:
        api_code = clean_code(code)
        df = ak.fund_etf_spot_em() if is_etf else ak.stock_bid_ask_em(symbol=api_code)
        if is_etf:
            row = df[df["代码"] == api_code]
            name = row.iloc[0]["名称"] if not row.empty else code
        else:
            snap = dict(zip(df["item"], df["value"]))
            name = snap.get("股票简称") or snap.get("名称") or code
        _name_cache[code] = name
        return name
    except: return code
    if code in _name_cache: return _name_cache[code]
    try:
        df = ak.fund_etf_spot_em() if is_etf else ak.stock_bid_ask_em(symbol=code)
        if is_etf:
            row = df[df["代码"] == code]
            name = row.iloc[0]["名称"] if not row.empty else code
        else:
            snap = dict(zip(df["item"], df["value"]))
            name = snap.get("股票简称") or snap.get("名称") or code
        _name_cache[code] = name
        return name
    except: return code

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


def _strategy_memory_for_code(code: str) -> Dict[str, Any]:
    base = {}
    if isinstance(STRATEGY_MEMORY, dict):
        global_mem = STRATEGY_MEMORY.get("GLOBAL", {})
        if isinstance(global_mem, dict):
            base.update(global_mem)
        code_mem = STRATEGY_MEMORY.get(code, {})
        if isinstance(code_mem, dict):
            base.update(code_mem)
    return base


def _starvation_state_file() -> str:
    return os.path.join(T_IO_DIR, "buy_starvation_state.json")


def load_starvation_state() -> Dict[str, dict]:
    path = _starvation_state_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_starvation_state(state: Dict[str, dict]):
    try:
        with open(_starvation_state_file(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
    if not os.path.exists(HOLDINGS_FILE): return {}
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            holdings = json.load(f)
    except json.JSONDecodeError as e:
        log.error(f"❌ holdings.json 格式错误: {e}。请检查标点符号是否遗漏！")
        return {}

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
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

        if removed:
            log.info(f"🧹 清理过期分钟线缓存 {removed} 个")
    except Exception as e:
        log.debug(f"⚠️  清理缓存失败: {str(e)[:60]}")


def fetch_minute_bar(code: str, is_etf: bool = False) -> pd.DataFrame:
    """获取分钟线数据，优先使用本地缓存，再使用直连接口。"""
    market_date = _now().strftime("%Y-%m-%d")
    fetch_started = _now()
    MINUTE_FETCH_DETAIL[code] = ""
    api_code = clean_code(code)  # 去除 _A/_B 等后缀

    cached = _load_minute_cache(code, market_date)
    if not cached.empty:
        MINUTE_FETCH_STATUS[code] = "cache_hit"
        log.debug(f"♻️  {code} 命中分钟线缓存")
        _append_jsonl(_trace_path("data_quality", market_date), {
            "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
            "source": "cache",
            "minute_status": "cache_hit",
            "raw_rows": int(len(cached)),
            "parsed_rows": int(len(cached)),
            "valid_rows": int(len(cached)),
            "fetch_cost_ms": int((_now() - fetch_started).total_seconds() * 1000),
        })
        return cached

    last_error = ""
    for attempt in range(3):
        try:
            market = "sh" if api_code.startswith(("5", "6", "9")) else "sz"
            symbol = f"{market}{api_code}"
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.qq.com/"
            }

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read().decode("utf-8", errors="ignore")
                if not content.strip():
                    MINUTE_FETCH_STATUS[code] = "json_empty"
                    MINUTE_FETCH_DETAIL[code] = "响应体为空"
                    raise ValueError("empty response body")
                if "<html" in content.lower() or "<!doctype html" in content.lower():
                    MINUTE_FETCH_STATUS[code] = "json_html"
                    MINUTE_FETCH_DETAIL[code] = "响应像HTML拦截页"
                    raise ValueError("html response body")
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    MINUTE_FETCH_STATUS[code] = "json_error"
                    MINUTE_FETCH_DETAIL[code] = f"非JSON响应: {content[:80]}"
                    raise ValueError("json decode error")

            if data.get("code") != 0 or not data.get("data"):
                MINUTE_FETCH_STATUS[code] = "api_empty"
                MINUTE_FETCH_DETAIL[code] = f"返回code={data.get('code')} data为空"
                raise ValueError("minute api returned empty data")

            minute_data = data["data"].get(symbol) or data["data"].get(api_code)
            if not minute_data:
                MINUTE_FETCH_STATUS[code] = "symbol_missing"
                MINUTE_FETCH_DETAIL[code] = f"data中未找到{symbol}或{api_code}"
                raise ValueError("minute api missing symbol data")

            rows = minute_data.get("data") or minute_data.get("day") or []
            if isinstance(rows, dict):
                rows = rows.get("data") or []

            parsed = []
            today_str = _now().strftime("%Y-%m-%d")
            total_rows = len(rows) if hasattr(rows, "__len__") else 0
            if total_rows == 1 and isinstance(rows[0], str) and rows[0].strip() == "0":
                MINUTE_FETCH_STATUS[code] = "parse_zero_placeholder"
                MINUTE_FETCH_DETAIL[code] = "接口返回占位0行，不是有效分钟数据"
                raise ValueError("minute api returned zero placeholder")
            short_rows = 0
            type_rows = 0
            parse_fail_rows = 0
            derived_ohlc_rows = 0
            for row in rows:
                try:
                    if isinstance(row, str):
                        parts = row.split()
                    elif isinstance(row, list):
                        parts = [str(x) for x in row]
                    else:
                        type_rows += 1
                        continue

                    if len(parts) >= 6:
                        tm = parts[0]
                        open_p, close_p, high_p, low_p, vol = map(float, parts[1:6])
                        amount = float(parts[6]) if len(parts) > 6 else np.nan
                    elif len(parts) >= 4:
                        tm = parts[0]
                        close_p = float(parts[1])
                        vol = float(parts[2])
                        amount = float(parts[3]) if len(parts) > 3 else np.nan
                        open_p = high_p = low_p = close_p
                        derived_ohlc_rows += 1
                    else:
                        short_rows += 1
                        continue

                    tm = str(tm).strip()
                    if tm.isdigit() and len(tm) in (3, 4):
                        tm = tm.zfill(4)
                        ts = f"{today_str} {tm[:2]}:{tm[2:]}:00"
                    elif ":" in tm and len(tm) <= 5:
                        ts = f"{today_str} {tm}:00"
                    else:
                        ts = tm

                    parsed.append({
                        "time": ts,
                        "open": open_p,
                        "close": close_p,
                        "high": high_p,
                        "low": low_p,
                        "volume": vol,
                        "amount": amount,
                    })
                except Exception:
                    parse_fail_rows += 1
                    continue

            df = pd.DataFrame(parsed)
            if df.empty:
                if total_rows == 0:
                    MINUTE_FETCH_STATUS[code] = "parse_no_rows"
                    MINUTE_FETCH_DETAIL[code] = "接口返回0行分钟数据"
                elif short_rows == total_rows:
                    MINUTE_FETCH_STATUS[code] = "parse_short_rows"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，全部字段不足4列"
                elif type_rows == total_rows:
                    MINUTE_FETCH_STATUS[code] = "parse_type_rows"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，全部为不支持的行类型"
                elif parse_fail_rows == total_rows:
                    MINUTE_FETCH_STATUS[code] = "parse_value_error"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，全部在数值转换时失败"
                else:
                    MINUTE_FETCH_STATUS[code] = "parse_empty"
                    MINUTE_FETCH_DETAIL[code] = f"原始行数{total_rows}，短行{short_rows}，类型行{type_rows}，解析失败{parse_fail_rows}"
                raise ValueError("no parsed minute rows")

            df["time"] = pd.to_datetime(df["time"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
            _save_minute_cache(code, market_date, df)
            MINUTE_FETCH_STATUS[code] = "ok"
            MINUTE_FETCH_DETAIL[code] = f"解析成功{len(df)}行，4列行{derived_ohlc_rows}，跳过短行{short_rows}，类型行{type_rows}，解析失败{parse_fail_rows}"
            _append_jsonl(_trace_path("data_quality", market_date), {
                "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code,
                "source": "api",
                "minute_status": "ok",
                "raw_rows": int(total_rows),
                "parsed_rows": int(len(df)),
                "valid_rows": int(len(df)),
                "short_rows": int(short_rows),
                "type_rows": int(type_rows),
                "parse_fail_rows": int(parse_fail_rows),
                "derived_ohlc_rows": int(derived_ohlc_rows),
                "fetch_cost_ms": int((_now() - fetch_started).total_seconds() * 1000),
            })
            return df

        except urllib.error.URLError as e:
            last_error = str(e)
            reason = getattr(e, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in last_error.lower():
                MINUTE_FETCH_STATUS[code] = "network_timeout"
                MINUTE_FETCH_DETAIL[code] = f"请求超时: {last_error[:80]}"
            elif isinstance(reason, OSError):
                err_text = str(reason).lower()
                if "name or service not known" in err_text or "temporary failure" in err_text or "dns" in err_text:
                    MINUTE_FETCH_STATUS[code] = "network_dns"
                    MINUTE_FETCH_DETAIL[code] = f"DNS解析失败: {last_error[:80]}"
                elif "ssl" in err_text or "certificate" in err_text:
                    MINUTE_FETCH_STATUS[code] = "network_ssl"
                    MINUTE_FETCH_DETAIL[code] = f"SSL握手失败: {last_error[:80]}"
                else:
                    MINUTE_FETCH_STATUS[code] = "network_error"
                    MINUTE_FETCH_DETAIL[code] = f"网络错误: {last_error[:80]}"
            elif hasattr(reason, "code"):
                MINUTE_FETCH_STATUS[code] = "network_http"
                MINUTE_FETCH_DETAIL[code] = f"HTTP错误{getattr(reason, 'code', '')}: {last_error[:80]}"
            else:
                MINUTE_FETCH_STATUS[code] = "network_error"
                MINUTE_FETCH_DETAIL[code] = f"网络错误: {last_error[:80]}"
        except Exception as e:
            last_error = str(e)
            if MINUTE_FETCH_STATUS.get(code) not in {"json_empty", "json_html", "json_error", "api_empty", "symbol_missing", "parse_no_rows", "parse_short_rows", "parse_type_rows", "parse_value_error", "parse_zero_placeholder", "parse_empty"}:
                MINUTE_FETCH_STATUS[code] = "network_error"
                MINUTE_FETCH_DETAIL[code] = f"其他异常: {last_error[:80]}"
        if attempt < 2:
            time.sleep(0.8)

    log.warning(f"⚠️  {code} 分钟线获取失败[{MINUTE_FETCH_STATUS.get(code, 'unknown')}]: {MINUTE_FETCH_DETAIL.get(code, last_error[:60])}")
    return pd.DataFrame()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 2:
        return df
    c = df["close"]

    delta = c.diff()
    gain = delta.clip(lower=0).rolling(PARAMS["rsi_period"], min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(PARAMS["rsi_period"], min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

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
    df_15min["rsi_15m"] = 100 - 100 / (1 + rs)

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
