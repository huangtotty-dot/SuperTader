from dataclasses import dataclass, field

@dataclass
class PreOpenContext:
    market_score: float = 0.0
    market_bias: str = "unknown"
    breadth: Dict[str, Any] = field(default_factory=dict)
    theme_rank: List[Dict[str, Any]] = field(default_factory=list)
    focus_codes: List[str] = field(default_factory=list)
    active_codes: List[str] = field(default_factory=list)
    watch_codes: List[str] = field(default_factory=list)
    blocked_codes: List[str] = field(default_factory=list)
    favored_sectors: List[str] = field(default_factory=list)
    weak_sectors: List[str] = field(default_factory=list)
    session_note: str = ""
    generated_at: str = ""
    source: str = "offline"
    market_snapshot: Dict[str, Any] = field(default_factory=dict)
    code_snapshots: Dict[str, Any] = field(default_factory=dict)
    auction_summary: Dict[str, Any] = field(default_factory=dict)


class PreOpenEngine:
    def __init__(self, holdings: Dict[str, dict], watchlist: Dict[str, dict]):
        self.holdings = holdings or {}
        self.watchlist = watchlist or {}

    def _sector_text(self, code: str, holding: dict) -> str:
        meta = self.watchlist.get(code, {}) if isinstance(self.watchlist, dict) else {}
        sector = meta.get("sector") or holding.get("sector") or ""
        return str(sector or "")

    def _build_theme_rank(self) -> List[Dict[str, Any]]:
        sector_counter: Dict[str, Dict[str, Any]] = {}
        for code, holding in self.holdings.items():
            sector_text = self._sector_text(code, holding)
            if not sector_text:
                continue
            parts = [p.strip() for p in sector_text.split("/") if p.strip()]
            if not parts:
                parts = [sector_text]
            score_base = 1.0 + float(holding.get("t_qty", 0) > 0)
            for part in parts:
                bucket = sector_counter.setdefault(part, {"sector": part, "count": 0, "score": 0.0, "codes": []})
                bucket["count"] += 1
                bucket["score"] += score_base
                bucket["codes"].append(code)
        ranked = sorted(sector_counter.values(), key=lambda x: (x["score"], x["count"]), reverse=True)
        return ranked

    def _pick_focus(self, theme_rank: List[Dict[str, Any]]) -> tuple[list[str], list[str], list[str], list[str]]:
        focus_codes: List[str] = []
        watch_codes: List[str] = []
        active_codes: List[str] = []
        blocked_codes: List[str] = []
        for item in theme_rank[:2]:
            for code in item.get("codes", [])[:4]:
                if code not in active_codes:
                    active_codes.append(code)
        for item in theme_rank[2:4]:
            for code in item.get("codes", [])[:4]:
                if code not in active_codes and code not in watch_codes:
                    watch_codes.append(code)
        for item in theme_rank[4:6]:
            for code in item.get("codes", [])[:4]:
                if code not in active_codes and code not in watch_codes and code not in focus_codes:
                    focus_codes.append(code)
        if theme_rank:
            weak_pool = theme_rank[-3:]
            for item in weak_pool:
                for code in item.get("codes", [])[:2]:
                    if code not in active_codes and code not in watch_codes and code not in focus_codes and code not in blocked_codes:
                        blocked_codes.append(code)
        return active_codes, watch_codes, focus_codes, blocked_codes

    def _auction_target_codes(self, active_codes: List[str], watch_codes: List[str], focus_codes: List[str]) -> List[str]:
        ordered: List[str] = []
        for code in list(active_codes or []) + list(watch_codes or []) + list(focus_codes or []) + list(self.holdings.keys()):
            code = str(code or "").strip()
            if code and code not in ordered:
                ordered.append(code)
        return ordered[:40]

    def _preopen_qt_symbol(self, code: str) -> str:
        code = str(code or "").strip()
        market = "sh" if code.startswith(("5", "6", "9")) else "sz"
        return f"{market}{code}"

    def _parse_qt_snapshot_line(self, line: str) -> tuple[str, Dict[str, Any]]:
        line = str(line or "").strip()
        if not line or "=\"" not in line:
            return "", {}
        symbol = line.split("=", 1)[0].strip()
        payload = line.split("=", 1)[1].strip().strip(';').strip('"')
        fields = payload.split("~")
        if len(fields) < 8:
            return "", {}
        code = str(fields[2]).strip() or symbol[-6:]
        try:
            price = float(fields[3] or 0)
        except Exception:
            price = 0.0
        try:
            volume = float(fields[6] or 0)
        except Exception:
            volume = 0.0
        amount = 0.0
        turnover_raw = ""
        for field in fields:
            parts = str(field).strip().split("/")
            if len(parts) == 3:
                try:
                    amount = float(parts[2] or 0)
                    turnover_raw = parts[2].strip()
                    break
                except Exception:
                    continue
        if amount <= 0:
            try:
                amount = float(fields[7] or 0) * 10000.0
                turnover_raw = str(fields[7]).strip()
            except Exception:
                amount = 0.0
        return code, {
            "symbol": symbol,
            "name": str(fields[1]).strip(),
            "price": price,
            "volume": volume,
            "amount": amount,
            "turnover_raw": turnover_raw,
            "source": "qt",
        }

    def _fetch_auction_snapshot_map(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        snapshot_map: Dict[str, Dict[str, Any]] = {}
        symbols = [self._preopen_qt_symbol(code) for code in codes if str(code or "").strip()]
        if not symbols:
            return snapshot_map
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.qq.com/",
        }
        for chunk in chunk_list(symbols, 40):
            url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
            text = ""
            try:
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()
                text = response.text or ""
            except Exception as e:
                log.debug(f"⚠️  集合竞价快照抓取失败: {type(e).__name__}: {str(e)[:120]}")
                continue
            if not text.strip():
                continue
            for line in text.splitlines():
                code, data = self._parse_qt_snapshot_line(line)
                if not code or not data:
                    continue
                if data.get("price", 0) <= 0:
                    continue
                snapshot_map[code] = data
        return snapshot_map

    def _build_auction_profile(self, code: str, holding: dict, raw: Dict[str, Any]) -> Dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        price = float(raw.get("price", 0) or 0)
        amount = float(raw.get("amount", 0) or 0)
        volume = float(raw.get("volume", 0) or 0)
        daily_ctx = get_daily_context(code, holding or {}, current_price=price)
        prev_close = float(daily_ctx.get("daily_prev_close", 0) or 0)
        if prev_close <= 0:
            prev_close = float(daily_ctx.get("daily_price_ref", 0) or 0)
        open_gap = (price - prev_close) / prev_close if prev_close else 0.0
        gate = str(daily_ctx.get("daily_gate", "neutral") or "neutral")
        trend_bg = str(daily_ctx.get("daily_trend_bg", "unknown") or "unknown")
        score = 0.0
        if prev_close > 0 and price > 0:
            if 0.0 <= open_gap <= 0.03:
                score += 18.0
            elif 0.03 < open_gap <= 0.07:
                score += 10.0
            elif open_gap < -0.03:
                score -= 15.0
            else:
                score -= 6.0
        else:
            score -= 8.0
        if amount > 0:
            if amount >= 1.5e8:
                score += 12.0
            elif amount >= 5e7:
                score += 7.0
            elif amount >= 1e7:
                score += 2.0
            else:
                score -= 5.0
        if gate in {"supportive", "neutral"}:
            score += 6.0
        elif gate in {"risk", "overheat"}:
            score -= 10.0
        if trend_bg in {"bull", "uptrend"}:
            score += 6.0
        elif trend_bg in {"weak_breakdown", "downtrend"}:
            score -= 10.0
        if bool(daily_ctx.get("daily_ma_clustered")) and open_gap >= 0:
            score += 4.0
        if bool(daily_ctx.get("daily_hard_breakdown")):
            score -= 12.0
        if bool(daily_ctx.get("daily_breakdown_risk")):
            score -= 8.0
        data_quality = 1.0
        if price <= 0:
            data_quality -= 0.5
        if prev_close <= 0:
            data_quality -= 0.3
        if amount <= 0:
            data_quality -= 0.2
        if volume <= 0:
            data_quality -= 0.1
        data_quality = float(_clamp(data_quality, 0.0, 1.0))
        auction_tag = "flat_open"
        if data_quality < 0.5:
            auction_tag = "stale_or_missing"
        elif score >= 18:
            auction_tag = "strong_open"
        elif score <= -12:
            auction_tag = "weak_open"
        return {
            "code": code,
            "name": raw.get("name", holding.get("name", code)),
            "price": price,
            "prev_close": prev_close,
            "open_gap": open_gap,
            "volume": volume,
            "amount": amount,
            "auction_score": score,
            "auction_tag": auction_tag,
            "data_quality": data_quality,
            "daily_gate": gate,
            "daily_trend_bg": trend_bg,
            "daily_pullback_support": bool(daily_ctx.get("daily_pullback_support")),
            "daily_near_support": bool(daily_ctx.get("daily_near_support")),
            "daily_breakdown_risk": bool(daily_ctx.get("daily_breakdown_risk")),
            "daily_hard_breakdown": bool(daily_ctx.get("daily_hard_breakdown")),
            "source": raw.get("source", "qt"),
            "raw": raw,
            "daily_context": daily_ctx,
        }

    def _sort_codes_by_auction_score(self, codes: List[str], profiles: Dict[str, Dict[str, Any]]) -> List[str]:
        return sorted(
            [str(code) for code in codes if str(code or "").strip()],
            key=lambda c: (-float(profiles.get(c, {}).get("auction_score", -999.0) or -999.0), c)
        )

    def _fetch_market_snapshot(self) -> Dict[str, Any]:
        snapshot = {
            "source": "watchlist",
            "market_open": False,
            "index_trend": "unknown",
            "advance_decline": "unknown",
            "hot_theme": [],
            "risk_flag": "unknown",
            "market_sentence": "",
        }
        try:
            spot = pd.DataFrame()
            for fn in ["stock_zh_a_spot_em", "stock_zh_a_spot"]:
                if hasattr(ak, fn):
                    try:
                        spot = getattr(ak, fn)()
                        if isinstance(spot, pd.DataFrame) and not spot.empty:
                            break
                    except Exception:
                        continue
            if isinstance(spot, pd.DataFrame) and not spot.empty:
                snapshot["source"] = "spot"
                cols = set(spot.columns)
                if {"涨跌幅", "名称"}.issubset(cols):
                    up = int((pd.to_numeric(spot["涨跌幅"], errors="coerce") > 0).sum())
                    down = int((pd.to_numeric(spot["涨跌幅"], errors="coerce") < 0).sum())
                    flat = int(len(spot) - up - down)
                    snapshot["advance_decline"] = {"up": up, "down": down, "flat": flat}
                    snapshot["risk_flag"] = "risk_on" if up >= max(1, down * 1.2) else ("risk_off" if down > up else "neutral")
                if {"涨跌幅", "概念板块"}.issubset(cols):
                    concept_df = spot[["名称", "涨跌幅"]].copy()
                    concept_df["涨跌幅"] = pd.to_numeric(concept_df["涨跌幅"], errors="coerce")
                    top = concept_df.sort_values("涨跌幅", ascending=False).head(5)
                    snapshot["hot_theme"] = top["名称"].dropna().astype(str).tolist()
                    if not top.empty:
                        snapshot["index_trend"] = "positive" if float(top.iloc[0]["涨跌幅"] or 0) > 0 else "negative"
                elif "代码" in cols and "名称" in cols:
                    top = spot.head(5)
                    snapshot["hot_theme"] = top["名称"].dropna().astype(str).tolist()
        except Exception:
            pass
        if not snapshot["market_sentence"]:
            adv = snapshot.get("advance_decline", {})
            if isinstance(adv, dict) and adv and adv.get("up") is not None:
                snapshot["market_sentence"] = f"涨{adv.get('up', 0)} / 跌{adv.get('down', 0)} / 平{adv.get('flat', 0)}"
            else:
                snapshot["market_sentence"] = "市场快照不足，按名单结构解读"
        return snapshot

    def evaluate(self) -> PreOpenContext:
        market_snapshot_raw = self._fetch_market_snapshot()
        market_snapshot = market_snapshot_raw if isinstance(market_snapshot_raw, dict) else {}
        theme_rank = self._build_theme_rank()
        active_codes, watch_codes, focus_codes, blocked_codes = self._pick_focus(theme_rank)
        total = max(1, len(self.holdings))
        etf_count = sum(1 for h in self.holdings.values() if h.get("type") == "etf")
        stock_count = total - etf_count
        concentrated = theme_rank[0]["count"] / total if theme_rank else 0.0
        market_score = 40.0 + min(30.0, concentrated * 30.0) + min(10.0, etf_count * 1.5) - min(8.0, stock_count * 0.1)
        if len(theme_rank) >= 2 and theme_rank[0]["count"] > theme_rank[1]["count"]:
            market_score += 4.0
        if theme_rank and theme_rank[0]["count"] >= max(3, total // 3):
            market_score += 6.0
        if theme_rank and theme_rank[0]["count"] <= 1:
            market_score -= 8.0
        market_score = float(_clamp(market_score, 0, 100))
        if market_score >= 72:
            market_bias = "risk_on"
        elif market_score >= 58:
            market_bias = "neutral_to_positive"
        elif market_score <= 38:
            market_bias = "risk_off"
        else:
            market_bias = "neutral"

        favored_sectors = [item["sector"] for item in theme_rank[:3]]
        weak_sectors = [item["sector"] for item in theme_rank[-3:]] if theme_rank else []
        market_open = bool(market_snapshot.get("market_open", False))
        risk_flag = str(market_snapshot.get("risk_flag", "unknown"))
        adv = market_snapshot.get("advance_decline", {}) if isinstance(market_snapshot, dict) else {}
        up = int(adv.get("up", 0) or 0) if isinstance(adv, dict) else 0
        down = int(adv.get("down", 0) or 0) if isinstance(adv, dict) else 0
        hot_theme = market_snapshot.get("hot_theme", []) if isinstance(market_snapshot, dict) else []
        hot_theme_text = "、".join(hot_theme[:3]) if isinstance(hot_theme, list) else str(hot_theme)

        target_codes = self._auction_target_codes(active_codes, watch_codes, focus_codes)
        raw_snapshots = self._fetch_auction_snapshot_map(target_codes)
        code_snapshots: Dict[str, Dict[str, Any]] = {}
        for code in target_codes:
            holding = self.holdings.get(code, {}) if isinstance(self.holdings, dict) else {}
            code_snapshots[code] = self._build_auction_profile(code, holding, raw_snapshots.get(code, {}))

        auction_scores = [float(item.get("auction_score", 0.0) or 0.0) for item in code_snapshots.values() if item.get("data_quality", 0.0) >= 0.4]
        auction_mean_score = float(sum(auction_scores) / max(1, len(auction_scores))) if auction_scores else 0.0
        strong_open_count = sum(1 for item in code_snapshots.values() if item.get("auction_tag") == "strong_open")
        weak_open_count = sum(1 for item in code_snapshots.values() if item.get("auction_tag") == "weak_open")
        missing_open_count = sum(1 for item in code_snapshots.values() if item.get("auction_tag") == "stale_or_missing")
        auction_summary = {
            "target_count": len(target_codes),
            "snapshot_count": len(raw_snapshots),
            "strong_open_count": strong_open_count,
            "weak_open_count": weak_open_count,
            "missing_open_count": missing_open_count,
            "mean_score": round(auction_mean_score, 2),
            "source": "qt",
            "source_ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        market_score += min(6.0, max(-6.0, auction_mean_score / 6.0))
        if strong_open_count >= max(1, len(target_codes) // 8):
            market_score += 2.0
        if weak_open_count >= max(1, len(target_codes) // 6):
            market_score -= 2.5
        market_score += min(10.0, max(-6.0, (up - down) / max(1, total) * 1.2))
        if risk_flag == "risk_on":
            market_score += 4.0
        elif risk_flag == "risk_off":
            market_score -= 5.0
        if market_open and up > down:
            market_score += 3.0
        elif market_open and down > up:
            market_score -= 3.0
        if hot_theme:
            market_score += 2.0 if len(hot_theme) >= 3 else 0.5
        market_score = float(_clamp(market_score, 0, 100))
        if market_score >= 72:
            market_bias = "risk_on"
        elif market_score >= 58:
            market_bias = "neutral_to_positive"
        elif market_score <= 38:
            market_bias = "risk_off"
        else:
            market_bias = "neutral"

        active_codes = self._sort_codes_by_auction_score(active_codes, code_snapshots)
        watch_codes = self._sort_codes_by_auction_score(watch_codes, code_snapshots)
        focus_codes = self._sort_codes_by_auction_score(focus_codes, code_snapshots)
        blocked_codes = self._sort_codes_by_auction_score(blocked_codes, code_snapshots)

        breadth = {
            "total_codes": total,
            "etf_count": etf_count,
            "stock_count": stock_count,
            "theme_count": len(theme_rank),
            "top_theme_share": round(concentrated, 3),
            "advance_decline": adv,
            "hot_theme": hot_theme,
            "risk_flag": risk_flag,
            "market_open": market_open,
            "hot_theme_text": hot_theme_text,
            "auction_summary": auction_summary,
        }
        session_note = (
            f"盘面快照 {market_snapshot.get('market_sentence', '暂无')}"
            if market_open or up or down or hot_theme
            else "盘面偏强，适合顺势低吸" if market_bias in {"risk_on", "neutral_to_positive"} else ("盘面偏弱，优先控仓等待" if market_bias == "risk_off" else "盘面中性，按信号择机")
        )
        if auction_summary["target_count"]:
            session_note += f" | 竞价强{strong_open_count}/弱{weak_open_count}/缺{missing_open_count}"
        return PreOpenContext(
            market_score=market_score,
            market_bias=market_bias,
            breadth=breadth,
            theme_rank=theme_rank[:8],
            focus_codes=focus_codes,
            active_codes=active_codes,
            watch_codes=watch_codes,
            blocked_codes=blocked_codes,
            favored_sectors=favored_sectors,
            weak_sectors=weak_sectors,
            session_note=session_note,
            generated_at=_now().strftime("%Y-%m-%d %H:%M:%S"),
            source=market_snapshot.get("source", "watchlist"),
            market_snapshot=market_snapshot,
            code_snapshots=code_snapshots,
            auction_summary=auction_summary,
        )

    def persist(self, context: PreOpenContext) -> None:
        try:
            os.makedirs(PREOPEN_DIR, exist_ok=True)
            with open(_preopen_path(), "w", encoding="utf-8") as f:
                json.dump(context.__dict__, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _trend_reason_label(path: str) -> str:
    mapping = {
        "sell_fast_path": "卖出快速路径",
        "sell_confirm_path": "卖出确认路径",
        "buy_path": "买入路径",
        "buy_soft_path": "买入软确认路径",
        "hold": "持有",
    }
    return mapping.get(path, path or "持有")


def _buy_soft_support_count(buy_momentum_ok: bool, buy_ema_ok: bool, buy_volume_ok: bool, buy_price_ok: bool, buy_gap_ok: bool, buy_detail_count_ok: bool, buy_time_ready: bool, buy_15m_ok: bool = True, buy_5m_ok: bool = True) -> int:
    return sum([buy_momentum_ok, buy_ema_ok, buy_volume_ok, buy_price_ok, buy_gap_ok, buy_detail_count_ok, buy_time_ready, buy_15m_ok, buy_5m_ok])


def _starvation_state_file() -> str:
    return os.path.join(T_IO_DIR, "buy_starvation_state.json")


def _merge_memory(code: str, updates: dict):
    current = STRATEGY_MEMORY.get(code, {}) if isinstance(STRATEGY_MEMORY, dict) else {}
    merged = dict(current)
    sample_count = int(merged.get("sample_count", 0) or 0) + 1
    merged["sample_count"] = sample_count
    if sample_count < 3:
        return
    for key, value in updates.items():
        if isinstance(value, (int, float)):
            base = merged.get(key, value)
            if not isinstance(base, (int, float)):
                base = value
            merged[key] = base * 0.85 + value * 0.15
        else:
            merged[key] = value
    STRATEGY_MEMORY[code] = merged
    try:
        with open(LEARNING_FILE, "w", encoding="utf-8") as f:
            json.dump(STRATEGY_MEMORY, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _learning_state_file() -> str:
    return os.path.join(T_IO_DIR, "t_trader_learning_state.json")


def load_learning_state() -> Dict[str, dict]:
    path = _learning_state_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_learning_state(state: Dict[str, dict]):
    try:
        with open(_learning_state_file(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _replay_health(stats: dict) -> float:
    return float(
        stats.get("buy_ok", 0) * 2
        + stats.get("sell_ok", 0) * 2
        + stats.get("rebuild_buy_ok", 0)
        - stats.get("buy_candidate_but_rejected", 0)
        - stats.get("preempt_by_sell_fast_path", 0)
        - stats.get("buy_blocked", 0) * 0.2
        - stats.get("sell_blocked", 0) * 0.2
    )


def _dominant_reason(counter: dict) -> tuple[str, int]:
    if not counter:
        return "", 0
    reason, count = max(counter.items(), key=lambda kv: kv[1])
    return str(reason), int(count)


def _learning_patch_for_code(code: str, code_stats: dict) -> tuple[dict, str]:
    buy_reason, buy_count = _dominant_reason(code_stats.get("buy_block_by_reason", {}))
    sell_reason, sell_count = _dominant_reason(code_stats.get("sell_block_by_reason", {}))
    preempt = int(code_stats.get("preempt_by_sell_fast_path", 0) or 0)
    cand_rej = int(code_stats.get("buy_candidate_but_rejected", 0) or 0)

    patch = {}
    reason = ""
    if preempt > 0 and preempt >= max(2, cand_rej // 3):
        patch = {"buy_priority_margin": 3}
        reason = "卖快路径抢占"
    elif buy_reason in {"buy_momentum_fail", "buy_volume_fail", "buy_ema_fail"} and buy_count > 0:
        if buy_reason == "buy_momentum_fail":
            patch = {"buy_confirm_min_seconds": 20}
        elif buy_reason == "buy_volume_fail":
            patch = {"vol_ratio_confirm": 1.7}
        else:
            patch = {"buy_confirm_min_factors": 2}
        reason = buy_reason
    elif buy_reason in {"buy_confirm_wait", "buy_detail_fail", "buy_gap_fail", "buy_price_fail", "post_sell_block"} and buy_count > 0:
        if buy_reason == "buy_confirm_wait":
            patch = {"buy_confirm_min_seconds": 15}
        elif buy_reason == "buy_detail_fail":
            patch = {"buy_confirm_min_factors": 2}
        elif buy_reason == "buy_gap_fail":
            patch = {"buy_rebound_min_score_gap": 2}
        elif buy_reason == "buy_price_fail":
            patch = {"buy_confirm_min_score": 40}
        else:
            patch = {"post_sell_rebuild_min_seconds": 20}
        reason = buy_reason
    elif sell_reason in {"sell_confirm_wait", "sell_detail_fail", "sell_momentum_fail", "sell_ema_fail", "sell_volume_fail"} and sell_count > 0:
        if sell_reason == "sell_confirm_wait":
            patch = {"sell_confirm_min_seconds": 25}
        elif sell_reason == "sell_detail_fail":
            patch = {"sell_confirm_min_factors": 4}
        elif sell_reason == "sell_momentum_fail":
            patch = {"sell_needs_momentum": True}
        elif sell_reason == "sell_ema_fail":
            patch = {"sell_needs_ema": True}
        else:
            patch = {"sell_needs_volume": True}
        reason = sell_reason
    elif cand_rej > 0:
        patch = {"buy_confirm_min_score": 40}
        reason = "买候选未成交"

    return patch, reason


def _apply_replay_learning(today: str):
    replay_file = os.path.join(TRACE_DIR, f"replay_compare_{today}.json")
    if not os.path.exists(replay_file):
        return
    try:
        with open(replay_file, "r", encoding="utf-8") as f:
            replay_data = json.load(f)
    except Exception:
        return

    stats = replay_data.get("stats", {}) if isinstance(replay_data, dict) else {}
    by_code = stats.get("by_code", {}) if isinstance(stats, dict) else {}
    if not isinstance(by_code, dict) or not by_code:
        return

    learning_state = load_learning_state()
    changed = []
    for code, code_stats in by_code.items():
        if not isinstance(code_stats, dict):
            continue
        current_health = _replay_health(code_stats)
        state = learning_state.get(code, {}) if isinstance(learning_state, dict) else {}
        last_health = float(state.get("last_health", -9999) or -9999)
        last_patch = state.get("last_patch", {}) if isinstance(state, dict) else {}
        last_snapshot = state.get("last_snapshot", {}) if isinstance(state, dict) else {}

        if last_patch and current_health < last_health:
            if last_snapshot:
                STRATEGY_MEMORY[code] = dict(last_snapshot)
                changed.append(f"{code} 回滚上次学习补丁")
            learning_state[code] = {
                "last_health": current_health,
                "last_patch": {},
                "last_snapshot": dict(STRATEGY_MEMORY.get(code, {})),
                "last_date": today,
                "rollbacked": True,
                "rollback_reason": "health_down",
            }
            continue

        patch, reason = _learning_patch_for_code(code, code_stats)
        if not patch:
            learning_state[code] = {
                "last_health": current_health,
                "last_patch": {},
                "last_snapshot": dict(STRATEGY_MEMORY.get(code, {})),
                "last_date": today,
                "reason": "",
            }
            continue

        current_memory = dict(STRATEGY_MEMORY.get(code, {}))
        snapshot = dict(current_memory)
        updated = dict(current_memory)
        updated.update(patch)
        STRATEGY_MEMORY[code] = updated
        changed.append(f"{code} {reason} -> {patch}")
        learning_state[code] = {
            "last_health": current_health,
            "last_patch": patch,
            "last_snapshot": snapshot,
            "last_date": today,
            "reason": reason,
        }

    if changed:
        save_learning_state(learning_state)
        try:
            with open(LEARNING_FILE, "w", encoding="utf-8") as f:
                json.dump(STRATEGY_MEMORY, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        log.info("学习更新: " + " | ".join(changed[:10]))


def log_eod_summary():
    global _eod_logged_date
    today = get_today_str()
    if _eod_logged_date == today: return

    lines = ["\n" + "═"*20 + " 【尾盘做T盈亏清算】 " + "═"*20]
    total_net_pnl = 0.0
    misjudge_counter = {"buy_early": {}, "sell_early": {}, "missed_buy": {}, "missed_sell": {}}
    misjudge_factor_counter = {"buy_early": {}, "sell_early": {}, "missed_buy": {}, "missed_sell": {}}
    action_misjudge_phase_factor_counter = {
        "BUY_LOW": {"early": {}, "missed": {}},
        "ADD_POS": {"early": {}, "missed": {}},
        "SELL_HIGH": {"early": {}, "missed": {}},
        "PANIC_SELL": {"early": {}, "missed": {}},
    }
    etf_review = []
    stock_review = []
    minute_issues = []
    minute_issue_stats = {"网络": {}, "解析": {}, "接口": {}, "缓存": {}, "其他": {}}

    def _brief_reasons(text: str, limit: int = 3) -> str:
        parts = [x for x in str(text or "").split(" + ") if x]
        if not parts:
            return "综合指标达标"
        short = " + ".join(parts[:limit])
        return short + ("..." if len(parts) > limit else "")

    def _bump(counter: dict, key: str):
        counter[key] = counter.get(key, 0) + 1

    action_bucket_counts = {
        "buy_low": 0,
        "buy_add": 0,
        "sell_high": 0,
        "panic_sell": 0,
    }
    action_bucket_qty = {
        "buy_low": 0,
        "buy_add": 0,
        "sell_high": 0,
        "panic_sell": 0,
    }
    for code, holding in HOLDINGS.items():
        dec = _ensure_daily_decision_stats(code, holding)
        buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
        sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
        buy_amt = sum(t["qty"] * t["price"] for t in buys)
        sell_amt = sum(t["qty"] * t["price"] for t in sells)
        fees = (buy_amt + sell_amt) * PARAMS["commission_rate"]

        current_price = dec.get("close_price") or dec.get("last_price") or holding["cost"]
        if not current_price or current_price <= 0:
            current_price = float(holding["cost"])

        net_qty = sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells)
        pnl = sell_amt - buy_amt + (net_qty * current_price) - fees
        total_net_pnl += pnl

        buy_signals = dec.get("buy_signals", [])
        buy_low_signals = dec.get("buy_low_signals", [])
        buy_add_signals = dec.get("buy_add_signals", [])
        sell_signals = dec.get("sell_signals", [])
        sell_high_signals = dec.get("sell_high_signals", [])
        panic_sell_signals = dec.get("panic_sell_signals", [])
        buy_count = len(buy_signals)
        sell_count = len(sell_signals)
        last_price = dec.get("last_price", current_price)
        last_vwap = dec.get("last_vwap", current_price)
        last_score = dec.get("last_score", 0)
        decision_bias = "偏买" if buy_count > sell_count else ("偏卖" if sell_count > buy_count else "均衡")
        close_gap = (current_price - last_vwap) / last_vwap if last_vwap else 0.0

        has_signal = buy_count > 0 or sell_count > 0
        has_trade = len(buys) > 0 or len(sells) > 0

        if has_signal or has_trade:
            lines.append(f"► {holding.get('name', code)}({code})")
            lines.append(f"   盘终敞口 {net_qty}股 | 做T净利润 {'+' if pnl>0 else ''}{pnl:.2f} 元 | 决策偏向 {decision_bias}")
            lines.append(f"   盘中信号 买{buy_count}次/卖{sell_count}次 | 分数 {last_score:.0f} | 现价 {current_price:.2f} | 偏离VWAP {close_gap*100:+.2f}%")
            lines.append(f"   指数联动 {dec.get('last_benchmark_name', '')} | 状态 {dec.get('last_benchmark_state', 'unknown')} | 门控 {dec.get('last_benchmark_gate', 'neutral')}")
            if buy_count or sell_count:
                last_buy = buy_signals[-1] if buy_count else None
                last_sell = sell_signals[-1] if sell_count else None
                if last_buy:
                    lines.append(f"   最近买点 {last_buy['time']} {last_buy['action']} 价{last_buy['price']:.2f} 分{last_buy['score']:.0f} | {_brief_reasons(last_buy['reasons'])}")
                if last_sell:
                    lines.append(f"   最近卖点 {last_sell['time']} {last_sell['action']} 价{last_sell['price']:.2f} 分{last_sell['score']:.0f} | {_brief_reasons(last_sell['reasons'])}")

            if buy_signals:
                buy_max_score = max(item["score"] for item in buy_signals)
                buy_best = max(buy_signals, key=lambda item: item["score"])
                buy_hit = sum(1 for item in buy_signals if item["price"] <= current_price)
                buy_success = buy_hit / buy_count if buy_count else 0.0
                lines.append(f"   买点轨迹 分{buy_max_score:.0f} | 有利 {buy_hit}/{buy_count} | 命中 {buy_success*100:.0f}% | 最佳 {buy_best['time']}@{buy_best['price']:.2f}")
            if sell_signals:
                sell_max_score = max(item["score"] for item in sell_signals)
                sell_best = max(sell_signals, key=lambda item: item["score"])
                sell_hit = sum(1 for item in sell_signals if item["price"] >= current_price)
                sell_success = sell_hit / sell_count if sell_count else 0.0
                lines.append(f"   卖点轨迹 分{sell_max_score:.0f} | 有利 {sell_hit}/{sell_count} | 命中 {sell_success*100:.0f}% | 最佳 {sell_best['time']}@{sell_best['price']:.2f}")

            if buy_signals and current_price > last_vwap:
                lines.append("   盘后初判 买点整体偏早，若后续回落则可进一步收紧")
                _bump(misjudge_counter["buy_early"], code)
                for item in buy_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["buy_early"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["early"], factor)
            if sell_signals and current_price < last_vwap * 0.992:
                lines.append("   盘后初判 卖点整体偏早，若后续继续下探且偏离明显，则可保留当前卖法")
                _bump(misjudge_counter["sell_early"], code)
                for item in sell_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["sell_early"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["early"], factor)
            if buy_signals and current_price < last_vwap:
                lines.append("   盘后初判 买点在收盘未占优，买点条件仍可再校准")
                _bump(misjudge_counter["missed_buy"], code)
                for item in buy_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["missed_buy"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["missed"], factor)
            if sell_signals and current_price > last_vwap * 1.002:
                lines.append("   盘后初判 卖点在收盘未占优，卖点条件仍可再校准")
                _bump(misjudge_counter["missed_sell"], code)
                for item in sell_signals:
                    for factor in item.get("reasons", "").split(" + "):
                        if factor:
                            _bump(misjudge_factor_counter["missed_sell"], factor)
                            if item.get("action") in action_misjudge_phase_factor_counter:
                                _bump(action_misjudge_phase_factor_counter[item.get("action")]["missed"], factor)

        review_bucket = etf_review if holding.get("type") == "etf" else stock_review
        review_bucket.append(f"{holding.get('name', code)}({code}) {buy_count}/{sell_count} | {current_price:.2f} | VWAP{last_vwap:.2f} | {close_gap*100:+.2f}%")

        m_status = dec.get("minute_status", "未拉取")
        if m_status not in ("ok", "cache_hit", "未拉取"):
            minute_issues.append(f"{holding.get('name', code)}({code})[{_minute_status_label(m_status, dec.get('minute_detail', ''))}] {dec.get('minute_detail', '')[:28]}")

    def _top_keys(counter: dict, n: int = 3):
        return sorted(counter.items(), key=lambda x: x[1], reverse=True)[:n]

    lines.append(f"💰 今日预估做T总净利润: {'+' if total_net_pnl>0 else ''}{total_net_pnl:.2f} 元\n")
    lines.append("═"*20 + " 【动作分类汇总】 " + "═"*20)
    lines.append(f"低吸 BUY_LOW={action_bucket_counts['buy_low']}次/{action_bucket_qty['buy_low']}股 | 加仓 ADD_POS={action_bucket_counts['buy_add']}次/{action_bucket_qty['buy_add']}股")
    lines.append(f"高抛 SELL_HIGH={action_bucket_counts['sell_high']}次/{action_bucket_qty['sell_high']}股 | 减仓 PANIC_SELL={action_bucket_counts['panic_sell']}次/{action_bucket_qty['panic_sell']}股")
    lines.append("═"*20 + " 【ETF复盘小结】 " + "═"*20)
    if etf_review:
        lines.append(f"ETF {len(etf_review)}票 | 有信号 {sum(1 for x in etf_review if not x.endswith(' 0/0 | 0.00 | +0.00%'))}")
        lines.extend(etf_review)
    else:
        lines.append("暂无ETF复盘数据")
    lines.append("═"*20 + " 【普通股复盘小结】 " + "═"*20)
    if stock_review:
        lines.append(f"普通股 {len(stock_review)}票 | 有信号 {sum(1 for x in stock_review if not x.endswith(' 0/0 | 0.00 | +0.00%'))}")
        lines.extend(stock_review)
    else:
        lines.append("暂无普通股复盘数据")
    lines.append("═"*20 + " 【误判因子排行】 " + "═"*20)
    for title, counter in [("买点偏早", misjudge_counter["buy_early"]), ("卖点偏早", misjudge_counter["sell_early"]), ("漏买", misjudge_counter["missed_buy"]), ("漏卖", misjudge_counter["missed_sell"])]:
        top_items = _top_keys(counter)
        if top_items:
            lines.append(f"{title}: " + ", ".join(f"{k}:{v}" for k, v in top_items))
        else:
            lines.append(f"{title}: 暂无")
    lines.append("═"*20 + " 【误判因子细分】 " + "═"*20)
    for title, counter in [("买点偏早因子", misjudge_factor_counter["buy_early"]), ("卖点偏早因子", misjudge_factor_counter["sell_early"]), ("漏买因子", misjudge_factor_counter["missed_buy"]), ("漏卖因子", misjudge_factor_counter["missed_sell"])]:
        top_items = _top_keys(counter)
        if top_items:
            lines.append(f"{title}: " + ", ".join(f"{k}:{v}" for k, v in top_items))
        else:
            lines.append(f"{title}: 暂无")
    lines.append("═"*20 + " 【四类动作误判因子】 " + "═"*20)
    for action, title in [("BUY_LOW", "低吸"), ("ADD_POS", "加仓"), ("SELL_HIGH", "高抛"), ("PANIC_SELL", "跳水")]:
        early_top = _top_keys(action_misjudge_phase_factor_counter[action]["early"], 3)
        missed_top = _top_keys(action_misjudge_phase_factor_counter[action]["missed"], 3)
        if early_top:
            lines.append(f"{title} {action} 偏早: " + ", ".join(f"{k}:{v}" for k, v in early_top))
        else:
            lines.append(f"{title} {action} 偏早: 暂无")
        if missed_top:
            lines.append(f"{title} {action} 未占优: " + ", ".join(f"{k}:{v}" for k, v in missed_top))
        else:
            lines.append(f"{title} {action} 未占优: 暂无")
    if any(minute_issue_stats.values()):
        lines.append("═"*20 + " 【分钟线异常摘要】 " + "═"*20)
        short_minute_lines = []
        for bucket in ["网络", "解析", "接口", "缓存", "其他"]:
            items = minute_issue_stats.get(bucket, {})
            total = sum(items.values()) if isinstance(items, dict) else 0
            if total <= 0:
                continue
            top_items = sorted(items.items(), key=lambda kv: kv[1], reverse=True)[:3]
            short_minute_lines.append(f"{bucket}类 {total}项 | " + ", ".join(f"{k}:{v}" for k, v in top_items))
        lines.extend(short_minute_lines)
    minute_issue_total = sum(sum(items.values()) for items in minute_issue_stats.values() if isinstance(items, dict))
    if minute_issue_total:
        lines.insert(1, f"分钟线异常总计 {minute_issue_total} 项 | 网络 {sum(minute_issue_stats.get('网络', {}).values())} | 解析 {sum(minute_issue_stats.get('解析', {}).values())} | 接口 {sum(minute_issue_stats.get('接口', {}).values())}")
    lines.append("═"*20 + " 【策略最高分复盘数据(喂给AI)】 " + "═"*20)
    for code, holding in HOLDINGS.items():
        stats = AI_REVIEW_STATS.get(code, {})
        dec = DAILY_DECISION_STATS.get(code, {})
        name = stats.get("名称", holding.get("name", code))
        max_buy = stats.get("最大多头分", 0)
        max_sell = stats.get("最大空头分", 0)
        max_amp = stats.get("最大振幅", 0)
        buy_trig = stats.get("触发买入次数", 0)
        sell_trig = stats.get("触发卖出次数", 0)
        close_price = dec.get("close_price", dec.get("last_price", holding["cost"]))
        last_vwap = dec.get("last_vwap", close_price)
        vwap_dev = (close_price - last_vwap) / last_vwap * 100 if last_vwap else 0.0
        buy_qty = int(stats.get("触发买入股数", 0) or 0)
        sell_qty = int(stats.get("触发卖出股数", 0) or 0)
        lines.append(f"{name}({code}): 多={max_buy:.0f} 空={max_sell:.0f} 振={max_amp*100:.2f}% 偏={vwap_dev:+.2f}% 买={buy_trig}/{buy_qty} 卖={sell_trig}/{sell_qty}")
    lines.append("═"*20 + " 【复盘阅读顺序】 " + "═"*20)
    lines.append(f"1. sys日志 -> 2. data_quality -> 3. decision_trace")
    lines.append(f"4. shadow_signals -> 5. signal_outcome -> 6. ai_review")
    lines.append("═"*20 + " 【信号结果追踪汇总】 " + "═"*20)
    outcome_counts = {"correct": 0, "buy_early": 0, "sell_early": 0, "buy_validating": 0, "sell_validating": 0, "hold_pending": 0}
    side_counts = {"BUY_LOW": 0, "ADD_POS": 0, "SELL_HIGH": 0, "PANIC_SELL": 0}
    win_counts = {"win_5m": 0, "win_15m": 0}
    mat_counts = {"maturity_5m": 0, "maturity_15m": 0}
    action_maturity = {"BUY_LOW": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}, "ADD_POS": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}, "SELL_HIGH": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}, "PANIC_SELL": {"win_5m": 0, "win_15m": 0, "maturity_5m": 0, "maturity_15m": 0}}
    action_early = {"BUY_LOW": 0, "ADD_POS": 0, "SELL_HIGH": 0, "PANIC_SELL": 0}
    action_validating = {"BUY_LOW": 0, "ADD_POS": 0, "SELL_HIGH": 0, "PANIC_SELL": 0}
    trace_files = []
    try:
        trace_files = [f for f in os.listdir(TRACE_DIR) if f.startswith("signal_outcome_") and f.endswith(".jsonl")]
    except Exception:
        trace_files = []
    for fname in trace_files:
        try:
            with open(os.path.join(TRACE_DIR, fname), "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    cls = rec.get("final_classification", "correct")
                    outcome_counts[cls] = outcome_counts.get(cls, 0) + 1
                    action = rec.get("action", "")
                    if action in side_counts:
                        side_counts[action] += 1
                    if cls in {"buy_early", "buy_validating"} and action in action_early:
                        action_early[action] += 1
                    if cls in {"sell_early", "sell_validating"} and action in action_validating:
                        action_validating[action] += 1
                    if rec.get("maturity_5m"):
                        mat_counts["maturity_5m"] += 1
                        if rec.get("win_5m"):
                            win_counts["win_5m"] += 1
                        if action in action_maturity:
                            action_maturity[action]["maturity_5m"] += 1
                            if rec.get("win_5m"):
                                action_maturity[action]["win_5m"] += 1
                    if rec.get("maturity_15m"):
                        mat_counts["maturity_15m"] += 1
                        if rec.get("win_15m"):
                            win_counts["win_15m"] += 1
                        if action in action_maturity:
                            action_maturity[action]["maturity_15m"] += 1
                            if rec.get("win_15m"):
                                action_maturity[action]["win_15m"] += 1
        except Exception:
            continue
    win_5m_rate = (win_counts["win_5m"] / mat_counts["maturity_5m"] * 100) if mat_counts["maturity_5m"] else 0.0
    win_15m_rate = (win_counts["win_15m"] / mat_counts["maturity_15m"] * 100) if mat_counts["maturity_15m"] else 0.0
    buy_early_cnt = outcome_counts.get("buy_early", 0)
    sell_early_cnt = outcome_counts.get("sell_early", 0)
    lines.append(f"正确={outcome_counts.get('correct', 0)} | 买早={buy_early_cnt} | 卖早={sell_early_cnt} | 买验证中={outcome_counts.get('buy_validating', 0)} | 卖验证中={outcome_counts.get('sell_validating', 0)} | 待定={outcome_counts.get('hold_pending', 0)}")
    lines.append(f"动作分布 BUY_LOW={side_counts.get('BUY_LOW', 0)} | ADD_POS={side_counts.get('ADD_POS', 0)} | SELL_HIGH={side_counts.get('SELL_HIGH', 0)} | PANIC_SELL={side_counts.get('PANIC_SELL', 0)}")
    lines.append(f"5分钟胜率={win_5m_rate:.1f}% ({win_counts['win_5m']}/{mat_counts['maturity_5m']}) | 15分钟胜率={win_15m_rate:.1f}% ({win_counts['win_15m']}/{mat_counts['maturity_15m']})")
    for action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
        total_action = side_counts.get(action, 0)
        if total_action or action_maturity[action]["maturity_5m"] or action_maturity[action]["maturity_15m"]:
            a5 = (action_maturity[action]["win_5m"] / action_maturity[action]["maturity_5m"] * 100) if action_maturity[action]["maturity_5m"] else 0.0
            a15 = (action_maturity[action]["win_15m"] / action_maturity[action]["maturity_15m"] * 100) if action_maturity[action]["maturity_15m"] else 0.0
            early_rate = (action_early[action] / total_action * 100) if total_action else 0.0
            validating_rate = (action_validating[action] / total_action * 100) if total_action else 0.0
            lines.append(f"{action}: 偏早={action_early[action]} | 验证中={action_validating[action]} | 偏早率={early_rate:.1f}% | 验证率={validating_rate:.1f}% | 5分钟胜率={a5:.1f}% | 15分钟胜率={a15:.1f}%")
    best_outcome = max(outcome_counts.items(), key=lambda x: x[1]) if outcome_counts else ("correct", 0)
    if sell_early_cnt >= buy_early_cnt + 2 and sell_early_cnt > 0:
        worst_outcome = (sell_early_cnt, "卖早")
    elif buy_early_cnt >= sell_early_cnt + 2 and buy_early_cnt > 0:
        worst_outcome = (buy_early_cnt, "买早")
    else:
        worst_outcome = (0, "无明显偏差")
    shadow_count = 0
    try:
        for fname in trace_files:
            with open(os.path.join(TRACE_DIR, fname), "r", encoding="utf-8") as f:
                for _ in f:
                    shadow_count += 1
    except Exception:
        pass
    lines.append(f"一眼摘要：最高频结果={best_outcome[0]}({best_outcome[1]}) | 主要风险={worst_outcome[1]}({worst_outcome[0]}) | 影子机会={shadow_count}")
    lines.append("═"*60)

    summary_text = "\n".join(lines)
    with open(os.path.join(LOG_DIR, f"ai_review_{today}.log"), "w", encoding="utf-8") as f: f.write(summary_text)
    
    # V1.11: 记录EOD复盘日志（用于后续分析最优做T时机）
    if _log_enhancer:
        for code, holding in HOLDINGS.items():
            dec = DAILY_DECISION_STATS.get(code, {})
            stats = AI_REVIEW_STATS.get(code, {})
            buy_signals = dec.get("buy_signals", [])
            sell_signals = dec.get("sell_signals", [])
            close_price = dec.get("close_price", dec.get("last_price", holding["cost"]))
            last_vwap = dec.get("last_vwap", close_price)
            day_ret = dec.get("day_ret", 0.0)
            _log_enhancer.log_eod_review(
                code=code, name=holding.get("name", code),
                high_price=stats.get("最高价格", close_price), low_price=stats.get("最低价格", close_price),
                close_price=close_price, vwap=last_vwap, day_ret=day_ret,
                best_sell_time=sell_signals[-1]["time"] if sell_signals else None,
                best_sell_price=sell_signals[-1]["price"] if sell_signals else None,
                best_buy_time=buy_signals[-1]["time"] if buy_signals else None,
                best_buy_price=buy_signals[-1]["price"] if buy_signals else None,
                signals_triggered=buy_signals + sell_signals,
                profit_potential=stats.get("最大振幅", 0.0)
            )

    learning_summary = {"buy_low": [], "buy_add": [], "sell_high": [], "panic_sell": [], "insufficient": []}
    starvation_state = load_starvation_state()
    starvation_updates: Dict[str, dict] = {}
    for code, holding in HOLDINGS.items():
        stats = AI_REVIEW_STATS.get(code, {})
        dec = DAILY_DECISION_STATS.get(code, {})
        buy_trig = int(stats.get("触发买入次数", 0))
        sell_trig = int(stats.get("触发卖出次数", 0))
        if buy_trig + sell_trig < 2:
            learning_summary["insufficient"].append(f"{holding.get('name', code)}({code}) 样本不足")
            continue
        if buy_trig == 0 and sell_trig > 0:
            record = starvation_state.get(code, {})
            days = int(record.get("days", 0)) + 1
            starvation_updates[code] = {
                "days": days,
                "last_date": today,
                "relax_until": record.get("relax_until", "")
            }
        else:
            if code in starvation_state:
                starvation_updates[code] = {"days": 0, "last_date": today, "relax_until": ""}
        buy_signals = dec.get("buy_signals", [])
        buy_low_signals = dec.get("buy_low_signals", [])
        buy_add_signals = dec.get("buy_add_signals", [])
        sell_signals = dec.get("sell_signals", [])
        sell_high_signals = dec.get("sell_high_signals", [])
        panic_sell_signals = dec.get("panic_sell_signals", [])
        close_price = float(dec.get("close_price", 0) or 0)
        last_vwap = float(dec.get("last_vwap", close_price) or close_price)
        default_qty = _default_trade_qty(holding)
        buy_qty = _sum_signal_qty(buy_signals, default_qty)
        buy_low_qty = _sum_signal_qty(buy_low_signals, default_qty)
        buy_add_qty = _sum_signal_qty(buy_add_signals, default_qty)
        sell_qty = _sum_signal_qty(sell_signals, default_qty)
        sell_high_qty = _sum_signal_qty(sell_high_signals, default_qty)
        panic_sell_qty = _sum_signal_qty(panic_sell_signals, default_qty)
        buy_weight = _qty_weight(buy_qty, default_qty) if buy_signals else 0.0
        sell_weight = _qty_weight(sell_qty, default_qty) if sell_signals else 0.0
        buy_early_w = buy_weight if buy_signals and close_price > last_vwap else 0.0
        sell_early_w = sell_weight if sell_signals and close_price < last_vwap else 0.0
        missed_buy_w = buy_weight if buy_signals and close_price < last_vwap else 0.0
        missed_sell_w = sell_weight if sell_signals and close_price > last_vwap else 0.0
        buy_adj = _clamp(round((sell_early_w + missed_buy_w) - (buy_early_w + missed_sell_w)), -2, 2)
        sell_adj = _clamp(round((buy_early_w + missed_buy_w) - (sell_early_w + missed_sell_w)), -3, 3)
        buy_low_adj = _clamp(round((missed_buy_w * 2) - buy_early_w), -3, 3)
        buy_factor_adj = 0
        sell_factor_adj = 0
        buy_seconds_adj = 0
        sell_seconds_adj = 0
        if buy_early_w and buy_trig:
            buy_factor_adj += min(2, max(1, round(buy_early_w)))
            buy_seconds_adj += int(30 * buy_early_w)
        if missed_buy_w and buy_trig:
            buy_factor_adj += min(2, max(1, round(missed_buy_w)))
            buy_seconds_adj += int(15 * missed_buy_w)
        if sell_early_w and sell_trig:
            sell_factor_adj += min(2, max(1, round(sell_early_w)))
            sell_seconds_adj += int(30 * sell_early_w)
        if missed_sell_w and sell_trig:
            sell_factor_adj += min(2, max(1, round(missed_sell_w)))
            sell_seconds_adj += int(30 * missed_sell_w)
        base_memory = _strategy_memory_for_code(code)
        new_memory = {
            "buy_threshold_adj": _clamp(int(base_memory.get("buy_threshold_adj", 0)) + buy_adj, -3, 3),
            "sell_threshold_adj": _clamp(int(base_memory.get("sell_threshold_adj", 0)) + sell_adj, -3, 3),
            "buy_low_threshold_adj": _clamp(int(base_memory.get("buy_low_threshold_adj", 0)) + buy_low_adj, -3, 3),
            "buy_confirm_min_score": _clamp(int(base_memory.get("buy_confirm_min_score", PARAMS["buy_confirm_min_score"])) + buy_adj, 42, 58),
            "buy_confirm_min_factors": _clamp(int(base_memory.get("buy_confirm_min_factors", PARAMS["buy_confirm_min_factors"])) + buy_factor_adj, 3, 7),
            "buy_confirm_min_seconds": _clamp(int(base_memory.get("buy_confirm_min_seconds", PARAMS["buy_confirm_min_seconds"])) + buy_seconds_adj, 0, 180),
            "buy_rebound_min_score_gap": _clamp(int(base_memory.get("buy_rebound_min_score_gap", PARAMS["buy_rebound_min_score_gap"])) + max(0, buy_factor_adj - 1), 6, 14),
            "sell_confirm_min_factors": _clamp(int(base_memory.get("sell_confirm_min_factors", PARAMS["sell_confirm_min_factors"])) + sell_factor_adj, 5, 8),
            "sell_confirm_min_seconds": _clamp(int(base_memory.get("sell_confirm_min_seconds", PARAMS["sell_confirm_min_seconds"])) + sell_seconds_adj, 45, 180),
            "sell_needs_momentum": True,
            "sell_needs_ema": True,
            "sell_needs_volume": True,
            "buy_needs_momentum": True,
            "buy_needs_ema": True,
            "buy_needs_volume": True,
            "buy_min_time": base_memory.get("buy_min_time", "09:40")
        }
        _merge_memory(code, new_memory)
        if buy_adj != 0:
            if buy_low_signals:
                learning_summary["buy_low"].append(f"{holding.get('name', code)}({code}) 低吸调{buy_adj:+.0f} | 权重{buy_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{buy_low_qty} | 偏早权重{buy_early_w:.2f} | 未占优权重{missed_buy_w:.2f}")
            if buy_add_signals:
                learning_summary["buy_add"].append(f"{holding.get('name', code)}({code}) 加仓调{buy_adj:+.0f} | 权重{buy_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{buy_add_qty} | 偏早权重{buy_early_w:.2f} | 未占优权重{missed_buy_w:.2f}")
        if sell_adj != 0:
            if sell_high_signals:
                learning_summary["sell_high"].append(f"{holding.get('name', code)}({code}) 高抛收紧{sell_adj:+.0f} | 权重{sell_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{sell_high_qty} | 偏早权重{sell_early_w:.2f} | 未占优权重{missed_sell_w:.2f}")
            if panic_sell_signals:
                learning_summary["panic_sell"].append(f"{holding.get('name', code)}({code}) 跳水收紧{sell_adj:+.0f} | 权重{sell_weight:.2f} | 样本{buy_trig + sell_trig} | 股数{panic_sell_qty} | 偏早权重{sell_early_w:.2f} | 未占优权重{missed_sell_w:.2f}")
    if learning_summary["buy_low"]:
        lines.append("═"*20 + " 【学习摘要-低吸修正】 " + "═"*20)
        lines.extend(learning_summary["buy_low"][:8])
    if learning_summary["buy_add"]:
        lines.append("═"*20 + " 【学习摘要-加仓修正】 " + "═"*20)
        lines.extend(learning_summary["buy_add"][:8])
    if learning_summary["sell_high"]:
        lines.append("═"*20 + " 【学习摘要-高抛修正】 " + "═"*20)
        lines.extend(learning_summary["sell_high"][:8])
    if learning_summary["panic_sell"]:
        lines.append("═"*20 + " 【学习摘要-跳水修正】 " + "═"*20)
        lines.extend(learning_summary["panic_sell"][:8])
    if learning_summary["insufficient"]:
        lines.append("═"*20 + " 【学习摘要-样本不足】 " + "═"*20)
        lines.extend(learning_summary["insufficient"][:8])
    if starvation_updates:
        for code, rec in starvation_updates.items():
            if rec.get("days", 0) >= PARAMS["buy_starvation_days"]:
                relax_until = (datetime.now() + timedelta(days=PARAMS["buy_starvation_relax_ttl_days"])).strftime("%Y-%m-%d")
                starvation_updates[code] = {"days": rec["days"], "last_date": today, "relax_until": relax_until}
                lines.append(f"饥饿保护 {HOLDINGS.get(code, {}).get('name', code)}({code}) 连续{rec['days']}日无买入，次日放松确认")
        save_starvation_state({**starvation_state, **starvation_updates})
    log.info(summary_text)
    if FEISHU_WEBHOOK:
        try:
            def _extract_block(start_markers: List[str], stop_markers: List[str], max_lines: int = 8) -> str:
                start_idx = None
                for marker in start_markers:
                    for idx, item in enumerate(lines):
                        if marker in item:
                            start_idx = idx + 1
                            break
                    if start_idx is not None:
                        break
                if start_idx is None:
                    return "暂无"
                stop_idx = len(lines)
                for marker in stop_markers:
                    for idx in range(start_idx, len(lines)):
                        if marker in lines[idx]:
                            stop_idx = min(stop_idx, idx)
                            break
                block = [item for item in lines[start_idx:stop_idx] if item.strip()]
                return "\n".join(block[:max_lines]) if block else "暂无"

            learning_sources = [
                ("低吸", learning_summary["buy_low"]),
                ("加仓", learning_summary["buy_add"]),
                ("高抛", learning_summary["sell_high"]),
                ("跳水", learning_summary["panic_sell"]),
                ("样本", learning_summary["insufficient"]),
            ]
            learning_lines = []
            for title, items in learning_sources:
                if not items:
                    learning_lines.append(f"{title}：暂无")
                    continue
                top_items = items[:3]
                learning_lines.append(f"{title} | {len(items)}项 | " + " ; ".join(top_items))
            learning_text = "\n".join(learning_lines)
            def _section(title: str, body: str) -> List[dict]:
                return [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{title}**\n{body.strip()}"
                        },
                    },
                    {"tag": "hr"},
                ]

            card_elements = [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**日报汇总 {today}**\n盘后摘要"
                    },
                },
                {"tag": "hr"},
            ]
            card_elements.extend(_section("日报·尾盘复盘", overview_text))
            card_elements.extend(_section("日报·分钟线异常", minute_text))
            card_elements.extend(_section("日报·学习摘要", learning_text))
            if card_elements and card_elements[-1].get("tag") == "hr":
                card_elements.pop()

            summary_payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "elements": card_elements,
                },
                "notify_type": 1,
            }
            send_feishu_payload(
                payload=summary_payload,
                success_log="✅ 尾盘复盘已推送飞书",
                error_prefix="尾盘复盘飞书推送",
                trigger_urgent_alarm_after_success=False,
            )
        except Exception as e:
            log.warning(f"⚠️  尾盘复盘飞书推送失败: {str(e)[:120]}")
    _eod_logged_date = today

# ==================== 交易主循环 ====================
_last_idle_log = datetime.min
_scan_count = 0

def build_preopen_context() -> PreOpenContext:
    holdings = load_holdings()
    watchlist = load_watchlist()
    engine = PreOpenEngine(holdings, watchlist)
    context = engine.evaluate()
    engine.persist(context)
    return context


def _preopen_action_label(context: PreOpenContext) -> str:
    if context.market_bias in {"risk_on", "neutral_to_positive"} and context.market_score >= 58:
        return "进攻"
    if context.market_bias == "risk_off" or context.market_score <= 45:
        return "回避"
    return "观察"


def _preopen_card_template(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    if action == "进攻" or context.market_bias == "risk_on" or context.market_score >= 58:
        return "green"
    if action == "回避" or context.market_bias == "risk_off" or context.market_score <= 45:
        return "red"
    return "blue"


def _feishu_card_header(title: str, template: str) -> dict:
    return {"template": template, "title": {"tag": "plain_text", "content": title}}


def _is_preopen_monitor_window(now: datetime) -> bool:
    return now.weekday() < 5 and dtime(9, 15) <= now.time() < dtime(9, 25)


def _preopen_monitor_signature(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    score_bucket = int(max(0.0, float(context.market_score or 0)) // 5) * 5
    hot_theme = _preopen_hot_theme_text(context, 3)
    active = ",".join(_sort_codes_by_holding_priority(context.active_codes)[:3])
    watch = ",".join(_sort_codes_by_holding_priority(context.watch_codes)[:3])
    focus = ",".join(_sort_codes_by_holding_priority(context.focus_codes)[:3])
    blocked = ",".join(_sort_codes_by_holding_priority(context.blocked_codes)[:3])
    adv = _preopen_adv_counts(context)
    up = int(adv.get("up", 0) or 0)
    down = int(adv.get("down", 0) or 0)
    flat = int(adv.get("flat", 0) or 0)
    return "|".join([
        _preopen_action_label(context),
        context.market_bias or "unknown",
        str(score_bucket),
        f"{up}/{down}/{flat}",
        hot_theme,
        active,
        watch,
        focus,
        blocked,
    ])


def _reset_preopen_monitor_state_if_needed(today: str) -> None:
    global _preopen_monitor_date, _preopen_monitor_last_push_at, _preopen_monitor_last_signature, _preopen_monitor_push_count
    if _preopen_monitor_date == today:
        return
    _preopen_monitor_date = today
    _preopen_monitor_last_push_at = None
    _preopen_monitor_last_signature = None
    _preopen_monitor_push_count = 0


def _should_push_preopen_monitor(context: PreOpenContext, now: datetime) -> bool:
    global _preopen_monitor_last_push_at, _preopen_monitor_last_signature, _preopen_monitor_push_count
    if not FEISHU_WEBHOOK or not _is_preopen_monitor_window(now):
        return False
    today = now.strftime("%Y-%m-%d")
    _reset_preopen_monitor_state_if_needed(today)
    if _preopen_monitor_push_count >= 5:
        return False
    signature = _preopen_monitor_signature(context)
    if _preopen_overview_last_push_at is not None and _preopen_monitor_push_count == 0:
        if (now - _preopen_overview_last_push_at).total_seconds() < 60:
            return False
    if _preopen_monitor_last_push_at is None:
        return True
    elapsed = (now - _preopen_monitor_last_push_at).total_seconds()
    if signature != _preopen_monitor_last_signature and elapsed >= 60:
        return True
    if elapsed >= 120:
        return True
    return False


def _format_code_names(codes: List[str], limit: int = 4) -> str:
    names = []
    for code in codes[:limit]:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        names.append(f"{holding.get('name', code)}({code})")
    return "、".join(names) if names else "暂无"


def _preopen_strategy_line(context: PreOpenContext) -> str:
    return (
        f"策略结论：主做{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 2)} | "
        f"观察{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 2)} | "
        f"关注{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 2)} | "
        f"回避{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 2)}"
    )


def _rank_focus_codes(codes: List[str]) -> List[str]:
    scored = []
    for code in codes:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        qty = int(holding.get("qty", 0) or 0)
        cost = float(holding.get("cost", 0) or 0)
        score = qty + (cost * 0.1)
        scored.append((score, code))
    scored.sort(reverse=True)
    return [code for _, code in scored]


def _preopen_focus_text(context: PreOpenContext) -> str:
    ranked = _rank_focus_codes(context.watch_codes or context.focus_codes)
    return _format_code_names(ranked, 4)


def _sort_codes_by_holding_priority(codes: List[str]) -> List[str]:
    scored = []
    for code in codes:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        qty = int(holding.get("qty", 0) or 0)
        cost = float(holding.get("cost", 0) or 0)
        score = qty * 10 + cost
        scored.append((score, code))
    scored.sort(reverse=True)
    return [code for _, code in scored]


def _preopen_followup_text(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    if action == "进攻":
        return "开盘后 5 分钟看强主题承接，15 分钟看是否能站稳分时均线。"
    if action == "观察":
        return "开盘后先看分化，优先等 5~15 分钟确认强弱，再决定是否进。"
    return "开盘后只看核心标的承接，不追高，等量能和情绪同时转强再说。"


def _preopen_turn_strong_rule(context: PreOpenContext) -> str:
    if context.market_score < 50:
        return "若 9:35 后上涨家数回到高于下跌家数，且市场评分回到 55 上方，再把关注升为观察。"
    if context.market_score < 58:
        return "若 9:35 后主题集中度继续抬升，且重点标的出现放量承接，再把观察升为主做。"
    return "若开盘后 5 分钟强主题延续、量能不掉，再优先跟主做组。"


def _special_low_buy_plan(code: str) -> str:
    if code == "688102":
        return "斯瑞新材：优先回踩 VWAP/短均线后低吸，等止跌不再创新低再上。"
    if code == "601698":
        return "中国卫通：必须先止跌确认再低吸，确认站稳 VWAP 后再考虑介入。"
    return ""


def _special_low_buy_stage_rule(code: str, stage: str) -> str:
    stage = str(stage or "").strip()
    if code == "688102":
        if stage == "open":
            return "开盘看能否守住 VWAP 附近，不追第一波拉升。"
        if stage == "intraday":
            return "盘中等回踩不破、分时不创新低后再低吸。"
        if stage == "eod":
            return "尾盘若仍在 VWAP 附近反复且不破位，可小仓观察。"
    if code == "601698":
        if stage == "open":
            return "开盘先看是否止跌，不抢反弹。"
        if stage == "intraday":
            return "盘中必须重新站稳 VWAP 附近，再考虑低吸。"
        if stage == "eod":
            return "尾盘只有确认止跌并靠近均价时才考虑。"
    return ""


def _special_loss_reduction_rule(code: str) -> str:
    if code == "300364":
        return "中文在线：优先等反弹减亏，不追弱反弹；只有重新站稳 VWAP 且分时转强才允许少量加仓。"
    if code == "002639":
        return "雪人集团：优先利用反弹减亏，弱势不补仓；只有放量站回 VWAP 并确认止跌后才允许低吸。"
    return ""


def _special_loss_reduction_stage_rule(code: str, stage: str) -> str:
    stage = str(stage or "").strip()
    if code == "300364":
        if stage == "open":
            return "开盘先看是否高开回落，优先等反弹减亏，不追开盘脉冲。"
        if stage == "intraday":
            return "盘中只在重新站稳 VWAP、分时转强时才考虑减亏或小补。"
        if stage == "eod":
            return "尾盘若仍弱于 VWAP，优先保留减亏思路，不做被动摊平。"
    if code == "002639":
        if stage == "open":
            return "开盘先看承接，弱势不抢反弹，先等减亏窗口。"
        if stage == "intraday":
            return "盘中只有放量站回 VWAP 且止跌确认，才允许小仓修复。"
        if stage == "eod":
            return "尾盘若未收复均价，优先减亏思路，避免继续扩大浮亏。"
    return ""


def _special_loss_threshold_adjustments(code: str, action: str, buy_threshold: int, sell_threshold: int, buy_score: float, sell_score: float, price: float, vwap: float, is_strong_pullback: bool) -> tuple[int, int, float, float]:
    if code == "300364":
        if action in {"BUY_LOW", "ADD_POS"}:
            buy_threshold += 4
            if not is_strong_pullback:
                buy_threshold += 2
            buy_score -= 2
        if action in {"SELL_HIGH", "PANIC_SELL"} or (vwap and price > vwap * 1.002):
            sell_threshold = max(35, sell_threshold - 2)
            sell_score += 3
    elif code == "002639":
        if action in {"BUY_LOW", "ADD_POS"}:
            buy_threshold += 5
            if not is_strong_pullback:
                buy_threshold += 2
            buy_score -= 3
        if action in {"SELL_HIGH", "PANIC_SELL"} or (vwap and price > vwap * 1.0015):
            sell_threshold = max(35, sell_threshold - 3)
            sell_score += 4
    return buy_threshold, sell_threshold, buy_score, sell_score


def _format_preopen_brief(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    market_snapshot = context.market_snapshot if isinstance(context.market_snapshot, dict) else {}
    hot_theme = "、".join(market_snapshot.get("hot_theme", [])[:3]) or breadth.get("hot_theme_text", "") or "暂无"
    auction_summary = breadth.get("auction_summary", {}) if isinstance(breadth, dict) else {}
    auction_text = ""
    if isinstance(auction_summary, dict) and auction_summary:
        auction_text = (
            f"竞价：强{auction_summary.get('strong_open_count', 0)} / 弱{auction_summary.get('weak_open_count', 0)} / 缺{auction_summary.get('missing_open_count', 0)} | "
            f"均分 {float(auction_summary.get('mean_score', 0) or 0):.1f}"
        )
    return (
        f"早盘集合竞价结论：{_preopen_action_label(context)}\n"
        f"市场评分：{context.market_score:.1f} / 100 | 偏向：{context.market_bias} | 风险：{breadth.get('risk_flag', 'unknown')}\n"
        f"{auction_text + chr(10) if auction_text else ''}"
        f"{_preopen_strategy_line(context)}\n"
        f"1. 主做：{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 3)}\n"
        f"2. 观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 3)}\n"
        f"3. 关注：{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 3)}\n"
        f"4. 回避：{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 3)}\n"
        f"执行顺序：先1后2，再3，最后4\n"
        f"升级条件：{_preopen_turn_strong_rule(context)}\n"
        f"开盘后跟踪：{_preopen_followup_text(context)}\n"
        f"热门关注：{hot_theme}"
    )


def _record_preopen_trace(context: PreOpenContext) -> None:
    try:
        _append_jsonl(_trace_path("preopen_trace"), context.__dict__)
    except Exception:
        pass



def _feishu_md_div(content: str) -> dict:
    return {"tag": "div", "text": {"content": content, "tag": "lark_md"}}



def _feishu_hr() -> dict:
    return {"tag": "hr"}



def _preopen_safe_breadth(context: PreOpenContext) -> Dict[str, Any]:
    return context.breadth if isinstance(context.breadth, dict) else {}



def _preopen_adv_counts(context: PreOpenContext) -> Dict[str, int]:
    adv = _preopen_safe_breadth(context).get("advance_decline", {})
    if not isinstance(adv, dict):
        return {"up": 0, "down": 0, "flat": 0}
    return {
        "up": int(adv.get("up", 0) or 0),
        "down": int(adv.get("down", 0) or 0),
        "flat": int(adv.get("flat", 0) or 0),
    }



def _preopen_adv_text(context: PreOpenContext) -> str:
    adv = _preopen_adv_counts(context)
    return f"涨{adv['up']} / 跌{adv['down']} / 平{adv['flat']}"



def _preopen_hot_theme_text(context: PreOpenContext, limit: int = 3) -> str:
    snapshot_hot = context.market_snapshot.get("hot_theme", []) if isinstance(context.market_snapshot, dict) else []
    if isinstance(snapshot_hot, list) and snapshot_hot:
        return "、".join([str(x) for x in snapshot_hot[:limit] if str(x).strip()]) or "暂无"
    breadth_hot = _preopen_safe_breadth(context).get("hot_theme_text", "")
    if breadth_hot:
        return str(breadth_hot)
    return "暂无"



def _preopen_action_hint(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    return {
        "进攻": "优先看强主题中的回踩确认，不追弱票。",
        "观察": "只盯重点标的，等开盘后强弱分化再动。",
        "回避": "优先控仓，弱市不追价，只看核心标的是否有承接。",
    }.get(action, "按信号择机")



def _preopen_group_line(label: str, codes: List[str], limit: int = 4) -> str:
    return f"**{label}**：{_format_code_names(_sort_codes_by_holding_priority(codes), limit)}"



def _preopen_theme_lines(context: PreOpenContext, limit: int = 5, code_limit: int = 4) -> List[str]:
    lines = []
    for item in context.theme_rank[:limit]:
        sector = item.get("sector", "")
        count = item.get("count", 0)
        codes = item.get("codes", [])[:code_limit]
        names = []
        for code in codes:
            h = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
            names.append(f"{h.get('name', code)}({code})")
        lines.append(f"- {sector}：{count} 只 | {'、'.join(names) if names else '暂无'}")
    if not lines:
        lines.append("- 暂无主题聚合数据")
    return lines



def _build_preopen_card_payload(title: str, elements: List[dict], at_all: bool, at_text: str, template: Optional[str] = None) -> dict:
    card_elements = []
    if at_all and at_text:
        card_elements.append(_feishu_md_div(at_text))
    card_elements.append(_feishu_md_div(title))
    card_elements.extend(elements)
    card = {"config": {"wide_screen_mode": True}, "elements": card_elements}
    if template:
        card["header"] = _feishu_card_header(title, template)
    return {
        "msg_type": "interactive",
        "card": card,
        "notify_type": 1,
    }



def _build_preopen_summary_elements(context: PreOpenContext) -> List[dict]:
    breadth = _preopen_safe_breadth(context)
    elements = [
        _feishu_md_div(
            f"**集合竞价总览**\n"
            f"动作建议：{_preopen_action_label(context)} | 评分 {context.market_score:.1f} | 偏向 {context.market_bias} | 风险 {breadth.get('risk_flag', 'unknown')}\n"
            f"盘面判断：{context.session_note}\n"
            f"竞价摘要：强{breadth.get('auction_summary', {}).get('strong_open_count', 0)} / 弱{breadth.get('auction_summary', {}).get('weak_open_count', 0)} / 缺{breadth.get('auction_summary', {}).get('missing_open_count', 0)}\n"
            f"开盘：{_special_low_buy_stage_rule('688102', 'open')} | {_special_low_buy_stage_rule('601698', 'open')}\n"
            f"盘中：{_special_low_buy_stage_rule('688102', 'intraday')} | {_special_low_buy_stage_rule('601698', 'intraday')}\n"
            f"尾盘：{_special_low_buy_stage_rule('688102', 'eod')} | {_special_low_buy_stage_rule('601698', 'eod')}\n"
            f"数量：按剩余资金缩放，优先小仓试错"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**看板分组**\n"
            f"{_preopen_group_line('主做', context.active_codes, 2)}\n"
            f"{_preopen_group_line('观察', context.watch_codes, 2)}\n"
            f"{_preopen_group_line('关注', context.focus_codes, 2)}\n"
            f"{_preopen_group_line('回避', context.blocked_codes, 2)}\n"
            f"**快照** 涨跌 {_preopen_adv_text(context)} | 热点 {_preopen_hot_theme_text(context)} | 集中度 {float(breadth.get('top_theme_share', 0) or 0):.2%}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**开盘执行**\n"
            f"先1后2，再3，最后4\n"
            f"跟踪：{_preopen_followup_text(context)}\n"
            f"转强：{_preopen_turn_strong_rule(context)}"
        ),
    ]
    return elements



def _build_preopen_detail_elements(context: PreOpenContext) -> List[dict]:
    breadth = _preopen_safe_breadth(context)
    elements = [
        _feishu_md_div(
            f"**集合竞价详细结果**\n"
            f"动作建议：{_preopen_action_label(context)} | {_preopen_action_hint(context)}\n"
            f"时间：{context.generated_at} | 数据源：{context.source}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**市场状态**\n"
            f"评分 {context.market_score:.1f} | 偏向 {context.market_bias} | 风险 {breadth.get('risk_flag', 'unknown')} | 开盘 {'是' if breadth.get('market_open', False) else '否'}\n"
            f"盘面：{context.session_note}\n"
            f"快照：{_preopen_adv_text(context)} | 热点：{_preopen_hot_theme_text(context)}\n"
            f"竞价：强{breadth.get('auction_summary', {}).get('strong_open_count', 0)} / 弱{breadth.get('auction_summary', {}).get('weak_open_count', 0)} / 缺{breadth.get('auction_summary', {}).get('missing_open_count', 0)} | 均分 {float(breadth.get('auction_summary', {}).get('mean_score', 0) or 0):.1f}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**标的结构**\n"
            f"覆盖 {breadth.get('total_codes', 0)} | ETF {breadth.get('etf_count', 0)} | 个股 {breadth.get('stock_count', 0)} | 主题 {breadth.get('theme_count', 0)} | 集中度 {float(breadth.get('top_theme_share', 0) or 0):.2%}\n"
            f"强：{'、'.join(context.favored_sectors[:2]) if context.favored_sectors else '暂无'} | 弱：{'、'.join(context.weak_sectors[:2]) if context.weak_sectors else '暂无'}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**看板池**\n"
            f"主做：{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 4)}\n"
            f"观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 4)}\n"
            f"关注：{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 4)}\n"
            f"回避：{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 4)}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**主题聚合 Top5**\n" + "\n".join(_preopen_theme_lines(context, 3, 3))
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**开盘跟踪**\n"
            f"顺序：1->2->3->4 | 跟踪：{_preopen_followup_text(context)}\n"
            f"升级：{_preopen_turn_strong_rule(context)}"
        ),
    ]
    return elements


def _preopen_message_text(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    adv = _preopen_adv_counts(context)
    hot_theme = breadth.get("hot_theme_text", "")
    active_names = _format_code_names(context.active_codes, 6)
    focus_names = _format_code_names(context.focus_codes, 6)
    blocked_names = _format_code_names(context.blocked_codes, 6)
    theme_lines = []
    for item in context.theme_rank[:5]:
        sector = item.get("sector", "")
        count = item.get("count", 0)
        codes = item.get("codes", [])[:4]
        names = []
        for code in codes:
            h = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
            names.append(f"{h.get('name', code)}({code})")
        theme_lines.append(f"- {sector}：{count} 只 | {'、'.join(names) if names else '暂无'}")
    if not theme_lines:
        theme_lines.append("- 暂无主题聚合数据")
    action = _preopen_action_label(context)
    action_hint = {
        "进攻": "优先看强主题中的回踩确认，不追弱票。",
        "观察": "只盯重点标的，等开盘后强弱分化再动。",
        "回避": "优先控仓，弱市不追价，只看核心标的是否有承接。",
    }.get(action, "按信号择机")
    return (
        f"【集合竞价详细结果】\n"
        f"动作建议：{action} | {action_hint}\n"
        f"时间：{context.generated_at}\n"
        f"市场评分：{context.market_score:.1f} / 100\n"
        f"市场偏向：{context.market_bias}\n"
        f"盘面判断：{context.session_note}\n"
        f"市场快照：涨{adv['up']} / 跌{adv['down']} / 平{adv['flat']} | 热主题：{hot_theme or '暂无'}\n"
        f"覆盖标的：{breadth.get('total_codes', 0)} | ETF {breadth.get('etf_count', 0)} | 个股 {breadth.get('stock_count', 0)}\n"
        f"主题集中度：{breadth.get('top_theme_share', 0):.2%} | 风险标记：{breadth.get('risk_flag', 'unknown')}\n"
        f"偏强主题：{'、'.join(context.favored_sectors[:3]) if context.favored_sectors else '暂无'}\n"
        f"偏弱主题：{'、'.join(context.weak_sectors[:3]) if context.weak_sectors else '暂无'}\n"
        f"1. 主做：{active_names}\n"
        f"2. 观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 6)}\n"
        f"3. 关注：{focus_names}\n"
        f"4. 回避：{blocked_names}\n"
        f"执行顺序：先1后2，再3，最后4\n"
        f"主题聚合：\n" + "\n".join(theme_lines)
    )


def _preopen_summary_text(context: PreOpenContext) -> str:
    breadth = _preopen_safe_breadth(context)
    action = _preopen_action_label(context)
    return (
        f"【集合竞价总览】\n"
        f"动作建议：{action}\n"
        f"{_preopen_strategy_line(context)}\n"
        f"1. 主做：{_format_code_names(_sort_codes_by_holding_priority(context.active_codes), 4)}\n"
        f"2. 观察：{_format_code_names(_sort_codes_by_holding_priority(context.watch_codes), 4)}\n"
        f"3. 关注：{_format_code_names(_sort_codes_by_holding_priority(context.focus_codes), 4)}\n"
        f"4. 回避：{_format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 4)}\n"
        f"开盘后跟踪：{_preopen_followup_text(context)}\n"
        f"转强条件：{_preopen_turn_strong_rule(context)}\n"
        f"覆盖标的：{breadth.get('total_codes', 0)} | 主题集中度：{breadth.get('top_theme_share', 0):.2%} | 风险标记：{breadth.get('risk_flag', 'unknown')}"
    )


def _send_preopen_feishu(context: PreOpenContext) -> bool:
    global _preopen_pushed_date, _preopen_overview_last_push_at
    today = get_today_str()
    if _preopen_pushed_date == today or not FEISHU_WEBHOOK:
        return False
    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    use_strong = feishu_cfg.get("use_strong_notification", True)
    relay_urgent_alarm = feishu_cfg.get("relay_urgent_alarm_on_feishu", True)
    at_all = feishu_cfg.get("at_all_on_signal", True)
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    template = _preopen_card_template(context)

    summary_title = f"🚨 集合竞价总览 - {FEISHU_KEYWORD}" if use_strong else f"📢 集合竞价总览 - {FEISHU_KEYWORD}"
    summary_payload = _build_preopen_card_payload(
        summary_title,
        _build_preopen_summary_elements(context),
        at_all,
        at_text,
        template=template,
    )
    ok = send_feishu_payload(
        payload=summary_payload,
        success_log="✅ 集合竞价总览已推送飞书",
        error_prefix="集合竞价总览飞书推送",
        trigger_urgent_alarm_after_success=False,
    )

    detail_title = f"🚨 集合竞价详细结果 - {FEISHU_KEYWORD}" if use_strong else f"📢 集合竞价详细结果 - {FEISHU_KEYWORD}"
    detail_payload = _build_preopen_card_payload(
        detail_title,
        _build_preopen_detail_elements(context),
        at_all,
        at_text,
        template=template,
    )
    detail_ok = send_feishu_payload(
        payload=detail_payload,
        success_log="✅ 集合竞价详细结果已推送飞书",
        error_prefix="集合竞价详细飞书推送",
        trigger_urgent_alarm_after_success=use_strong and relay_urgent_alarm,
    )
    if ok or detail_ok:
        _preopen_pushed_date = today
        _preopen_overview_last_push_at = _now()
    return ok and detail_ok


def _send_preopen_monitor_feishu(context: PreOpenContext, now: Optional[datetime] = None) -> bool:
    global _preopen_monitor_last_push_at, _preopen_monitor_last_signature, _preopen_monitor_push_count
    now = now or _now()
    if not _should_push_preopen_monitor(context, now):
        return False
    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    at_all = bool(feishu_cfg.get("at_all_on_preopen_monitor", False))
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    title = f"📊 集合竞价监控 - {FEISHU_KEYWORD}"
    payload = _build_preopen_card_payload(
        title,
        _build_preopen_monitor_elements(context, now),
        at_all,
        at_text,
        template=_preopen_card_template(context),
    )
    ok = send_feishu_payload(
        payload=payload,
        success_log="✅ 集合竞价监控已推送飞书",
        error_prefix="集合竞价监控飞书推送",
        trigger_urgent_alarm_after_success=False,
    )
    if ok:
        _preopen_monitor_last_push_at = now
        _preopen_monitor_last_signature = _preopen_monitor_signature(context)
        _preopen_monitor_push_count += 1
    return ok


def _build_preopen_monitor_elements(context: PreOpenContext, now: datetime) -> List[dict]:
    breadth = _preopen_safe_breadth(context)
    adv = _preopen_adv_counts(context)
    up, down = adv['up'], adv['down']
    # 只保留有抛压的个股（blocked_codes），其余省略
    blocked = _format_code_names(_sort_codes_by_holding_priority(context.blocked_codes), 6)
    blocked_text = blocked if blocked and blocked != "暂无" else "暂无"
    
    elements = [
        _feishu_md_div(
            f"**集合竞价监控**\n"
            f"时间：{now.strftime('%H:%M:%S')} | 动作：{_preopen_action_label(context)} | 评分 {context.market_score:.1f} | 偏向 {context.market_bias} | 风险 {breadth.get('risk_flag', 'unknown')}"
        ),
        _feishu_hr(),
        _feishu_md_div(
            f"**市场热度**\n"
            f"涨跌 涨{up} / 跌{down} / 平{adv['flat']} | 热点 {_preopen_hot_theme_text(context)} | 集中度 {float(breadth.get('top_theme_share', 0) or 0):.2%}"
        ),
    ]
    # 只有存在需要回避的个股时，才展示抛压板块
    if blocked_text != "暂无":
        elements.append(
            _feishu_md_div(
                f"**⚠️ 个股抛压**\n"
                f"回避：{blocked_text}"
            )
        )
    return elements


def _ensure_preopen_context(force: bool = False) -> Optional[PreOpenContext]:
    global PREOPEN_CONTEXT, SESSION_CONTEXT, _preopen_logged_date
    today = get_today_str()
    if not force and PREOPEN_CONTEXT is not None and _preopen_logged_date == today:
        return PREOPEN_CONTEXT
    try:
        PREOPEN_CONTEXT = build_preopen_context()
        SESSION_CONTEXT = {
            "date": today,
            "market_score": PREOPEN_CONTEXT.market_score,
            "market_bias": PREOPEN_CONTEXT.market_bias,
            "session_note": PREOPEN_CONTEXT.session_note,
            "favored_sectors": PREOPEN_CONTEXT.favored_sectors,
            "weak_sectors": PREOPEN_CONTEXT.weak_sectors,
            "focus_codes": PREOPEN_CONTEXT.focus_codes,
        }
        _preopen_logged_date = today
        _record_preopen_trace(PREOPEN_CONTEXT)
        log.info(_format_preopen_brief(PREOPEN_CONTEXT))
        if force or _preopen_pushed_date != today:
            _send_preopen_feishu(PREOPEN_CONTEXT)
        return PREOPEN_CONTEXT
    except Exception as e:
        log.warning(f"⚠️  早盘解读生成失败: {str(e)[:120]}")
        return PREOPEN_CONTEXT


