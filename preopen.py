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
import time
# V3: analyze_auction + format_auction_feishu 由 auction_analyzer.py exec 加载提供（globals）

@dataclass
class PreOpenContext:
    """早盘集合竞价分析结论（V3 竞价增强版）"""
    market_score: float = 0.0
    market_bias: str = "unknown"
    breadth: Dict[str, Any] = field(default_factory=dict)
    session_note: str = ""
    # V3 竞价三层分析
    auction_result: Dict[str, Any] = field(default_factory=dict)
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
_preopen_logged_date = ""
_eod_logged_date = ""
_last_idle_log = datetime.min
_scan_count = 0
_scan_lock = False

# 已移除的变量（飞书推送已禁用）:
# - _preopen_pushed_date（推送状态控制，已无用）
# - _preopen_overview_last_push_at（推送时间戳，已无用）
# - _preopen_monitor_last_push_at（推送时间戳，已无用）
# - _preopen_monitor_push_count（推送计数，已无用）
# - _preopen_monitor_date（推送日期，已无用）


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
        A-3(2026-08-21): 空返回时按 slot 重试 1 次(间隔20s)；仍空则标 top20_status=empty，
        区分"真空"与"中性"(避免日志静默 涨0/跌0 误导)。
        """
        result = {
            "total_up": 0, "total_down": 0, "total_flat": 0,
            "top_gainers": [], "top_volume_stocks": [],
            "sectors": [], "bias": "neutral", "note": "",
        }
        empty_reason = None
        for attempt in range(2):
            empty_reason = self._fetch_top20_attempt(result)
            if empty_reason is None:
                return result
            if attempt == 0:
                print(f"[A-3] Top20 竞价量获取失败({empty_reason})，20s 后重试")
                try:
                    time.sleep(20)
                except Exception:
                    pass
        # 两次仍空 → 显式标注 top20_status=empty（B-2 读到 empty 时降级为单条件）
        result["top20_status"] = "empty"
        result["note"] = f"Top20 数据为空({empty_reason})"
        return result

    def _fetch_top20_attempt(self, result: Dict[str, Any]) -> Optional[str]:
        """单次 Top20 竞价量抓取。成功返回 None；失败返回失败原因字符串。"""
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
                return "无法获取市场快照"

            vol_col = None
            for col in ["成交额", "amount", "成交金额", "turnover"]:
                if col in spot.columns:
                    vol_col = col
                    break
            if not vol_col:
                return "无成交额列"

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
            return None

        except Exception as e:
            result["note"] = f"Top20分析异常: {type(e).__name__}: {str(e)[:80]}"
            log.debug(f"⚠️  Top20竞价量分析失败: {str(e)[:120]}")
            return f"异常:{type(e).__name__}"

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
            # 注意口径：open_gap 为小数（0.0558 = 5.58%），不是百分比数值；7 月老文件 prev_close=0 导致 gap 恒 0 不可信
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

        # 3. 盘前（9:30前）无有效市场数据 → 返回等待状态
        top20_up = top20.get("total_up", 0)
        top20_down = top20.get("total_down", 0)
        if top20_up + top20_down == 0:
            return PreOpenContext(
                market_score=50.0, market_bias="data_pending",
                breadth={"total_codes": max(1, len(self.holdings)),
                         "etf_count": sum(1 for h in self.holdings.values() if h.get("type") == "etf"),
                         "stock_count": max(0, len(self.holdings) - sum(1 for h in self.holdings.values() if h.get("type") == "etf")),
                         "advance_decline": market_snapshot.get("advance_decline", {}),
                         "risk_flag": "unknown", "market_open": False},
                session_note="等待9:30开盘后获取市场数据",
                generated_at=_now().strftime("%Y-%m-%d %H:%M:%S"),
                source=market_snapshot.get("source", "watchlist"),
                market_snapshot=market_snapshot, code_snapshots=code_snapshots,
                top20_volume_analysis=top20,
            )

        # 4. 市场评分 = Top20涨家占比
        market_score = top20_up / (top20_up + top20_down) * 100

        # 5. 偏向判定
        if market_score >= 65:
            market_bias = "risk_on"
        elif market_score >= 45:
            market_bias = "neutral"
        else:
            market_bias = "risk_off"

        session_note = f"竞价额Top20中涨{top20_up}家/跌{top20_down}家"

        auction_summary = {
            "top20_bias": top20.get("bias", "neutral"),
            "top20_up": top20.get("total_up", 0),
            "top20_down": top20.get("total_down", 0),
            "holdings_bullish": bullish_count,
            "holdings_bearish": bearish_count,
            "source_ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # V3: 竞价三层分析（指数+板块+持仓）— 从 exec globals 取
        auction_result = {}
        _analyze = globals().get("analyze_auction")
        if _analyze:
            try:
                auction_result = _analyze(self.holdings)
                _sig_count = len(auction_result.get("auction_signals", []))
                _bias = auction_result.get("market_bias", "?")
                session_note = f"{session_note} | 指数{_bias} | 持仓{_sig_count}股竞价完毕"
            except Exception:
                pass

        return PreOpenContext(
            auction_result=auction_result,
            market_score=market_score,
            market_bias=market_bias,
            breadth={
                "total_codes": total,
                "etf_count": etf_count,
                "stock_count": stock_count,
                "advance_decline": market_snapshot.get("advance_decline", {}) if isinstance(market_snapshot, dict) else {},
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


# ==================== PreOpen 上下文管理 ====================

def build_preopen_context() -> PreOpenContext:
    holdings = load_holdings()
    watchlist = load_watchlist()
    engine = PreOpenEngine(holdings, watchlist)
    context = engine.evaluate()
    engine.persist(context)
    return context


def _is_preopen_monitor_window(now: datetime) -> bool:
    """9:20-9:25 不可撤单时段，数据真实可信"""
    return now.weekday() < 5 and dtime(9, 20) <= now.time() < dtime(9, 25)


def _record_preopen_trace(context: PreOpenContext) -> None:
    try:
        _append_jsonl(_trace_path("preopen_trace"), context.__dict__)
    except Exception:
        pass


# ==================== Feishu 卡片辅助函数（支撑/压力位推送用） ====================

def _feishu_card_header(title: str, template: str) -> dict:
    return {"template": template, "title": {"tag": "plain_text", "content": title}}


def _feishu_md_div(content: str) -> dict:
    return {"tag": "div", "text": {"content": content, "tag": "lark_md"}}


# ==================== 已删除的集合竞价飞书推送相关函数（2026-08-26） ====================
# 以下函数已删除，因为集合竞价推送已禁用，改为 UI 面板显示：
# - _preopen_action_label() → 飞书卡片标签，已无用
# - _preopen_card_template() → 飞书卡片配色，已无用
# - _format_preopen_brief() → 飞书推送文本格式，已无用
# - _feishu_hr() → 飞书分割线，已无用
# - _preopen_safe_breadth() → 飞书数据处理，已无用
# - _preopen_adv_counts() → 飞书涨跌统计，已无用
# - _writeback_auction_summary() → 飞书推送前的回写，已无用
    """A-2(2026-08-21): 竞价分析完成后将 auction_summary 合并回写 preopen_{date}.json。
    读改写保留其他字段；文件不存在则新建最小结构。修复 08-19 auction_summary={} 缺值。"""
    try:
        summary = context.auction_summary if isinstance(context.auction_summary, dict) else {}
        if not summary:
            return
        fp = _preopen_path()
        data = {}
        if os.path.exists(fp):
            try:
                data = json.load(open(fp, encoding="utf-8"))
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        old = data.get("auction_summary") if isinstance(data.get("auction_summary"), dict) else {}
        merged = dict(old)
        for k in ("top20_bias", "top20_up", "top20_down",
                  "holdings_bullish", "holdings_bearish", "source_ts"):
            if summary.get(k) is not None:
                merged[k] = summary[k]
        data["auction_summary"] = merged
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _send_preopen_feishu(context: PreOpenContext, force_push: bool = False) -> bool:
    """已禁用：竞价诊断改为 UI 面板显示（auction_analyzer.py 生成）
    保留函数签名用于向后兼容，但不执行飞书推送。
    """
    return False


def _send_preopen_monitor_feishu(context: PreOpenContext, now: Optional[datetime] = None) -> bool:
    """已禁用：竞价观察中推送（方案 §4.1 取消无意义推送）
    改为在 9:24:45 由 auction_analyzer 生成完整诊断报告。
    """
    return False



def _ensure_preopen_context(force: bool = False) -> Optional[PreOpenContext]:
    global PREOPEN_CONTEXT, SESSION_CONTEXT, _preopen_logged_date
    today = get_today_str()
    if not force and PREOPEN_CONTEXT is not None and _preopen_logged_date == today:
        # 缓存为 data_pending 且已过 9:30 → 刷新
        if PREOPEN_CONTEXT.market_bias == "data_pending" and dtime(9, 30) <= datetime.now().time():
            pass  # 继续执行刷新
        else:
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
        # 早盘竞价分析已改为 UI 面板显示（9:24:45 由 auction_analyzer 生成诊断报告）
        log.info(f"📊 早盘竞价分析完成（评分 {PREOPEN_CONTEXT.market_score:.0f} 分）")
        return PREOPEN_CONTEXT
    except Exception as e:
        log.warning(f"⚠️  早盘解读生成失败: {str(e)[:120]}")
        return PREOPEN_CONTEXT


# ==================== 支撑/压力位 9:25 推送 ====================

_pivot_pushed_date = ""


def _maybe_push_pivot_report(now: datetime) -> bool:
    """9:25-9:30 推送支撑/压力位（每日一次）"""
    global _pivot_pushed_date
    today = get_today_str()
    if _pivot_pushed_date == today:
        return False
    if not FEISHU_WEBHOOK:
        return False
    t = now.time()
    if now.weekday() >= 5 or not (dtime(9, 25) <= t <= dtime(9, 30)):
        return False

    try:
        holdings = load_holdings()
        all_levels = calc_for_holdings(holdings)
        if not all_levels:
            log.info("📊 支撑/压力位：无持仓数据")
            return False

        text = format_pivot_text(all_levels, max_stocks=8)

        card = {"config": {"wide_screen_mode": True},
                "header": _feishu_card_header(f"📊 支撑/压力位 - {FEISHU_KEYWORD}", "blue"),
                "elements": [_feishu_md_div(text)]}
        payload = {"msg_type": "interactive", "card": card, "notify_type": 1}

        ok = send_feishu_payload(
            payload=payload,
            success_log="✅ 支撑/压力位已推送飞书",
            error_prefix="支撑/压力位飞书推送",
        )
        if ok:
            _pivot_pushed_date = today
            log.info(f"📊 支撑/压力位推送完成 ({len(all_levels)} 只)")
        return ok
    except Exception as e:
        log.warning(f"⚠️  支撑/压力位推送异常: {str(e)[:120]}")
        return False
