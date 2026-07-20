# -*- coding: utf-8 -*-
"""
preopen.py — 早盘集合竞价分析引擎（V2 重写版）

基于真实竞价机制：
- 9:15-9:20 可撤单，数据不可信
- 9:20-9:25 不可撤单，数据真实可信
- 全市场竞价量 Top20 分析判定当日风向
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import json
import os

# ==================== PreOpenContext 数据结构 ====================

@dataclass
class PreOpenContext:
    """早盘集合竞价分析结论（V2 简化版）"""
    market_score: float = 0.0
    market_bias: str = "unknown"
    breadth: Dict[str, Any] = field(default_factory=dict)
    session_note: str = ""
    generated_at: str = ""
    source: str = "offline"
    market_snapshot: Dict[str, Any] = field(default_factory=dict)
    code_snapshots: Dict[str, Any] = field(default_factory=dict)
    auction_summary: Dict[str, Any] = field(default_factory=dict)
    top20_volume_analysis: Dict[str, Any] = field(default_factory=dict)
    # 兼容字段（保留空列表）
    theme_rank: List[Dict[str, Any]] = field(default_factory=list)
    focus_codes: List[str] = field(default_factory=list)
    active_codes: List[str] = field(default_factory=list)
    watch_codes: List[str] = field(default_factory=list)
    blocked_codes: List[str] = field(default_factory=list)
    favored_sectors: List[str] = field(default_factory=list)
    weak_sectors: List[str] = field(default_factory=list)


# ==================== 模块级状态变量 ====================
PREOPEN_CONTEXT = None
SESSION_CONTEXT = {}
_preopen_pushed_date = ""
_preopen_logged_date = ""
_preopen_overview_last_push_at = None
_preopen_monitor_last_push_at = None
_preopen_monitor_push_count = 0
_preopen_monitor_date = ""
_eod_logged_date = ""


# ==================== PreOpenEngine ====================

class PreOpenEngine:
    """集合竞价分析引擎（V2）—— 轻量版，聚焦真实竞价信号"""

    def __init__(self, holdings: Dict[str, dict], watchlist: Dict[str, dict]):
        self.holdings = holdings or {}
        self.watchlist = watchlist or {}

    # ----- 全市场快照（保留原逻辑） -----

    def _fetch_market_snapshot(self) -> Dict[str, Any]:
        """拉取全市场快照：涨跌家数、热主题"""
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
                    # 涨幅前5名（个股，非板块概念）
                    top5 = spot.sort_values("涨跌幅", ascending=False).head(5)
                    snapshot["hot_theme"] = top5["名称"].dropna().astype(str).tolist()
                    if not top5.empty:
                        snapshot["index_trend"] = "positive" if float(top5.iloc[0]["涨跌幅"] or 0) > 0 else "negative"
                    # "概念板块"不存在于 stock_zh_a_spot_em() 的列中，所以删除原"概念板块"检查
        except Exception:
            pass
        if not snapshot["market_sentence"]:
            adv = snapshot.get("advance_decline", {})
            if isinstance(adv, dict) and adv and adv.get("up") is not None:
                snapshot["market_sentence"] = f"涨{adv.get('up', 0)} / 跌{adv.get('down', 0)} / 平{adv.get('flat', 0)}"
            else:
                snapshot["market_sentence"] = "市场快照不足，按名单结构解读"
        return snapshot

    # ----- 全市场竞价量 Top20 分析（新增 V2）-----

    def _fetch_top20_auction_volume(self) -> Dict[str, Any]:
        """
        提取早盘集合竞价成交量最大的20家公司，判定当日市场风向。
        9:20-9:25 期间用 akshare 全市场快照，按成交额降序取前20。
        """
        result = {
            "total_up": 0, "total_down": 0, "total_flat": 0,
            "top_gainers": [], "top_volume_stocks": [],
            "sectors": [], "bias": "neutral", "note": "",
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
            if not isinstance(spot, pd.DataFrame) or spot.empty:
                result["note"] = "无法获取市场快照"
                return result

            vol_col = None
            for col in ["成交额", "amount", "成交金额", "turnover"]:
                if col in spot.columns:
                    vol_col = col
                    break
            if not vol_col:
                result["note"] = "无成交额列"
                return result

            spot[vol_col] = pd.to_numeric(spot[vol_col], errors="coerce").fillna(0)
            spot["涨跌幅"] = pd.to_numeric(spot["涨跌幅"], errors="coerce").fillna(0)

            top20 = spot.nlargest(20, vol_col)
            top20_list = []
            for _, row in top20.iterrows():
                name = str(row.get("名称", row.get("name", "")))
                code = str(row.get("代码", row.get("code", "")))
                pct = float(row.get("涨跌幅", 0))
                vol = float(row.get(vol_col, 0))
                top20_list.append({
                    "code": code,
                    "name": name,
                    "change_pct": round(pct, 2),
                    "volume": vol,
                })
            result["top_volume_stocks"] = top20_list

            up_count = int((top20["涨跌幅"] > 0).sum())
            down_count = int((top20["涨跌幅"] < 0).sum())
            flat_count = int((top20["涨跌幅"] == 0).sum())
            result["total_up"] = up_count
            result["total_down"] = down_count
            result["total_flat"] = flat_count

            total_valid = up_count + down_count
            if total_valid > 0:
                up_ratio = up_count / total_valid
                if up_ratio >= 0.70:
                    result["bias"] = "strong_bullish"
                elif up_ratio >= 0.50:
                    result["bias"] = "bullish"
                elif up_ratio <= 0.30:
                    result["bias"] = "strong_bearish"
                elif up_ratio <= 0.50:
                    result["bias"] = "bearish"

            result["note"] = f"Top20竞价量：涨{up_count}/跌{down_count}/平{flat_count}，偏向{result['bias']}"

        except Exception as e:
            result["note"] = f"Top20分析异常: {type(e).__name__}: {str(e)[:80]}"
            log.debug(f"⚠️  Top20竞价量分析失败: {str(e)[:120]}")

        return result

    # ----- 主评估方法（V2 简化版）-----

    def evaluate(self) -> PreOpenContext:
        """基于真实竞价信号生成早盘结论"""
        market_snapshot_raw = self._fetch_market_snapshot()
        market_snapshot = market_snapshot_raw if isinstance(market_snapshot_raw, dict) else {}

        # 1. 全市场竞价量 Top20
        top20 = self._fetch_top20_auction_volume()
        market_snapshot["top20_volume_analysis"] = top20

        # 2. 持仓竞价分析
        total = max(1, len(self.holdings))
        etf_count = sum(1 for h in self.holdings.values() if h.get("type") == "etf")
        stock_count = total - etf_count
        bullish_count = 0
        bearish_count = 0
        code_snapshots = {}

        for code, holding in self.holdings.items():
            price = float(holding.get("pre_close", 0) or 0)
            daily_ctx = get_daily_context(code, holding or {}, current_price=price)
            prev_close = float(daily_ctx.get("daily_prev_close", 0) or 0)
            open_gap = (price - prev_close) / prev_close if prev_close > 0 else 0.0

            direction = "neutral"
            if open_gap > 0.005:
                direction = "bullish"
                bullish_count += 1
            elif open_gap < -0.005:
                direction = "bearish"
                bearish_count += 1

            code_snapshots[code] = {
                "code": code,
                "name": holding.get("name", code),
                "open_gap": open_gap,
                "direction": direction,
                "prev_close": prev_close,
            }

        # 3. 市场评分 = Top20涨家占比（清晰透明）
        top20_up = top20.get("total_up", 0)
        top20_down = top20.get("total_down", 0)
        market_score = top20_up / max(1, top20_up + top20_down) * 100

        # 4. 偏向判定
        if market_score >= 65:
            market_bias = "risk_on"
        elif market_score >= 45:
            market_bias = "neutral"
        else:
            market_bias = "risk_off"

        # 5. 简短判断
        session_note = f"竞价额Top20中涨{top20_up}家/跌{top20_down}家"

        auction_summary = {
            "top20_bias": top20.get("bias", "neutral"),
            "top20_up": top20.get("total_up", 0),
            "top20_down": top20.get("total_down", 0),
            "holdings_bullish": bullish_count,
            "holdings_bearish": bearish_count,
            "source_ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        return PreOpenContext(
            market_score=market_score,
            market_bias=market_bias,
            breadth={
                "total_codes": total,
                "etf_count": etf_count,
                "stock_count": stock_count,
                "advance_decline": adv,
                "hot_theme": market_snapshot.get("hot_theme", []),
                "hot_theme_text": "、".join(market_snapshot.get("hot_theme", [])[:3])
                    if isinstance(market_snapshot.get("hot_theme"), list) else "",
                "risk_flag": market_snapshot.get("risk_flag", "unknown"),
                "market_open": market_snapshot.get("market_open", False),
                "auction_summary": auction_summary,
            },
            session_note=session_note,
            generated_at=_now().strftime("%Y-%m-%d %H:%M:%S"),
            source=market_snapshot.get("source", "watchlist"),
            market_snapshot=market_snapshot,
            code_snapshots=code_snapshots,
            auction_summary=auction_summary,
            top20_volume_analysis=top20,
        )

    def persist(self, context: PreOpenContext) -> None:
        try:
            os.makedirs(PREOPEN_DIR, exist_ok=True)
            with open(_preopen_path(), "w", encoding="utf-8") as f:
                json.dump(context.__dict__, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ==================== 保留的跨模块辅助函数 ====================

def _buy_soft_support_count(buy_momentum_ok: bool, buy_ema_ok: bool, buy_volume_ok: bool,
                            buy_price_ok: bool, buy_gap_ok: bool, buy_detail_count_ok: bool,
                            buy_time_ready: bool, buy_15m_ok: bool = True,
                            buy_5m_ok: bool = True) -> int:
    return sum([buy_momentum_ok, buy_ema_ok, buy_volume_ok, buy_price_ok,
                buy_gap_ok, buy_detail_count_ok, buy_time_ready, buy_15m_ok, buy_5m_ok])


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


def _special_loss_threshold_adjustments(code: str, action: str, buy_threshold: int,
                                         sell_threshold: int, buy_score: float,
                                         sell_score: float, price: float, vwap: float,
                                         is_strong_pullback: bool) -> tuple:
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


# ==================== PreOpen 上下文管理 ====================

def build_preopen_context() -> PreOpenContext:
    holdings = load_holdings()
    watchlist = load_watchlist()
    engine = PreOpenEngine(holdings, watchlist)
    context = engine.evaluate()
    engine.persist(context)
    return context


def _preopen_action_label(context: PreOpenContext) -> str:
    """评分=Top20涨家占比, >=65偏多, <=40偏空"""
    if context.market_score >= 65:
        return "进攻"
    if context.market_score <= 40:
        return "回避"
    return "观察"


def _preopen_card_template(context: PreOpenContext) -> str:
    action = _preopen_action_label(context)
    if action == "进攻":
        return "green"
    if action == "回避":
        return "red"
    return "blue"


def _feishu_card_header(title: str, template: str) -> dict:
    return {"template": template, "title": {"tag": "plain_text", "content": title}}


def _is_preopen_monitor_window(now: datetime) -> bool:
    return now.weekday() < 5 and dtime(9, 15) <= now.time() < dtime(9, 25)


def _format_code_names(codes: List[str], limit: int = 4) -> str:
    names = []
    for code in codes[:limit]:
        holding = HOLDINGS.get(code, {}) if isinstance(HOLDINGS, dict) else {}
        names.append(f"{holding.get('name', code)}({code})")
    return "、".join(names) if names else "暂无"


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


def _format_preopen_brief(context: PreOpenContext) -> str:
    top20 = context.top20_volume_analysis if isinstance(context.top20_volume_analysis, dict) else {}
    top20_up = top20.get("total_up", 0)
    top20_down = top20.get("total_down", 0)
    return (
        f"竞价额Top20：涨{top20_up}/跌{top20_down}\n"
        f"评分={context.market_score:.0f}分 → {_preopen_action_label(context)}"
    )


def _record_preopen_trace(context: PreOpenContext) -> None:
    try:
        _append_jsonl(_trace_path("preopen_trace"), context.__dict__)
    except Exception:
        pass


# ==================== Feishu 卡片辅助函数 ====================

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
    return {"up": int(adv.get("up", 0) or 0), "down": int(adv.get("down", 0) or 0), "flat": int(adv.get("flat", 0) or 0)}


def _preopen_adv_text(context: PreOpenContext) -> str:
    adv = _preopen_adv_counts(context)
    return f"涨{adv['up']} / 跌{adv['down']} / 平{adv['flat']}"


def _preopen_hot_theme_text(context: PreOpenContext, limit: int = 3) -> str:
    snapshot_hot = context.market_snapshot.get("hot_theme", []) if isinstance(context.market_snapshot, dict) else []
    if isinstance(snapshot_hot, list) and snapshot_hot:
        return "、".join([str(x) for x in snapshot_hot[:limit] if str(x).strip()]) or "暂无"
    return _preopen_safe_breadth(context).get("hot_theme_text", "") or "暂无"


# ==================== Feishu 推送函数 ====================

def _send_preopen_feishu(context: PreOpenContext) -> bool:
    global _preopen_pushed_date, _preopen_overview_last_push_at
    today = get_today_str()
    if _preopen_pushed_date == today or not FEISHU_WEBHOOK:
        return False

    top20 = context.top20_volume_analysis if isinstance(context.top20_volume_analysis, dict) else {}
    top20_up = top20.get("total_up", 0)
    top20_down = top20.get("total_down", 0)
    top20_bias = top20.get("bias", "neutral")
    volume_stocks = top20.get("top_volume_stocks", [])[:6]
    action = _preopen_action_label(context)
    template = _preopen_card_template(context)

    lines = [f"{action}"]
    lines.append(f"竞价额TOP6")
    for s in volume_stocks:
        pct = s.get("change_pct", 0)
        tag = "🔴" if pct < 0 else "🟢"
        lines.append(f" {tag}{s.get('name','')} {pct:+.1f}%")
    lines.append(f"涨{top20_up}家/跌{top20_down}家")
    lines.append(top20_bias)

    text = "\n".join(lines)

    card = {"config": {"wide_screen_mode": True},
            "header": _feishu_card_header(f"📊 早盘竞价 - {FEISHU_KEYWORD}", template),
            "elements": [_feishu_md_div(text)]}
    payload = {"msg_type": "interactive", "card": card, "notify_type": 1}

    ok = send_feishu_payload(
        payload=payload,
        success_log="✅ 早盘竞价分析已推送飞书",
        error_prefix="早盘竞价分析飞书推送",
    )
    if ok:
        _preopen_pushed_date = today
        _preopen_overview_last_push_at = _now()
    return ok


def _send_preopen_monitor_feishu(context: PreOpenContext, now: Optional[datetime] = None) -> bool:
    global _preopen_monitor_last_push_at, _preopen_monitor_push_count
    now = now or _now()
    if not FEISHU_WEBHOOK or not _is_preopen_monitor_window(now):
        return False
    if _preopen_monitor_push_count >= 5:
        return False
    if _preopen_monitor_last_push_at is not None and (now - _preopen_monitor_last_push_at).total_seconds() < 60:
        return False

    elements = [_feishu_md_div(
        f"{_preopen_action_label(context)} | {context.market_score:.0f}分 | {now.strftime('%H:%M')}"
    )]

    card = {"config": {"wide_screen_mode": True},
            "header": _feishu_card_header(f"📊 竞价监控 - {FEISHU_KEYWORD}", _preopen_card_template(context)),
            "elements": elements}
    payload = {"msg_type": "interactive", "card": card, "notify_type": 1}

    ok = send_feishu_payload(
        payload=payload,
        success_log="✅ 竞价监控已推送飞书",
        error_prefix="竞价监控飞书推送",
    )
    if ok:
        _preopen_monitor_last_push_at = now
        _preopen_monitor_push_count += 1
    return ok


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
