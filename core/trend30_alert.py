# -*- coding: utf-8 -*-
"""30min 趋势状态翻转飞书告警（2026-10-04，方案 §4.6）。

盘中枚举 持仓(手动) + 自选(monitoring/signal) 标的，比较 30min 状态机最新状态与上次
持久化状态：BULL↔BEAR 互转、或任一 →RANGE（退出）即记事件并飞行书（每次翻转推一次）。

⚠️ 手动做T已于 2026-09-22 关闭 ⇒ 本告警为**纯通知**，不触发任何交易动作；
   §4.6「正T失败当日必卖」不接线。

去重/持久化：t_io/state/trend30_state_seen.json = {code:{state,bar_time,date}}（TTL 修剪），
仿 main.py 的 index_divergence_seen 事件键模式。
"""
import json
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger("trend30_alert")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_FP = os.path.join(_BASE, "t_io", "state", "trend30_state_seen.json")
_WL_FP = os.path.join(_BASE, "t_io", "state", "watchlist_buy.json")
_TTL_DAYS = 5
_CN = {"BULL": "多头", "BEAR": "空头", "RANGE": "震荡"}


def _load_seen() -> dict:
    try:
        with open(_STATE_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_seen(seen: dict) -> None:
    try:
        cutoff = (datetime.now() - timedelta(days=_TTL_DAYS)).strftime("%Y-%m-%d")
        pruned = {c: v for c, v in seen.items()
                  if isinstance(v, dict) and str(v.get("date", "")) >= cutoff}
        os.makedirs(os.path.dirname(_STATE_FP), exist_ok=True)
        tmp = _STATE_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATE_FP)
    except Exception:
        pass


def _codes() -> list:
    """持仓(手动 qty>0) + 自选 monitoring/signal，剥离 _A/_B 后缀去重。"""
    out, seen = [], set()

    def _add(c):
        b = str(c).split("_")[0]
        if b and b not in seen:
            seen.add(b)
            out.append(b)

    try:
        from src.holdings_repo import load_held_manual
        for c in load_held_manual():
            _add(c)
    except Exception:
        pass
    try:
        with open(_WL_FP, "r", encoding="utf-8") as f:
            wl = json.load(f)
        for c, info in (wl.get("stocks") or {}).items():
            if isinstance(info, dict) and info.get("status") in ("monitoring", "signal"):
                _add(c)
    except Exception:
        pass
    return out


def _names() -> dict:
    names = {}
    try:
        from src.holdings_repo import load_union
        for c, h in load_union().items():
            names[str(c)] = (h or {}).get("name") or str(c)
    except Exception:
        pass
    try:
        with open(_WL_FP, "r", encoding="utf-8") as f:
            wl = json.load(f)
        for c, info in (wl.get("stocks") or {}).items():
            if isinstance(info, dict) and info.get("name"):
                names.setdefault(str(c).split("_")[0], info["name"])
    except Exception:
        pass
    return names


def check_trend30_flips(codes=None, persist=True) -> list:
    """比较各码 30min 最新状态与上次持久化值，返回翻转事件列表。首见（无上次）不报。"""
    try:
        from analysis.trend30.adapter import get_trend30
    except Exception:
        return []
    codes = codes or _codes()
    seen = _load_seen()
    names = _names()
    today = datetime.now().strftime("%Y-%m-%d")
    events = []
    for c in codes:
        try:
            r = get_trend30(c)
        except Exception:
            continue
        if r.get("source") != "30min" or not r.get("state"):
            continue
        cur, bt = r.get("state"), r.get("bar_time")
        prev = seen.get(c) or {}
        prev_state = prev.get("state")
        seen[c] = {"state": cur, "bar_time": bt, "date": today}
        if prev_state and prev_state != cur:
            events.append({"code": c, "name": names.get(c, c), "from": prev_state, "to": cur,
                           "adx": r.get("adx"), "confidence": r.get("confidence"), "bar_time": bt})
    if persist:
        _save_seen(seen)
    return events


def build_trend30_card(events: list) -> dict:
    """30min 趋势翻转提醒卡片。无事件返回 None。"""
    if not events:
        return None
    lines = ["**30分钟趋势翻转提醒**",
             f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}（30min 状态机）", ""]
    for e in events:
        ar = "" if e.get("adx") is None else f" ｜ ADX {e['adx']}"
        cf = "（弱）" if e.get("confidence") == "low" else ""
        lines.append(f"🔀 **{e['code']}** {e['name']}  "
                     f"{_CN.get(e['from'], e['from'])}→{_CN.get(e['to'], e['to'])}{cf}{ar}")
    lines += ["", "📌 BULL/BEAR=趋势确认，RANGE=退出/未确认（震荡）。",
              "⚠️ 纯趋势通知，不构成交易指令；请人工确认。"]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"template": "orange",
                       "title": {"tag": "plain_text", "content": "🔀 30min 趋势翻转提醒"}},
            "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
        },
    }


def run_trend30_alert(codes=None, dry_run=False) -> list:
    """检测翻转 → 推飞书。返回本次推送的事件。推送失败不写状态去重（下次重试）。

    注：状态见文件在检测时即写入（记录"当前状态"），避免同一次翻转在下一轮重复判 new；
    故即便推送失败，也只在下一轮"状态再次变化"时才可能重报。"""
    events = check_trend30_flips(codes=codes, persist=True)
    if not events:
        return []
    card = build_trend30_card(events)
    if dry_run:
        return events
    # ⚠️ send_feishu_payload 的必填参数是 (payload, success_log, error_prefix)。
    # 2026-10-10 前这里只传了 card，每次抛 TypeError 又被裸 except 吞掉 ⇒
    # 本告警同样从未推送成功过且不留痕迹（与 core/hunter_ma5_alert.py 同一处坑）。
    try:
        from config import send_feishu_payload
        ok = send_feishu_payload(
            card, success_log=f"30min趋势翻转飞书推送: {len(events)} 只",
            error_prefix="30min趋势翻转推送")
        if not ok:
            log.warning("⚠️ 30min趋势翻转推送返回失败")
    except Exception as e:
        log.warning(f"⚠️ 30min趋势翻转推送异常: {type(e).__name__}: {str(e)[:180]}")
    return events
